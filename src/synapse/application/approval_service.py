"""Trusted approval application boundary; not connected to any transport."""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
import time
from typing import Protocol

from synapse.domain.approvals import ApprovalChange, ApprovalState
from synapse.persistence.audit_chain import append_audit, flush_anchors
from synapse.domain.contracts import ApprovalId, validate_id
from synapse.persistence.approval_store import _timestamp, write_approval_request, write_approval_transition
from synapse.persistence.candidate_manifests import is_invalidated as is_manifest_invalidated
from synapse.persistence.idempotency import CommandResult, execute_idempotent
from synapse.persistence.state_store import StateConflict


@dataclass(frozen=True, slots=True)
class Actor:
    """Stable authority-issued subject, not a client-supplied label."""
    subject: str
    kind: str

    def __post_init__(self):
        validate_id(self.subject)
        if self.kind not in ("human", "agent"):
            raise ValueError("invalid actor kind")


class ApprovalAuthority(Protocol):
    """Trusted adapter holds authentication/revocation stable for the context.

    Subject IDs must be stable, globally comparable across human and agent
    identities, and cannot be selected by the caller. Policy must bind the full
    supplied resource and prohibit unauthorized actors and operations.
    """
    def authenticated(self, credential: object) -> AbstractContextManager[Actor]: ...
    def allows(self, actor: Actor, action: str, resource: dict) -> bool: ...


class ApprovalAccessDenied(PermissionError):
    """Access denied; ``code`` is a coarse, fixed audit reason (never caller-supplied text)."""
    def __init__(self, *args, code: str = "unauthenticated", principal: str | None = None):
        super().__init__(*args)
        self.code = code
        self.principal = principal  # set only after authentication succeeded


DENIAL_REASONS = frozenset({"unauthenticated", "not_permitted", "self_approval", "conflict", "rate_limited"})
DENIAL_WINDOW_SECONDS = 60
DENIAL_MAX_PER_WINDOW = 20
DENIAL_MAX_SUPPRESSED_BUCKETS = 64  # bounds the in-memory dropped-attempt counters


def _principal(actor: Actor) -> str:
    return actor.kind + ":" + actor.subject


def _audit(db: sqlite3.Connection, actor: Actor, action: str, approval_id: str,
           correlation_id: str, details: dict) -> None:
    append_audit(db, _principal(actor), action, approval_id, "allowed", correlation_id,
                 json.dumps(details, sort_keys=True, separators=(",", ":")), datetime.now(timezone.utc).isoformat())


