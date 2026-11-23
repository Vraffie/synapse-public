"""Daemon-owned approval records; callers must authorize human decisions."""
from datetime import datetime, timezone
import re
import sqlite3

from synapse.domain.approvals import ApprovalChange, ApprovalState
from synapse.domain.contracts import EventEnvelope, encode_payload, validate_id
from synapse.persistence.state_store import StateConflict


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _event(connection, *, approval_id, run_id, kind, payload):
    event = EventEnvelope.create(aggregate_type="approval", aggregate_id=approval_id,
                                 event_type=kind, payload=payload)
    connection.execute(
        "INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (event.id, event.aggregate_type, approval_id, run_id, kind, event.schema_version,
         encode_payload(event.payload), event.occurred_at),
    )
    connection.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))


def write_approval_request(connection: sqlite3.Connection, *, approval_id: str, run_id: str,
                           operation: str, target: str, candidate_sha256: str,
                           policy_version: str, expires_at: str,
                           requester_subject: str | None = None, requester_kind: str | None = None) -> None:
    """Insert a request and its event in a caller-owned command transaction."""
    if not connection.in_transaction:
        raise RuntimeError("Approval request requires a command transaction")
    for value in (approval_id, run_id, operation, target, policy_version):
        validate_id(value)
    if not isinstance(candidate_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", candidate_sha256):
        raise ValueError("candidate_sha256 must be a lowercase SHA-256 digest")
    if (requester_subject is None) != (requester_kind is None):
        raise ValueError("requester subject and kind must be supplied together")
    if requester_subject is not None:
        validate_id(requester_subject)
        if requester_kind not in ("human", "agent"):
            raise ValueError("invalid requester kind")
    now = datetime.now(timezone.utc)
    expiry = _timestamp(expires_at)
    if expiry <= now:
        raise ValueError("expiry must be in the future")
    connection.execute(
        "INSERT INTO approval_requests(id,run_id,operation,target,candidate_manifest_sha256,policy_version,"
        "state,expires_at,requested_at,state_version,requester_subject,requester_kind) VALUES(?,?,?,?,?,?,'requested',?,?,0,?,?)",
        (approval_id, run_id, operation, target, candidate_sha256, policy_version,
         expiry.isoformat(), now.isoformat(), requester_subject, requester_kind),
    )
    _event(connection, approval_id=approval_id, run_id=run_id, kind="approval.requested",
           payload={"operation": operation, "target": target, "candidate_sha256": candidate_sha256,
                    "policy_version": policy_version, "expires_at": expiry.isoformat(), "version": 0,
                    "requester_subject": requester_subject, "requester_kind": requester_kind})


def write_approval_transition(connection: sqlite3.Connection, change: ApprovalChange, *,
                              principal: str, reason: str | None = None,
                              candidate_sha256: str | None = None,
                              policy_version: str | None = None,
                              environment: str | None = None) -> int:
    """Compare-and-swap a decision; authorization is a required caller obligation.

    Approved decisions must match the stored candidate and policy and be unexpired.
    This does not authorize execution: the caller must recheck the binding at use.
    If the approval has an environment binding (ADR-022), approving requires ``environment`` equal to it; approvals
    without a binding behave exactly as before.
    """
    if not connection.in_transaction:
        raise RuntimeError("Approval transition requires a command transaction")
    validate_id(principal)
    if reason is not None and (not isinstance(reason, str) or len(reason) > 4096):
        raise ValueError("invalid decision reason")
    row = connection.execute(
        "SELECT run_id,state,state_version,expires_at,candidate_manifest_sha256,policy_version "
        "FROM approval_requests WHERE id=?", (change.approval_id,)
    ).fetchone()
    if row is None or row[1] != change.before.value or row[2] != change.expected_version:
        raise StateConflict("Approval state/version mismatch")
    now = datetime.now(timezone.utc)
    expiry = _timestamp(row[3])
    if change.after == ApprovalState.APPROVED:
        if expiry <= now or candidate_sha256 != row[4] or policy_version != row[5]:
            raise StateConflict("Approval expired or candidate/policy changed")
        try:
            bound = connection.execute("SELECT environment FROM approval_environment_binding WHERE approval_id=?",
                                       (change.approval_id,)).fetchone()
        except sqlite3.OperationalError:  # binding table not installed: nothing can be bound
            bound = None
        if bound is not None and environment != bound[0]:
            raise StateConflict("Approval is bound to a different environment than the one decided")
    if change.after == ApprovalState.EXPIRED and expiry > now:
        raise StateConflict("Approval has not expired")
    version = change.expected_version + 1
    result = connection.execute(
        "UPDATE approval_requests SET state=?,state_version=?,decided_at=?,decision_principal=?,decision_reason=? "
        "WHERE id=? AND state=? AND state_version=?",
        (change.after.value, version, now.isoformat(), principal, reason,
         change.approval_id, change.before.value, change.expected_version),
    )
    if result.rowcount != 1:
        raise StateConflict("Approval state/version mismatch")
    _event(connection, approval_id=change.approval_id, run_id=row[0], kind="approval.state_changed",
           payload={"before": change.before.value, "after": change.after.value,
                    "version": version, "principal": principal, "reason": reason})
    return version
