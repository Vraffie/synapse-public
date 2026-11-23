"""Atomic daemon-owned claims for eligible task execution."""
import sqlite3
from collections.abc import Callable

from synapse.domain.contracts import EventEnvelope, encode_payload, validate_id
from synapse.domain.workflow import State
from synapse.persistence.state_store import StateConflict


def claim_task(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    expected_version: int,
    profile_id: str,
    principal: str,
    max_running: int = 6,
    budget_check: Callable[[sqlite3.Connection, str, str], None] | None = None,
) -> int:
    """Claim an eligible task in the caller's write transaction.

    Start the caller-owned transaction with BEGIN IMMEDIATE. The application
    must authorize the command and evaluate profile health, policy, resource
    locks and budgets. Exceptions must roll back the entire transaction.

    ``budget_check(connection, task_id, profile_id)``, if given, runs after the capacity checks (a capacity
    refusal is never confused with a budget refusal) and before the claim UPDATE, in this transaction; it raises
    to refuse. Callers that pass none are unbudgeted (ADR 020 D1).
    """
    if not connection.in_transaction:
        raise RuntimeError("Task claim requires a command transaction")
    for value in (task_id, profile_id, principal):
        validate_id(value)
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("expected_version must be a nonnegative integer")
    if type(max_running) is not int or not 1 <= max_running <= 6:
        raise ValueError("max_running must be between 1 and 6")

    task = connection.execute(
        "SELECT t.run_id,t.state,t.state_version,r.state "
        "FROM tasks t JOIN runs r ON r.id=t.run_id WHERE t.id=?", (task_id,)
    ).fetchone()
    if task is None or task[1] != State.READY or task[2] != expected_version:
        raise StateConflict("Task state/version mismatch")
    if task[3] != "running":
        raise StateConflict("Run is not running")
    unresolved = connection.execute(
        "SELECT 1 FROM task_attempts WHERE task_id=? "
        "AND state NOT IN ('completed','failed','cancelled') LIMIT 1", (task_id,)
    ).fetchone()
    if unresolved:
        raise StateConflict("Task has an unresolved attempt")
    incomplete = connection.execute(
        "SELECT 1 FROM task_dependencies d LEFT JOIN tasks dep ON dep.id=d.depends_on_id "
        "WHERE d.task_id=? AND (dep.id IS NULL OR dep.run_id<>? OR dep.state<>?) LIMIT 1",
        (task_id, task[0], State.COMPLETED),
    ).fetchone()
    if incomplete:
        raise StateConflict("Task dependencies are incomplete or outside the run")
    profile = connection.execute(
        "SELECT enabled,max_concurrency FROM profiles WHERE id=?", (profile_id,)
    ).fetchone()
    if profile is None or profile[0] != 1 or profile[1] < 1:
        raise StateConflict("Profile is not enabled or has no capacity")
    # Count unresolved attempts even if their task has left the running state,
    # but not those of finished runs (abandon leaves attempts 'unknown'; ADR 016 W0).
    # Assigned/running tasks without an attempt still reserve a worker slot.
    # An unresolved attempt with no profile cannot be safely assigned capacity.
    active = connection.execute(
        "WITH occupied(profile_id) AS ("
        " SELECT a.profile_id FROM task_attempts a "
        " JOIN tasks att ON att.id=a.task_id JOIN runs ar ON ar.id=att.run_id "
        " WHERE a.state NOT IN ('completed','failed','cancelled')"
        " AND ar.state NOT IN ('failed','cancelled','succeeded')"
        " UNION ALL "
        " SELECT t.assigned_profile_id FROM tasks t JOIN runs r ON r.id=t.run_id "
        " WHERE t.state IN ('assigned','running') "
        " AND r.state NOT IN ('failed','cancelled','succeeded') AND NOT EXISTS ("
        "  SELECT 1 FROM task_attempts a WHERE a.task_id=t.id "
        "  AND a.state NOT IN ('completed','failed','cancelled'))"
        ") SELECT COUNT(*),"
        " COALESCE(SUM(CASE WHEN profile_id=? THEN 1 ELSE 0 END),0),"
        " COALESCE(SUM(CASE WHEN profile_id IS NULL THEN 1 ELSE 0 END),0) "
        "FROM occupied", (profile_id,)
    ).fetchone()
    if active[2]:
        raise StateConflict("An occupied worker has an unknown profile")
    if active[0] >= max_running or active[1] >= profile[1]:
        raise StateConflict("Worker or profile capacity is exhausted")
    if budget_check is not None:
        budget_check(connection, task_id, profile_id)

    event = EventEnvelope.create(
        aggregate_type="task", aggregate_id=task_id, event_type="task.state_changed",
        payload={"before": "ready", "after": "assigned", "profile_id": profile_id,
                 "version": expected_version + 1, "principal": principal},
    )
    updated = connection.execute(
        "UPDATE tasks SET state=?,state_version=state_version+1,assigned_profile_id=?,updated_at=? "
        "WHERE id=? AND state=? AND state_version=?",
        (State.ASSIGNED, profile_id, event.occurred_at, task_id, State.READY, expected_version),
    )
    if updated.rowcount != 1:
        raise StateConflict("Task state/version mismatch")
    connection.execute(
        "INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (event.id, event.aggregate_type, task_id, task[0], event.event_type,
         event.schema_version, encode_payload(event.payload), event.occurred_at),
    )
    connection.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
    return expected_version + 1
