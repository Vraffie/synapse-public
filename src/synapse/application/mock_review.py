"""In-process mock author/reviewer handoff through scoped durable messaging."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from collections.abc import Callable

from synapse.application.message_gateway import MessageGateway
from synapse.application.session_authority import SessionAuthority
from synapse.domain.contracts import new_id, validate_id
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.persistence.artifact_store import publish_artifact


class MockReviewHandoff:
    """Synchronous trusted mock actors for a single dedicated conversation.

    This does not establish human or independently authenticated verifier
    identity. The caller supplies a trusted independent review function and
    owns the daemon lock, connection, pre-registered actors and dedicated
    thread. It must keep all other senders out of both actors' inboxes.
    """
    def __init__(self, connection: sqlite3.Connection, blobs: ArtifactBlobStore, *, run_id: str,
                 author_id: str, reviewer_id: str, thread_id: str,
                 review: Callable[[str, bytes], bool]):
        for value in (run_id, author_id, reviewer_id, thread_id):
            validate_id(value)
        if author_id == reviewer_id:
            raise ValueError("Author and reviewer must be distinct instances")
        if not callable(review):
            raise TypeError("review must be callable")
        self._db, self._blobs, self._review = connection, blobs, review
        self._run, self._author, self._reviewer, self._thread = run_id, author_id, reviewer_id, thread_id

    def _candidate(self, task_id: str, candidate: bytes) -> tuple[str, str]:
        if not isinstance(candidate, bytes):
            raise TypeError("candidate must be bytes")
        row = self._db.execute(
            "SELECT a.id FROM tasks t JOIN task_attempts a ON a.task_id=t.id AND a.attempt_no=t.attempt_count "
            "WHERE t.id=? AND t.run_id=? AND t.state='verifying' AND a.state='completed'", (task_id, self._run)
        ).fetchone()
        if row is None:
            raise ValueError("Current completed attempt is required")
        attempt_id = row[0]
        digest = hashlib.sha256(candidate).hexdigest()
        candidates = []
        for artifact_id, metadata in self._db.execute("SELECT id,provenance_json FROM artifacts WHERE sha256=?", (digest,)):
            try:
                provenance = json.loads(metadata)
            except (TypeError, ValueError):
                continue
            if (isinstance(provenance, dict) and provenance.get('run_id') == self._run
                    and provenance.get('task_id') == task_id and provenance.get('attempt_id') == attempt_id):
                candidates.append(artifact_id)
        if len(candidates) != 1 or self._blobs.read(digest) != candidate:
            raise ValueError("Exactly one verified candidate artifact is required")
        return attempt_id, candidates[0]

    @staticmethod
    def _call(gateway: MessageGateway, secret: str, op: str, **payload):
        return gateway.invoke(secret, op, json.dumps(payload, separators=(",", ":"), allow_nan=False).encode())

    def __call__(self, task_id: str, candidate: bytes) -> bool:
        validate_id(task_id)
        if self._db.in_transaction:
            raise RuntimeError("Mock handoff requires an autocommit connection")
        for instance in (self._author, self._reviewer):
            row = self._db.execute("SELECT run_id FROM agent_instances WHERE id=?", (instance,)).fetchone()
            if row is None or row[0] != self._run:
                raise ValueError("Both registered instances must belong to the run")
        if self._db.execute("SELECT 1 FROM threads WHERE id=? AND run_id=? AND status='open'", (self._thread, self._run)).fetchone() is None:
            raise ValueError("An open run-scoped thread is required")
        if self._db.execute("SELECT 1 FROM message_deliveries WHERE recipient_instance_id IN (?,?) AND state IN ('pending','leased') LIMIT 1", (self._author, self._reviewer)).fetchone():
            raise RuntimeError("Mock actor inbox is not empty; reconcile before review")
        attempt_id, artifact_id = self._candidate(task_id, candidate)
        request_id, finding_id = new_id(), new_id()
        review_receipt, author_receipt = new_id(), new_id()
        authority = SessionAuthority(max_sessions=2)
        author = authority.issue(instance_id=self._author, run_id=self._run, grants=[
            ('message.send', self._thread), ('message.recipient', self._reviewer), ('message.artifact', artifact_id),
            ('inbox.claim', self._author), ('thread.access', self._thread), ('message.result_artifact', author_receipt)])
        reviewer = None
        try:
            reviewer = authority.issue(instance_id=self._reviewer, run_id=self._run, grants=[
                ('message.send', self._thread), ('message.recipient', self._author), ('message.read_artifact', artifact_id),
                ('inbox.claim', self._reviewer), ('thread.access', self._thread), ('message.result_artifact', review_receipt)])
            gateway = MessageGateway(self._db, authority)
            created_at = datetime.now(timezone.utc).isoformat()
            request = self._call(gateway, author.secret, 'send', idempotency_key=request_id, message_id=request_id,
                                 thread_id=self._thread, type='review.request', payload={'task_id': task_id, 'attempt_id': attempt_id},
                                 created_at=created_at, recipients=[self._reviewer], artifact_refs=[artifact_id], correlation_id=attempt_id)
            request_delivery, = request.response
            lease = self._claim(gateway, reviewer, request_delivery, request_id)
            gateway.grant_owned_delivery(reviewer.identity.principal, request_delivery)
            received = self._call(gateway, reviewer.secret, 'read', delivery_id=request_delivery)
            if received['artifact_refs'] != [artifact_id] or received['payload'] != {'task_id': task_id, 'attempt_id': attempt_id}:
                raise RuntimeError("Review request does not match candidate")
            verdict = self._review(task_id, self._blobs.read(hashlib.sha256(candidate).hexdigest()))
            if type(verdict) is not bool:
                raise ValueError("Reviewer must return bool")
            finding = self._call(gateway, reviewer.secret, 'send', idempotency_key=finding_id, message_id=finding_id,
                                 thread_id=self._thread, type='review.finding', payload={'task_id': task_id, 'attempt_id': attempt_id, 'approved': verdict},
                                 created_at=datetime.now(timezone.utc).isoformat(), recipients=[self._author],
                                 correlation_id=attempt_id, causation_id=request_id)
            finding_delivery, = finding.response
            self._receipt(review_receipt, request_delivery, request_id, reviewer.identity.principal, result_message_id=finding_id)
            self._call(gateway, reviewer.secret, 'complete', idempotency_key='ack-' + request_id,
                       delivery_id=request_delivery, token=lease['token'], success=True, result_artifact_id=review_receipt)
            response_lease = self._claim(gateway, author, finding_delivery, finding_id)
            gateway.grant_owned_delivery(author.identity.principal, finding_delivery)
            response = self._call(gateway, author.secret, 'read', delivery_id=finding_delivery)
            if (response['causation_id'] != request_id or response['correlation_id'] != attempt_id
                    or response['payload'] != {'task_id': task_id, 'attempt_id': attempt_id, 'approved': verdict}):
                raise RuntimeError("Review finding does not match request")
            self._receipt(author_receipt, finding_delivery, finding_id, author.identity.principal)
            self._call(gateway, author.secret, 'complete', idempotency_key='ack-' + finding_id,
                       delivery_id=finding_delivery, token=response_lease['token'], success=True, result_artifact_id=author_receipt)
            return response['payload']['approved']
        finally:
            authority.revoke(author.identity.principal)
            if reviewer is not None:
                authority.revoke(reviewer.identity.principal)

    def _claim(self, gateway: MessageGateway, actor, delivery_id: str, message_id: str) -> dict:
        lease = gateway.claim_exact_for_mock(actor.secret, delivery_id, message_id)
        if lease.message_id != message_id:
            raise RuntimeError("Unexpected message identity")
        return {'delivery_id': lease.delivery_id, 'token': lease.token}

    def _receipt(self, artifact_id: str, delivery_id: str, message_id: str, principal: str,
                 *, result_message_id: str | None = None) -> None:
        content = json.dumps({'delivery_id': delivery_id, 'message_id': message_id,
                              'result_message_id': result_message_id}, sort_keys=True).encode()
        publish_artifact(self._db, self._blobs, chunks=[content], artifact_id=artifact_id, media_type='application/json',
                         provenance={'run_id': self._run, 'delivery_id': delivery_id, 'kind': 'mock-delivery-receipt'},
                         principal=principal, idempotency_key='receipt-' + artifact_id)
