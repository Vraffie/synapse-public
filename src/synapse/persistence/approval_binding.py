"""Execution-time approval binding check (read-only).

An approval record never authorizes execution by itself. Immediately before an
external action the daemon must call ``verify_approval_for_execution`` with the
operation it is *about to perform*. On success it returns an ``ApprovalGrant``
proof object that only this module can mint; execution code should require one
as a parameter. Every other outcome raises ``ApprovalNotGranted``.
"""
import hmac
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from synapse.domain.contracts import validate_id
from synapse.persistence import audit_chain
from synapse.persistence.candidate_manifests import is_invalidated as is_manifest_invalidated

_SHA256 = re.compile(r"[0-9a-f]{64}")


class DenialReason(StrEnum):
    NOT_FOUND = "not_found"
    WRONG_RUN = "wrong_run"
    NOT_APPROVED = "not_approved"
    EXPIRED = "expired"
    BINDING_MISMATCH = "binding_mismatch"
    REVOKED = "revoked"
    MALFORMED = "malformed"
    CHAIN_BROKEN = "chain_broken"
    MANIFEST_INVALIDATED = "manifest_invalidated"


class ApprovalNotGranted(Exception):
    """Base error: execution must not proceed. ``reason`` is machine-readable."""

    def __init__(self, reason: DenialReason, message: str | None = None) -> None:
        self.reason = DenialReason(reason)
        super().__init__(message or f"Approval not granted: {self.reason.value}")


class ApprovalNotFound(ApprovalNotGranted): ...
class ApprovalWrongRun(ApprovalNotGranted): ...
class ApprovalNotApproved(ApprovalNotGranted): ...
class ApprovalExpired(ApprovalNotGranted): ...
class ApprovalBindingMismatch(ApprovalNotGranted): ...
class ApprovalRevoked(ApprovalNotGranted): ...
class ApprovalMalformed(ApprovalNotGranted): ...
class ApprovalChainBroken(ApprovalNotGranted): ...
class ApprovalManifestInvalidated(ApprovalNotGranted): ...


_ERRORS = {
    DenialReason.NOT_FOUND: ApprovalNotFound, DenialReason.WRONG_RUN: ApprovalWrongRun,
    DenialReason.NOT_APPROVED: ApprovalNotApproved, DenialReason.EXPIRED: ApprovalExpired,
    DenialReason.BINDING_MISMATCH: ApprovalBindingMismatch, DenialReason.REVOKED: ApprovalRevoked,
    DenialReason.MALFORMED: ApprovalMalformed, DenialReason.CHAIN_BROKEN: ApprovalChainBroken,
    DenialReason.MANIFEST_INVALIDATED: ApprovalManifestInvalidated,
}


def _deny(reason: DenialReason, message: str | None = None) -> ApprovalNotGranted:
    return _ERRORS[reason](reason, message)


_MINT = object()


@dataclass(frozen=True, slots=True, init=False)
class ApprovalGrant:
    """Proof that a binding check succeeded at ``verified_at``; not a bearer credential.

    Cannot be constructed, copied, pickled or subclassed outside this module.
    It records what was verified so audit/execution code can cite it, and it is
    only valid for the instant of the check: execution must follow promptly.
    """
    approval_id: str
    state_version: int
    decided_at: str
    principal: str
    verified_at: str

    def __init__(self, *args, **kwargs) -> None:
        raise TypeError("ApprovalGrant can only be obtained from verify_approval_for_execution")

    def __init_subclass__(cls, **kwargs) -> None:
        raise TypeError("ApprovalGrant cannot be subclassed")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        raise TypeError("ApprovalGrant cannot be serialized")


def _mint(token: object, **fields) -> ApprovalGrant:
    if token is not _MINT:  # pragma: no cover - defensive
        raise TypeError("ApprovalGrant is not constructible")
    grant = object.__new__(ApprovalGrant)
    for name, value in fields.items():
        object.__setattr__(grant, name, value)
    return grant


def _equal(left: object, right: object) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode(), right.encode())


