"""Scoped messaging commands for authenticated daemon adapters.

The adapter MUST authenticate the principal independently. Authority is a
trusted server-side policy implementation, never supplied by an agent.
"""
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from synapse.domain.contracts import MessageEnvelope, MessageId, MessageType, RunId, encode_payload, validate_id
from synapse.persistence.delivery_store import claim_deliveries, claim_deliveries_idempotently, complete_delivery_idempotently
from synapse.persistence.message_scope import delivery_scope, instance_in_run, read_owned_delivery
from synapse.persistence.message_store import send_message


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    principal: str
    instance_id: str
    run_id: str

    def __post_init__(self):
        for value in (self.principal, self.instance_id, self.run_id):
            validate_id(value)


class MessageAuthority(Protocol):
    """Trusted session registry and policy; a denial must return False.

    inbox.claim grants access to every delivery for this instance in its run.
    Other decisions apply to the exact named resource and run.
    """
    def identity(self, principal: str) -> AgentIdentity | None: ...
    def allows(self, identity: AgentIdentity, action: str, resource: str) -> bool: ...


class MessageAccessDenied(PermissionError):
    """No matching session, capability, or owned delivery."""


@dataclass(frozen=True, slots=True)
class MessageLimits:
    """Trusted server configuration; adapters must not accept these from agents."""
    payload_bytes: int = 65536
    recipients: int = 32
    artifacts: int = 32

    def __post_init__(self):
        for value in (self.payload_bytes, self.recipients, self.artifacts):
            if type(value) is not int or value < 1:
                raise ValueError("Message limits must be positive integers")


