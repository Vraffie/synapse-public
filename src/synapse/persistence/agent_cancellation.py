"""Durable cancellation intent and honest finalization for real-agent runs.

Unlike the mock path, a real process's outcome is never "discarded": finalizing
refuses while any attempt is unresolved (running or unknown), so a run whose
process could not be shown to have stopped stays out of ``cancelled``.
"""
from __future__ import annotations

import sqlite3

from synapse.domain.contracts import EventEnvelope, RunId, encode_payload, validate_id
from synapse.domain.runs import RunChange, RunState
from synapse.domain.workflow import State, StateChange
from synapse.persistence.agent_tasks import is_agent_run
from synapse.persistence.idempotency import execute_idempotent
from synapse.persistence.run_store import write_run_transition
from synapse.persistence.state_store import StateConflict, write_task_transition

EVENT_TYPE = "agent.cancellation_requested"
_PRINCIPAL = "agent-cancellation"


def cancellation_requested(db: sqlite3.Connection, run_id: str) -> bool:
    return db.execute("SELECT 1 FROM events WHERE run_id=? AND type=? LIMIT 1", (run_id, EVENT_TYPE)).fetchone() is not None


def request_agent_cancellation(db: sqlite3.Connection, *, run_id: str, key: str):
    validate_id(run_id)
    validate_id(key)

    def write(conn):
        row = conn.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if row != ("running",) or not is_agent_run(conn, run_id):
            raise StateConflict("Only a running real-agent run can be cancelled here")
        if cancellation_requested(conn, run_id):
            raise StateConflict("Agent cancellation already requested")
        event = EventEnvelope.create(aggregate_type="run", aggregate_id=run_id, event_type=EVENT_TYPE,
                                     payload={"run_id": run_id, "principal": "local-operator"})
        conn.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                     (event.id, event.aggregate_type, run_id, run_id, event.event_type, event.schema_version,
                      encode_payload(event.payload), event.occurred_at))
        conn.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
        return {"run_id": run_id, "state": "cancellation_requested"}
    return execute_idempotent(db, principal="local-operator", operation="agent.cancel", key=key,
                              request={"run_id": run_id}, execute=write)


def finalize_agent_cancellation(db: sqlite3.Connection, *, run_id: str) -> None:
    """Cancel every open task and the run, only when no attempt is unresolved.

    Raises StateConflict (leaving everything unchanged) if any attempt is
    created/running/unknown: the caller must route such a run to recovery.
    """
    validate_id(run_id)
    if db.in_transaction:
        raise RuntimeError("Cancellation finalization requires an autocommit connection")
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT state,state_version FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None or not is_agent_run(db, run_id) or not cancellation_requested(db, run_id):
            raise StateConflict("Agent cancellation intent is missing")
        if row[0] == "cancelled":
            db.execute("COMMIT")
            return
        if row[0] not in ("running", "recovering"):
            raise StateConflict("Agent run cannot be cancelled")
        if db.execute("SELECT 1 FROM task_attempts a JOIN tasks t ON t.id=a.task_id WHERE t.run_id=? "
                      "AND a.state NOT IN ('completed','failed','cancelled') LIMIT 1", (run_id,)).fetchone():
            raise StateConflict("An agent attempt has no established outcome")
        for task_id, state, version in db.execute("SELECT id,state,state_version FROM tasks WHERE run_id=? ORDER BY id", (run_id,)).fetchall():
            state = State(state)
            if state in (State.COMPLETED, State.FAILED, State.CANCELLED):
                continue
            if state == State.VERIFYING:
                version = write_task_transition(db, StateChange(task_id, state, State.BLOCKED, version), principal=_PRINCIPAL)
                state = State.BLOCKED
            write_task_transition(db, StateChange(task_id, state, State.CANCELLED, version), principal=_PRINCIPAL)
        write_run_transition(db, RunChange(RunId(run_id), RunState(row[0]), RunState.CANCELLED, row[1]), principal=_PRINCIPAL)
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
