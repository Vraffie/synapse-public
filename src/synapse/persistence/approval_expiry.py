"""Daemon-owned expiry sweep for overdue requested approvals.

Only the daemon's single database connection may call this. When an approval
that gates a parked task expires, the task is failed in the same transaction
through ``write_task_approval_resolution`` (the existing gate-resolution path).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import sqlite3

from synapse.domain.approvals import ApprovalChange, ApprovalState
from synapse.persistence.audit_chain import append_audit
from synapse.domain.contracts import ApprovalId
from synapse.persistence.approval_store import _timestamp, write_approval_transition
from synapse.persistence.state_store import StateConflict
from synapse.persistence.task_approval_gate import write_task_approval_resolution

log = logging.getLogger(__name__)
ACTION = "approval.expire"
_TERMINAL_RUNS = ("succeeded", "failed", "cancelled")


def _fail_gated_task(connection: sqlite3.Connection, approval_id: str, principal: str) -> str | None:
    """Fail the task parked on ``approval_id`` through gate resolution, if any.

    Runs inside the caller's transaction. Returns the failed task id, or None
    when there is no unresolved parked gate (already resolved, task not parked)
    or the run is terminal (gate resolution refuses terminal runs; the approval
    still expires). Never touches the triggers: resolution is the only writer.
    """
    row = connection.execute(
        "SELECT t.id,t.state_version FROM task_approval_gates g JOIN tasks t ON t.id=g.task_id "
        "JOIN runs r ON r.id=t.run_id WHERE g.approval_id=? AND g.resolved_at IS NULL "
        "AND t.state='awaiting_approval' AND r.state NOT IN (?,?,?)", (approval_id, *_TERMINAL_RUNS)).fetchone()
    if row is None:
        return None
    write_task_approval_resolution(connection, task_id=row[0], expected_task_version=row[1], principal=principal)
    return row[0]


def expire_due_approvals(connection: sqlite3.Connection, *, now: datetime | None = None,
                         limit: int = 100, principal: str = "daemon:expiry") -> list[str]:
    """Expire up to ``limit`` requested approvals whose ``expires_at <= now``.

    Each approval gets its own short BEGIN IMMEDIATE transaction holding the
    compare-and-swap transition, event, outbox row and audit entry together.
    Rows with naive or invalid timestamps are skipped and logged. Rows decided
    concurrently are skipped silently. Returns the ids expired by this call.
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    if connection.in_transaction:
        raise RuntimeError("Expiry sweep must not run inside a transaction")
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    due: list[tuple[str, int, str]] = []
    for approval_id, version, expires_at in connection.execute(
            "SELECT id,state_version,expires_at FROM approval_requests WHERE state='requested' ORDER BY id"):
        try:
            expiry = _timestamp(expires_at)
        except (ValueError, TypeError):
            log.warning("approval %s has an invalid or naive expires_at; skipped", approval_id)
            continue
        if expiry <= now:
            due.append((approval_id, version, expiry.isoformat()))
            if len(due) >= limit:
                break
    expired: list[str] = []
    for approval_id, version, expires_at in due:
        connection.execute("BEGIN IMMEDIATE")
        try:
            write_approval_transition(
                connection,
                ApprovalChange(ApprovalId(approval_id), ApprovalState.REQUESTED, ApprovalState.EXPIRED, version),
                principal=principal, reason="expired")
            _fail_gated_task(connection, approval_id, principal)
            append_audit(
                connection, principal, ACTION, approval_id, "allowed", "expiry:" + approval_id,
                json.dumps({"expires_at": expires_at, "expected_version": version},
                           sort_keys=True, separators=(",", ":")),
                datetime.now(timezone.utc).isoformat())
            connection.execute("COMMIT")
        except StateConflict:
            connection.execute("ROLLBACK")  # decided concurrently, or not yet due by the store's clock
            continue
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        expired.append(approval_id)
    _heal_expired_gates(connection, limit, principal)
    return expired


def _heal_expired_gates(connection: sqlite3.Connection, limit: int, principal: str) -> None:
    """Fail tasks still parked on an already-expired approval (e.g. expired before this fix).

    Idempotent: once resolved, the gate no longer matches. Each task is its own
    transaction; a concurrent resolution (StateConflict) is skipped.
    """
    parked = connection.execute(
        "SELECT g.approval_id FROM task_approval_gates g JOIN approval_requests a ON a.id=g.approval_id "
        "JOIN tasks t ON t.id=g.task_id JOIN runs r ON r.id=t.run_id WHERE g.resolved_at IS NULL "
        "AND a.state='expired' AND t.state='awaiting_approval' AND r.state NOT IN (?,?,?) ORDER BY g.task_id LIMIT ?",
        (*_TERMINAL_RUNS, limit)).fetchall()
    for (approval_id,) in parked:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _fail_gated_task(connection, approval_id, principal)
            connection.execute("COMMIT")
        except StateConflict:
            connection.execute("ROLLBACK")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


async def run_expiry_sweeps(connection: sqlite3.Connection, interval: float, *, limit: int = 100,
                            principal: str = "daemon:expiry") -> None:
    """Sweep every ``interval`` seconds until cancelled.

    Runs on the caller's event-loop thread with the daemon's own connection;
    each sweep is bounded by ``limit`` and yields to the loop between sweeps.
    Failures are logged and retried next interval; cancellation propagates.
    """
    if interval <= 0:
        raise ValueError("interval must be positive")
    while True:
        try:
            expire_due_approvals(connection, limit=limit, principal=principal)
        except Exception:
            log.exception("approval expiry sweep failed")
        await asyncio.sleep(interval)