class MessageService:
    def __init__(self, connection, authority: MessageAuthority, *, limits: MessageLimits = MessageLimits()):
        if not isinstance(limits, MessageLimits):
            raise TypeError("limits must be MessageLimits")
        self._connection = connection
        self._authority = authority
        self._limits = limits

    def _identity(self, principal: str) -> AgentIdentity:
        validate_id(principal)
        identity = self._authority.identity(principal)
        if (not isinstance(identity, AgentIdentity) or identity.principal != principal
                or not instance_in_run(self._connection, identity.instance_id, identity.run_id)):
            raise MessageAccessDenied("Message access denied")
        return identity

    def _allow(self, identity: AgentIdentity, action: str, resource: str) -> None:
        validate_id(resource)
        if self._authority.allows(identity, action, resource) is not True:
            raise MessageAccessDenied("Message access denied")

    def send(self, *, principal: str, idempotency_key: str, message_id: str,
             thread_id: str, type: MessageType, payload, created_at: str,
             recipients: tuple[str, ...], artifact_refs: tuple[str, ...] = (),
             correlation_id: str | None = None, causation_id: str | None = None):
        identity = self._identity(principal)
        self._allow(identity, "message.send", thread_id)
        if not isinstance(recipients, (tuple, list)):
            raise TypeError("Recipients must be a sequence of identifiers")
        if not isinstance(artifact_refs, (tuple, list)):
            raise TypeError("Artifact references must be a sequence of identifiers")
        if len(recipients) > self._limits.recipients:
            raise ValueError("Recipient limit exceeded")
        if len(artifact_refs) > self._limits.artifacts:
            raise ValueError("Artifact reference limit exceeded")
        for recipient in recipients:
            self._allow(identity, "message.recipient", recipient)
        for artifact in artifact_refs:
            self._allow(identity, "message.artifact", artifact)
        message = MessageEnvelope(
            id=MessageId(message_id), run_id=RunId(identity.run_id), thread_id=thread_id,
            sender_instance_id=identity.instance_id, type=type, payload=payload,
            created_at=created_at, artifact_refs=tuple(artifact_refs),
            correlation_id=correlation_id, causation_id=causation_id,
        )
        if len(encode_payload(message.payload).encode("utf-8")) > self._limits.payload_bytes:
            raise ValueError("Message payload limit exceeded")
        return send_message(self._connection, message, recipients=tuple(recipients),
                            principal=identity.principal, idempotency_key=idempotency_key)

    def claim(self, *, principal: str, limit: int = 100, lease_seconds: int = 60,
              max_attempts: int = 3, now: datetime | None = None,
              idempotency_key: str | None = None):
        identity = self._identity(principal)
        self._allow(identity, "inbox.claim", identity.instance_id)
        conn = self._connection
        if idempotency_key is not None:
            return claim_deliveries_idempotently(
                conn, principal=identity.principal, idempotency_key=idempotency_key,
                recipient=identity.instance_id, limit=limit, lease_seconds=lease_seconds,
                max_attempts=max_attempts, now=now,
            )
        if conn.in_transaction:
            raise RuntimeError("Nested transactions are unsupported")
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = claim_deliveries(conn, recipient=identity.instance_id, limit=limit,
                                      lease_seconds=lease_seconds, max_attempts=max_attempts, now=now)
            conn.execute("COMMIT")
            return result
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def claim_exact_for_mock(self, *, principal: str, delivery_id: str, message_id: str):
        """Trusted mock-only exclusive inbox claim; all failures roll back."""
        identity = self._identity(principal)
        self._allow(identity, "inbox.claim", identity.instance_id)
        validate_id(delivery_id)
        validate_id(message_id)
        conn = self._connection
        if conn.in_transaction:
            raise RuntimeError("Nested transactions are unsupported")
        conn.execute("BEGIN IMMEDIATE")
        try:
            scope = delivery_scope(conn, delivery_id, identity.instance_id)
            if scope is None or scope[0] != identity.run_id:
                raise MessageAccessDenied("Message access denied")
            if conn.execute("SELECT message_id FROM message_deliveries WHERE id=?", (delivery_id,)).fetchone() != (message_id,):
                raise RuntimeError("Mock message identity mismatch")
            other = conn.execute(
                "SELECT 1 FROM message_deliveries WHERE recipient_instance_id=? "
                "AND id<>? AND state IN ('pending','leased') LIMIT 1",
                (identity.instance_id, delivery_id),
            ).fetchone()
            if other:
                raise RuntimeError("Mock inbox contains another delivery")
            leases = claim_deliveries(conn, recipient=identity.instance_id, limit=1)
            if len(leases) != 1 or leases[0].delivery_id != delivery_id:
                raise RuntimeError("Mock delivery is not claimable")
            conn.execute("COMMIT")
            return leases[0]
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def read_delivery(self, *, principal: str, delivery_id: str):
        identity = self._identity(principal)
        validate_id(delivery_id)
        record = read_owned_delivery(self._connection, delivery_id, identity.instance_id)
        if record is None or record[0][3] != identity.run_id:
            raise MessageAccessDenied("Message access denied")
        row, artifacts = record
        self._allow(identity, "delivery.read", delivery_id)
        self._allow(identity, "thread.access", row[4])
        for artifact in artifacts:
            self._allow(identity, "message.read_artifact", artifact)
        return {
            "delivery_id": row[0], "delivery_state": row[1], "message_id": row[2],
            "run_id": row[3], "thread_id": row[4], "sender_instance_id": row[5],
            "type": row[6], "schema_version": row[7], "payload": json.loads(row[8]),
            "created_at": row[9], "correlation_id": row[10], "causation_id": row[11],
            "artifact_refs": list(artifacts),
        }

    def complete(self, *, principal: str, idempotency_key: str, delivery_id: str,
                 token: str, success: bool, result_artifact_id: str | None = None,
                 max_attempts: int = 3, now: datetime | None = None):
        identity = self._identity(principal)
        validate_id(delivery_id)
        scope = delivery_scope(self._connection, delivery_id, identity.instance_id)
        if scope is None or scope[0] != identity.run_id:
            raise MessageAccessDenied("Message access denied")
        self._allow(identity, "delivery.complete", delivery_id)
        self._allow(identity, "thread.access", scope[1])
        if result_artifact_id is not None:
            self._allow(identity, "message.result_artifact", result_artifact_id)
        return complete_delivery_idempotently(
            self._connection, principal=identity.principal, idempotency_key=idempotency_key,
            recipient=identity.instance_id, delivery_id=delivery_id, token=token,
            success=success, result_artifact_id=result_artifact_id,
            max_attempts=max_attempts, now=now,
        )
