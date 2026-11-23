"""Reserve a fresh attempt for a task that just resumed from an approval gate.

``reserve_attempt`` only accepts ASSIGNED tasks. A gate-resumed task is RUNNING
and has no unresolved attempt, so it needs its own reservation path. This one
demands an ``ApprovalGrant`` (proof that the execution-time binding recheck
passed) and that the grant belongs to the task's own consumed gate.
"""
from __future__ import annotations

import sqlite3

from synapse.domain.contracts import EventEnvelope, encode_payload, validate_id
from synapse.domain.workflow import State
from synapse.persistence.approval_binding import ApprovalGrant
from synapse.persistence.audit_chain import append_audit
from synapse.persistence.state_store import StateConflict


def reserve_attempt_after_approval(connection: sqlite3.Connection, *, task_id: str, attempt_id: str,
                                   expected_task_version: int, principal: str,
                                   grant: ApprovalGrant) -> int:
    """Reserve attempt N+1 for a RUNNING task inside the caller's transaction.

    Returns the attempt number. The attempt is left in ``created``; the caller
    moves it to ``running`` in the same transaction.
    """
    if not connection.in_transaction:
        raise RuntimeError("Attempt reservation requires a command transaction")
    if type(grant) is not ApprovalGrant:
        raise TypeError("An ApprovalGrant from verify_approval_for_execution is required")
    for value in (task_id, attempt_id, principal):
        validate_id(value)
    if type(expected_task_version) is not int or expected_task_version < 0:
        raise ValueError("expected_task_version must be a nonnegative integer")
    row = connection.execute(
        "SELECT t.run_id,t.state,t.state_version,t.attempt_count,t.assigned_profile_id,r.state,g.approval_id,g.resolved_at "
        "FROM tasks t JOIN runs r ON r.id=t.run_id JOIN task_approval_gates g ON g.task_id=t.id WHERE t.id=?",
        (task_id,)).fetchone()
    if row is None or row[1] != State.RUNNING or row[2] != expected_task_version:
        raise StateConflict("Task state/version mismatch")
    if row[5] != "running":
        raise StateConflict("Run is not running")
    if row[4] is None:
        raise ValueError("Task must have an assigned profile")
    if row[7] is None or row[6] != grant.approval_id:
        raise StateConflict("Grant does not belong to this task's consumed approval gate")
    if connection.execute(
        "SELECT 1 FROM task_attempts WHERE task_id=? AND state NOT IN ('completed','failed','cancelled')",
        (task_id,)).fetchone():
        raise StateConflict("Task already has an active or unresolved attempt")
    number = row[3] + 1
    event = EventEnvelope.create(
        aggregate_type="task", aggregate_id=task_id, event_type="task.attempt_reserved",
        payload={"attempt_id": attempt_id, "attempt_number": number, "version": expected_task_version + 1,
                 "principal": principal, "approval_id": grant.approval_id,
                 "approval_version": grant.state_version})
    updated = connection.execute(
        "UPDATE tasks SET attempt_count=?,state_version=state_version+1,updated_at=? "
        "WHERE id=? AND state=? AND state_version=?",
        (number, event.occurred_at, task_id, State.RUNNING, expected_task_version))
    if updated.rowcount != 1:
        raise StateConflict("Task state/version mismatch")
    connection.execute(
        "INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES(?,?,?,?,?)",
        (attempt_id, task_id, number, row[4], "created"))
    connection.execute(
        "INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (event.id, event.aggregate_type, task_id, row[0], event.event_type, event.schema_version,
         encode_payload(event.payload), event.occurred_at))
    connection.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
    # F13: use of an approval is audited in the same transaction that reserves the attempt.
    append_audit(connection, principal, "approval.executed", grant.approval_id, "allowed", None,
                 encode_payload({"task_id": task_id, "attempt_id": attempt_id, "attempt_number": number,
                                 "approval_version": grant.state_version}), event.occurred_at)
    return number
