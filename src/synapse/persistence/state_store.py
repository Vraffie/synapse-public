"""Compare-and-swap task transitions with atomic event and outbox writes."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from uuid import uuid4

from synapse.domain.workflow import GATE_ONLY_TRANSITIONS, State, StateChange, validate_transition
from synapse.domain.contracts import validate_id


class StateConflict(RuntimeError):
    """An aggregate changed or disappeared before the expected write."""


def transition_task(
    connection: sqlite3.Connection,
    change: StateChange,
    *,
    principal: str,
) -> int:
    """Commit one task transition, its event and outbox row atomically.

    The caller is the trusted daemon; untrusted agents must not invoke this
    repository directly. Requires an autocommit SQLite connection.
    """
    if connection.in_transaction:
        raise RuntimeError("Nested transaction is not supported")
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = write_task_transition(connection, change, principal=principal)
        connection.execute("COMMIT")
        return version
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def write_task_transition(
    connection: sqlite3.Connection,
    change: StateChange,
    *,
    principal: str,
) -> int:
    """Write a transition inside a daemon-owned command transaction.

    The transaction owner must propagate failures and roll back the command.
    """
    if change.before == State.VERIFYING and change.after == State.COMPLETED:
        raise ValueError("Task completion requires verified evidence")
    if change.before == State.AWAITING_APPROVAL and change.after == State.COMPLETED:
        raise ValueError("Task completion requires candidate-bound approval execution")
    if (change.before, change.after) in GATE_ONLY_TRANSITIONS:
        raise ValueError("Approval gate transitions require task_approval_gate")
    return _write_task_transition(connection, change, principal=principal)


def write_verified_task_transition(connection: sqlite3.Connection, change: StateChange, *, principal: str, verification_id: str, artifact_id: str) -> int:
    """Complete a task only for approved evidence tied to its latest attempt."""
    if not connection.in_transaction:
        raise RuntimeError("Task transition requires a command transaction")
    validate_id(principal)
    validate_id(verification_id)
    validate_id(artifact_id)
    if change.before != State.VERIFYING or change.after != State.COMPLETED:
        raise ValueError("Verified transition must complete a verifying task")
    row = connection.execute(
        "SELECT t.run_id,a.id,a.state,v.candidate_sha256,v.verifier_id,v.outcome,b.sha256,b.provenance_json "
        "FROM tasks t JOIN runs r ON r.id=t.run_id "
        "JOIN task_attempts a ON a.task_id=t.id AND a.attempt_no=t.attempt_count "
        "JOIN verification_results v ON v.task_id=t.id AND v.run_id=t.run_id AND v.id=? AND v.task_attempt_id=a.id "
        "JOIN artifacts b ON b.id=? AND b.id=v.candidate_artifact_id WHERE t.id=? AND r.state='running'",
        (verification_id, artifact_id, change.aggregate_id),
    ).fetchone()
    if row is None:
        raise StateConflict("Matching verification evidence is required")
    try:
        provenance = json.loads(row[7])
    except (ValueError, TypeError) as exc:
        raise StateConflict("Candidate provenance is invalid") from exc
    if (row[2] != "completed" or row[3] != row[6] or row[4] != principal or row[5] != "approved"
            or not isinstance(provenance, dict) or provenance.get("run_id") != row[0]
            or provenance.get("task_id") != change.aggregate_id or provenance.get("attempt_id") != row[1]):
        raise StateConflict("Matching verification evidence is required")
    return _write_task_transition(connection, change, principal=principal)


def _write_task_transition(connection: sqlite3.Connection, change: StateChange, *, principal: str) -> int:
    if not connection.in_transaction:
        raise RuntimeError("Task transition requires a command transaction")
    if not isinstance(principal, str) or not principal.strip():
        raise ValueError("principal required")
    validate_transition(change.before, change.after)
    if change.before == State.READY and change.after == State.ASSIGNED:
        raise ValueError("Task assignment requires claim_task")
    now = datetime.now(timezone.utc).isoformat()
    event_id = str(uuid4())
    row = connection.execute(
        "SELECT run_id, state, state_version FROM tasks WHERE id=?",
        (change.aggregate_id,),
    ).fetchone()
    if row is None or row[1] != change.before.value or row[2] != change.expected_version:
        raise StateConflict("Task state/version mismatch")
    if change.before == State.ASSIGNED and change.after == State.RUNNING:
        # An unresolved or superseded attempt must never be used as proof of a
        # fresh start. Reservation and this transition share the write lock.
        attempt = connection.execute(
            "SELECT a.id FROM task_attempts a JOIN tasks t ON t.id=a.task_id "
            "JOIN runs r ON r.id=t.run_id "
            "WHERE t.id=? AND r.state='running' AND a.attempt_no=t.attempt_count "
            "AND a.profile_id=t.assigned_profile_id AND a.state='created' "
            "AND 1=(SELECT COUNT(*) FROM task_attempts other WHERE other.task_id=t.id "
            "AND other.state NOT IN ('completed','failed','cancelled'))",
            (change.aggregate_id,),
        ).fetchone()
        if attempt is None:
            raise StateConflict("Task requires a running run and current reserved attempt")
    updated = connection.execute(
        "UPDATE tasks SET state=?, state_version=state_version+1, updated_at=? "
        "WHERE id=? AND state=? AND state_version=?",
        (change.after.value, now, change.aggregate_id, change.before.value, change.expected_version),
    )
    if updated.rowcount != 1:
        raise StateConflict("Task state/version mismatch")
    connection.execute(
        "INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (event_id, "task", change.aggregate_id, row[0], "task.state_changed", 1,
         json.dumps({"before": change.before.value, "after": change.after.value,
                     "principal": principal, "version": change.expected_version + 1}), now),
    )
    connection.execute("INSERT INTO outbox(event_id) VALUES(?)", (event_id,))
    return change.expected_version + 1
