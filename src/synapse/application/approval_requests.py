"""Internal daemon API to create approval requests for a run.

Not an HTTP route: only daemon code holding this object can request, as the
fixed ``daemon:workflow`` requester. Real agent sessions are out of scope.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3

from synapse.application.approval_service import ApprovalService
from synapse.domain.contracts import validate_id
from synapse.infrastructure.daemon_requester import DaemonRequesterAuthority
from synapse.persistence.approval_store import _timestamp
from synapse.persistence.idempotency import CommandResult

MAX_APPROVAL_TTL = timedelta(hours=24)


def approval_id_for(run_id: str, idempotency_key: str) -> str:
    """Deterministic ID so a retried command always targets the same record."""
    digest = hashlib.sha256(f"{run_id}\0{idempotency_key}".encode("utf-8")).hexdigest()
    return "apr-" + digest[:32]


class DaemonApprovalRequester:
    def __init__(self, connection: sqlite3.Connection):
        self._db = connection
        self._authority = DaemonRequesterAuthority()
        self._service = ApprovalService(connection, self._authority)

    def request_approval(self, *, run_id: str, operation: str, target: str, candidate_sha256: str,
                         policy_version: str, expires_at: str, idempotency_key: str,
                         correlation_id: str) -> CommandResult:
        validate_id(run_id)
        validate_id(idempotency_key)
        expiry = _timestamp(expires_at)
        now = datetime.now(timezone.utc)
        if expiry <= now or expiry > now + MAX_APPROVAL_TTL:
            raise ValueError("expiry must be in the future and within 24 hours")
        if self._db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            raise ValueError("unknown run")
        return self._service.request(
            self._authority.capability, approval_id=approval_id_for(run_id, idempotency_key),
            run_id=run_id, operation=operation, target=target, candidate_sha256=candidate_sha256,
            policy_version=policy_version, expires_at=expires_at,
            idempotency_key=idempotency_key, correlation_id=correlation_id)