def _utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def verify_approval_for_execution(connection: sqlite3.Connection, *, approval_id: str, run_id: str,
                                  operation: str, target: str, candidate_sha256: str,
                                  policy_version: str, now: datetime | None = None) -> ApprovalGrant:
    """Verify, in one read snapshot, that an approval covers exactly this execution.

    ``connection`` must not be inside a transaction. Nothing is written. The
    caller supplies the values it is about to execute, never values read back
    from the approval. ``now`` must be timezone-aware (default: current UTC);
    an approval is expired when ``expires_at <= now``.
    """
    try:
        for value in (approval_id, run_id, operation, target, policy_version):
            validate_id(value)
        if not isinstance(candidate_sha256, str) or not _SHA256.fullmatch(candidate_sha256):
            raise ValueError("candidate_sha256")
        if now is None:
            now = datetime.now(timezone.utc)
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(timezone.utc)
    except (ValueError, TypeError) as error:
        raise _deny(DenialReason.MALFORMED, "Malformed verification input") from error
    if connection.in_transaction:
        raise RuntimeError("Approval binding check requires its own read transaction")
    try:
        audit_chain.require_intact(connection)   # fail closed on a broken audit chain (ADR 018)
    except audit_chain.ChainBroken as error:
        raise _deny(DenialReason.CHAIN_BROKEN, "Audit chain verification failed") from error
    connection.execute("BEGIN")
    try:
        row = connection.execute(
            "SELECT run_id,operation,target,candidate_manifest_sha256,policy_version,state,state_version,"
            "expires_at,decided_at,decision_principal,requester_subject "
            "FROM approval_requests WHERE id=?", (approval_id,)
        ).fetchone()
        invalidated = row is not None and is_manifest_invalidated(connection, row[0], row[3])
    finally:
        connection.execute("COMMIT")
    if row is None:
        raise _deny(DenialReason.NOT_FOUND)
    (s_run, s_op, s_target, s_sha, s_policy, state, version, expires_at, decided_at,
     principal, requester) = row
    if not _equal(s_run, run_id):
        raise _deny(DenialReason.WRONG_RUN)
    if state == "revoked":
        raise _deny(DenialReason.REVOKED)
    if state == "expired":
        raise _deny(DenialReason.EXPIRED)
    if state != "approved":
        raise _deny(DenialReason.NOT_APPROVED)
    try:
        expiry = _utc(expires_at)
        _utc(decided_at)
        if type(version) is not int or version < 1:
            raise ValueError("version")
        if not isinstance(s_sha, str) or not _SHA256.fullmatch(s_sha):
            raise ValueError("stored digest")
    except (ValueError, TypeError) as error:
        raise _deny(DenialReason.MALFORMED, "Malformed approval record") from error
    if expiry <= now:
        raise _deny(DenialReason.EXPIRED)
    matches = [_equal(s_op, operation), _equal(s_target, target), _equal(s_sha, candidate_sha256),
               _equal(s_policy, policy_version)]
    if not all(matches):
        raise _deny(DenialReason.BINDING_MISMATCH)
    if invalidated:  # ADR 026 D9: a manifest invalidated after the decision authorizes nothing
        raise _deny(DenialReason.MANIFEST_INVALIDATED)
    if (not isinstance(principal, str) or not principal.strip()
            or not isinstance(requester, str) or not requester.strip()):
        raise _deny(DenialReason.MALFORMED, "Approval lacks a decision principal or requester identity")
    # decision_principal is namespaced ("human:<subject>"); requester_subject is bare.
    # Compare subjects like with like (and the raw strings, for legacy bare principals).
    # Strip whatever namespace prefix there is (any kind, not a fixed list); a bare principal has none.
    # Comparison is case-sensitive: subjects are opaque ids and are not normalised anywhere in the codebase.
    approver_subject = principal.split(":", 1)[-1]
    if _equal(approver_subject, requester) or _equal(principal, requester):
        raise _deny(DenialReason.MALFORMED, "Approver equals requester")
    try:
        audit_chain.require_decision_audited(connection, approval_id, principal)
    except audit_chain.ChainBroken as error:
        raise _deny(DenialReason.CHAIN_BROKEN, "Approval decision is not in the audit chain") from error
    return _mint(_MINT, approval_id=approval_id, state_version=version, decided_at=decided_at,
                 principal=principal, verified_at=now.isoformat())
