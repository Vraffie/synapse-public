"""Daemon-owned reservation of durable task attempts."""
import sqlite3

from synapse.domain.contracts import EventEnvelope, encode_payload, validate_id
from synapse.domain.workflow import State
from synapse.persistence.state_store import StateConflict


def reserve_attempt(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    attempt_id: str,
    expected_task_version: int,
    principal: str,
) -> int:
    """Reserve one attempt inside the caller's transaction.

    Reservation does not start a provider or certify task completion. The
    application must authorize the command and reconcile external execution.
    """
    if not connection.in_transaction:
        raise RuntimeError("Attempt reservation requires a command transaction")
    for value in (task_id, attempt_id, principal):
        validate_id(value)
    if type(expected_task_version) is not int or expected_task_version < 0:
        raise ValueError("expected_task_version must be a nonnegative integer")
    row = connection.execute(
        "SELECT run_id, state, state_version, attempt_count, assigned_profile_id "
        "FROM tasks WHERE id=?", (task_id,),
    ).fetchone()
    if row is None or row[1] != State.ASSIGNED or row[2] != expected_task_version:
        raise StateConflict("Task state/version mismatch")
    if row[4] is None:
        raise ValueError("Task must have an assigned profile")
    if connection.execute(
        "SELECT 1 FROM task_attempts WHERE task_id=? AND state NOT IN ('completed','failed','cancelled')",
        (task_id,),
    ).fetchone():
        raise StateConflict("Task already has an active or unresolved attempt")

    attempt_number = row[3] + 1
    event = EventEnvelope.create(
        aggregate_type="task", aggregate_id=task_id, event_type="task.attempt_reserved",
        payload={"attempt_id": attempt_id, "attempt_number": attempt_number,
                 "version": expected_task_version + 1, "principal": principal},
    )
    updated = connection.execute(
        "UPDATE tasks SET attempt_count=?,state_version=state_version+1,updated_at=? "
        "WHERE id=? AND state=? AND state_version=?",
        (attempt_number, event.occurred_at, task_id, State.ASSIGNED, expected_task_version),
    )
    if updated.rowcount != 1:
        raise StateConflict("Task state/version mismatch")
    connection.execute(
        "INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES(?,?,?,?,?)",
        (attempt_id, task_id, attempt_number, row[4], "created"),
    )
    connection.execute(
        "INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (event.id, event.aggregate_type, task_id, row[0], event.event_type,
         event.schema_version, encode_payload(event.payload), event.occurred_at),
    )
    connection.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
    return attempt_number


def mark_interrupted_attempts_unknown(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    principal: str,
) -> tuple[str, ...]:
    """Mark unreconciled attempts unknown during exclusive daemon startup.

    The caller must stop or fence prior workers before invoking this function.
    A later reconciliation service decides whether an external effect occurred.
    """
    if not connection.in_transaction:
        raise RuntimeError("Attempt recovery requires a command transaction")
    validate_id(run_id)
    validate_id(principal)
    if connection.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
        raise ValueError("Run does not exist")
    rows = connection.execute(
        "SELECT a.id,a.task_id,a.state FROM task_attempts a "
        "JOIN tasks t ON t.id=a.task_id WHERE t.run_id=? "
        "AND a.state IN ('created','starting','running') ORDER BY a.id",
        (run_id,),
    ).fetchall()
    changed = []
    for attempt_id, task_id, state in rows:
        event = EventEnvelope.create(
            aggregate_type="task_attempt", aggregate_id=attempt_id,
            event_type="task_attempt.unknown",
            payload={"task_id": task_id, "before": state, "after": "unknown",
                     "principal": principal},
        )
        updated = connection.execute(
            "UPDATE task_attempts SET state='unknown' WHERE id=? AND state=?",
            (attempt_id, state),
        )
        if updated.rowcount != 1:
            raise StateConflict("Attempt state changed during recovery")
        connection.execute(
            "INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (event.id, event.aggregate_type, attempt_id, run_id, event.event_type,
             event.schema_version, encode_payload(event.payload), event.occurred_at),
        )
        connection.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
        changed.append(attempt_id)
    return tuple(changed)