class ApprovalService:
    """Daemon-only authorization and idempotent approval state changes.

    No transport or credential implementation is supplied. The caller must
    ensure the injected authority really authenticates humans and that workers
    cannot use their credentials; an operator bearer token is insufficient.
    """
    def __init__(self, connection: sqlite3.Connection, authority: ApprovalAuthority, *,
                 clock=time.monotonic):
        self._db = connection
        self._authority = authority
        self._clock = clock
        self._denial_window: tuple[int, dict] = (-1, {})  # (window index, {bucket: rows written})
        self._denial_suppressed: dict[str, int] = {}  # {bucket: attempts dropped since its last written row}

    def preauthenticate(self, credential: object) -> None:
        """Run the authority's slow credential check off the event loop when it offers one. Touches no
        database (called from a worker thread); the result is consumed by the next decision call."""
        pre = getattr(self._authority, "preauthenticate", None)
        if pre is not None:
            pre(credential)

    def _record_authority_events(self) -> None:
        """Persist lockout / operator-unlock events the authority queued (loop thread only)."""
        pop = getattr(self._authority, "pop_events", None)
        if pop is None or self._db.in_transaction:
            return
        for action, details in pop():
            try:
                append_audit(self._db, "unauthenticated", action, "approver", "denied" if action == "approver.lockout" else "allowed",
                             "approver-lockout", json.dumps({"reason": "lockout" if action == "approver.lockout" else "unlock", **details},
                                        sort_keys=True, separators=(",", ":")),
                             datetime.now(timezone.utc).isoformat())
            except Exception:
                pass

    def record_denied(self, reason: str, *, correlation_id: str, http_status: int,
                      approval_id: str | None = None, principal: str | None = None) -> bool:
        """Best-effort audit of a denied decision attempt; returns True if a row was written.

        Never takes or stores credentials or caller-supplied text: reason is one of a
        fixed set, and resource is the approval id only for authenticated (post-token)
        denials, otherwise the constant "unauthenticated". Bounded: at most
        DENIAL_MAX_PER_WINDOW rows per DENIAL_WINDOW_SECONDS per principal bucket
        (unauthenticated callers share one bucket); the first overflow in a window
        writes one "rate_limited" marker, later attempts are dropped but counted, and the next row written for that principal carries
        ``suppressed_before`` (in-memory: a restart loses the count). Failures to write
        are swallowed so HTTP responses never change. Committed independently of the
        failed decision transaction (called after it has rolled back).
        """
        self._record_authority_events()
        try:
            if reason not in DENIAL_REASONS or self._db.in_transaction:
                return False
            who = principal or "unauthenticated"
            index = int(self._clock() // DENIAL_WINDOW_SECONDS)
            if self._denial_window[0] != index:
                self._denial_window = (index, {})
            counts = self._denial_window[1]
            used = counts.get(who, 0)
            if used > DENIAL_MAX_PER_WINDOW:
                if who in self._denial_suppressed or len(self._denial_suppressed) < DENIAL_MAX_SUPPRESSED_BUCKETS:
                    self._denial_suppressed[who] = self._denial_suppressed.get(who, 0) + 1
                return False
            if used == DENIAL_MAX_PER_WINDOW:
                reason = "rate_limited"
            counts[who] = used + 1
            details = {"reason": reason, "http_status": http_status}
            dropped = self._denial_suppressed.get(who, 0)
            if dropped:  # the next written row reports how many attempts the cap hid (F10); lost if the daemon restarts first
                details["suppressed_before"] = dropped
            append_audit(self._db, who, "approval.decide", (approval_id or "unauthenticated") if principal else "unauthenticated",
                         "denied", correlation_id, json.dumps(details, sort_keys=True, separators=(",", ":")),
                         datetime.now(timezone.utc).isoformat())
            self._denial_suppressed.pop(who, None)  # only after the row (and its count) is durably written
            return True
        except Exception:
            return False

    def request(self, credential: object, *, approval_id: str, run_id: str, operation: str,
                target: str, candidate_sha256: str, policy_version: str, expires_at: str,
                idempotency_key: str, correlation_id: str) -> CommandResult:
        for value in (approval_id, run_id, operation, target, policy_version, idempotency_key, correlation_id):
            validate_id(value)
        with self._authority.authenticated(credential) as actor:
            if not isinstance(actor, Actor):
                raise ApprovalAccessDenied("Approval access denied")
            resource = {"approval_id": approval_id, "run_id": run_id, "operation": operation,
                        "target": target, "candidate_sha256": candidate_sha256,
                        "policy_version": policy_version, "expires_at": expires_at}
            if self._authority.allows(actor, "approval.request", dict(resource)) is not True:
                raise ApprovalAccessDenied("Approval access denied")
            request = {**resource, "requester_subject": actor.subject, "requester_kind": actor.kind}
            def write(db):
                write_approval_request(db, **resource, requester_subject=actor.subject, requester_kind=actor.kind)
                _audit(db, actor, "approval.request", approval_id, correlation_id, request)
                return {"approval_id": approval_id, "state": "requested", "state_version": 0}
            return execute_idempotent(self._db, principal=_principal(actor), operation="approval.request",
                                      key=idempotency_key, request=request, execute=write)

    def decide(self, credential: object, *, approval_id: str, expected_version: int,
               decision: str, operation: str, target: str, candidate_sha256: str,
               policy_version: str, idempotency_key: str, correlation_id: str,
               reason: str | None = None) -> CommandResult:
        for value in (approval_id, operation, target, candidate_sha256, policy_version, idempotency_key, correlation_id):
            validate_id(value)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a nonnegative integer")
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved or rejected")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 4096):
            raise ValueError("invalid decision reason")
        if reason is not None and isinstance(credential, str) and len(credential) >= 16 and credential in reason:
            # Reasons are stored in audit rows and events; never persist a credential.
            raise ValueError("invalid decision reason")
        with self._authority.authenticated(credential) as actor:
            if not isinstance(actor, Actor) or actor.kind != "human":
                raise ApprovalAccessDenied("Approval access denied", code="not_permitted")
            row = self._db.execute("SELECT run_id,operation,target,candidate_manifest_sha256,policy_version,expires_at,requester_subject,requester_kind FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
            if row is None or row[6] is None or row[7] not in ("human", "agent") or not row[6].strip():
                raise ApprovalAccessDenied("Approval access denied", code="not_permitted", principal=_principal(actor))
            if actor.subject == row[6]:
                raise ApprovalAccessDenied("Approval access denied", code="self_approval", principal=_principal(actor))
            if (operation, target, candidate_sha256, policy_version) != (row[1], row[2], row[3], row[4]):
                raise StateConflict("Approval binding differs")
            resource = {"approval_id": approval_id, "run_id": row[0], "operation": operation, "target": target,
                        "candidate_sha256": candidate_sha256, "policy_version": policy_version,
                        "expires_at": row[5], "requester_subject": row[6], "requester_kind": row[7],
                        "decision": decision}
            if self._authority.allows(actor, "approval.decide", dict(resource)) is not True:
                raise ApprovalAccessDenied("Approval access denied", code="not_permitted", principal=_principal(actor))
            request = {**resource, "expected_version": expected_version, "reason": reason}
            def write(db):
                if _timestamp(row[5]) <= datetime.now(timezone.utc):
                    raise StateConflict("Approval expired")
                if decision == "approved" and is_manifest_invalidated(db, row[0], row[3]):
                    # ADR 026 D9: an invalidated candidate manifest can never be approved (a 0019 trigger agrees).
                    raise StateConflict("manifest_invalidated")
                change = ApprovalChange(ApprovalId(approval_id), ApprovalState.REQUESTED, ApprovalState(decision), expected_version)
                version = write_approval_transition(db, change, principal=_principal(actor), reason=reason,
                                                    candidate_sha256=candidate_sha256, policy_version=policy_version)
                _audit(db, actor, "approval.decide", approval_id, correlation_id, request)
                return {"approval_id": approval_id, "state": decision, "state_version": version}
            result = execute_idempotent(self._db, principal=_principal(actor), operation="approval.decide",
                                        key=idempotency_key, request=request, execute=write)
            flush_anchors(self._db)  # anchor the decision now, not at the next timer tick (ADR 018)
            return result

    def revoke(self, credential: object, *, approval_id: str, expected_version: int, idempotency_key: str,
               correlation_id: str, reason: str | None = None) -> CommandResult:
        """Withdraw an approved, not-yet-consumed approval (approved -> revoked), audited."""
        for value in (approval_id, idempotency_key, correlation_id):
            validate_id(value)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a nonnegative integer")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 4096):
            raise ValueError("invalid revocation reason")
        if reason is not None and isinstance(credential, str) and len(credential) >= 16 and credential in reason:
            raise ValueError("invalid revocation reason")
        with self._authority.authenticated(credential) as actor:
            if not isinstance(actor, Actor) or actor.kind != "human":
                raise ApprovalAccessDenied("Approval access denied", code="not_permitted")
            row = self._db.execute("SELECT run_id FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
            if row is None:
                raise ApprovalAccessDenied("Approval access denied", code="not_permitted", principal=_principal(actor))
            resource = {"approval_id": approval_id, "run_id": row[0]}
            if self._authority.allows(actor, "approval.revoke", dict(resource)) is not True:
                raise ApprovalAccessDenied("Approval access denied", code="not_permitted", principal=_principal(actor))
            request = {**resource, "expected_version": expected_version, "reason": reason}
            def write(db):
                change = ApprovalChange(ApprovalId(approval_id), ApprovalState.APPROVED, ApprovalState.REVOKED, expected_version)
                version = write_approval_transition(db, change, principal=_principal(actor), reason=reason)
                _audit(db, actor, "approval.revoke", approval_id, correlation_id, request)
                return {"approval_id": approval_id, "state": "revoked", "state_version": version}
            result = execute_idempotent(self._db, principal=_principal(actor), operation="approval.revoke",
                                        key=idempotency_key, request=request, execute=write)
            flush_anchors(self._db)  # anchor the revocation now (ADR 018)
            return result
