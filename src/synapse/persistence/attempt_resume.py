"""Startup re-queue of agent attempts that were PROVABLY never spawned (ADR 024).

An attempt is provably never spawned when it has no row in ``attempt_spawn_marks``. The agent engine commits that
row (``mark_spawn_begun``) immediately before it hands the attempt to the provider, so before the spawn journal
``begin`` and before any process can exist; migration 0017 marked every attempt that existed before it. Everything
with a mark MAY have run and is left to today's recovery (``unknown``, task blocked, run ``recovering``, operator
``agent-abandon``). This module never decides anything about a marked attempt.

``requeue_never_spawned`` is all-or-nothing per run: it re-queues only when EVERY piece of unresolved work in the run
is a never-spawned attempt, the run is still ``running`` with no cancellation request, each task stays within the
engine's attempt budget, and the per-run resume cap holds. Otherwise it changes nothing and returns ``None``. A
re-queued attempt is closed as ``cancelled`` (its id is never reused) and its task goes back to ``ready``, so the
next attempt passes every normal check again (claim, per-profile concurrency, budget, profile, approval, HEAD).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from synapse.domain.contracts import EventEnvelope, encode_payload, validate_id
from synapse.domain.workflow import State, StateChange
from synapse.persistence.mock_execution_store import set_attempt
from synapse.persistence.state_store import StateConflict, write_task_transition

EVENT_TYPE = "agent.attempt_resumed"
MAX_RESUMES_PER_RUN = 3
# Task states that hold no in-flight work: a run made only of these (plus re-queued attempts) may be re-driven.
_IDLE = frozenset({State.COMPLETED, State.CREATED, State.WAITING_DEPENDENCIES, State.READY, State.AWAITING_APPROVAL})
_TERMINAL_ATTEMPT = ("completed", "failed", "cancelled")


def mark_spawn_begun(connection: sqlite3.Connection, *, attempt_id: str) -> None:
    """Record, inside the caller's transaction, that a spawn may begin for ``attempt_id`` from now on."""
    if not connection.in_transaction:
        raise RuntimeError("Spawn mark requires a transaction")
    validate_id(attempt_id)
    connection.execute("INSERT INTO attempt_spawn_marks(attempt_id,marked_at) VALUES(?,?)",
                       (attempt_id, datetime.now(timezone.utc).isoformat()))


def resumed_count(connection: sqlite3.Connection, run_id: str) -> int:
    return connection.execute("SELECT count(*) FROM events WHERE run_id=? AND type=?", (run_id, EVENT_TYPE)).fetchone()[0]


def requeue_never_spawned(connection: sqlite3.Connection, *, run_id: str, principal: str, max_attempts: int,
                          cap: int = MAX_RESUMES_PER_RUN) -> tuple[str, ...] | None:
    """Re-queue the run's never-spawned attempts, or return ``None`` and change nothing.

    Returns the re-queued attempt ids (possibly empty: the run holds no in-flight work at all and may simply be
    re-driven). The caller must hold the daemon ownership lock and must already have established that the startup
    fence found nothing unverified.
    """
    validate_id(run_id)
    validate_id(principal)
    if connection.in_transaction:
        raise RuntimeError("Re-queue requires an autocommit connection")
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = _plan_and_apply(connection, run_id, principal, max_attempts, cap)
        if result is None:
            connection.execute("ROLLBACK")
        else:
            connection.execute("COMMIT")
        return result
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _plan_and_apply(db: sqlite3.Connection, run_id: str, principal: str, max_attempts: int,
                    cap: int) -> tuple[str, ...] | None:
    run = db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None or run[0] != "running":
        return None  # recovering (incl. anything awaiting agent-abandon), terminal, paused: never re-driven
    if db.execute("SELECT 1 FROM events WHERE run_id=? AND type IN ('agent.cancellation_requested','agent.run_abandoned') "
                  "LIMIT 1", (run_id,)).fetchone():
        return None
    candidates = []
    for task_id, state, version, count in db.execute(
            "SELECT id,state,state_version,attempt_count FROM tasks WHERE run_id=? ORDER BY id", (run_id,)).fetchall():
        open_attempts = db.execute(
            "SELECT a.id,a.state,(SELECT 1 FROM attempt_spawn_marks m WHERE m.attempt_id=a.id) FROM task_attempts a "
            "WHERE a.task_id=? AND a.state NOT IN (?,?,?)", (task_id, *_TERMINAL_ATTEMPT)).fetchall()
        if state in _IDLE and not open_attempts:
            continue
        if len(open_attempts) != 1:
            return None
        attempt_id, attempt_state, marked = open_attempts[0]
        if marked is not None:
            return None  # may have run: today's recovery, unchanged
        never_spawned = (state == State.RUNNING and attempt_state == "running") or \
                        (state == State.ASSIGNED and attempt_state == "created")
        if not never_spawned:
            return None
        if count >= max_attempts:
            return None  # the next attempt would exceed the engine's attempt budget
        candidates.append((task_id, State(state), version, attempt_id, attempt_state))
    if candidates and resumed_count(db, run_id) + len(candidates) > cap:
        return None
    for task_id, state, version, attempt_id, attempt_state in candidates:
        if attempt_state == "running":
            set_attempt(db, attempt_id=attempt_id, before="running", after="cancelled", run_id=run_id, task_id=task_id)
            version = write_task_transition(db, StateChange(task_id, State.RUNNING, State.RETRY_WAIT, version), principal=principal)
            write_task_transition(db, StateChange(task_id, State.RETRY_WAIT, State.READY, version), principal=principal)
        else:
            _cancel_created(db, run_id, task_id, attempt_id)
            write_task_transition(db, StateChange(task_id, State.ASSIGNED, State.READY, version), principal=principal)
        event = EventEnvelope.create(aggregate_type="task_attempt", aggregate_id=attempt_id, event_type=EVENT_TYPE,
                                     payload={"run_id": run_id, "task_id": task_id, "attempt_id": attempt_id,
                                              "principal": principal})
        db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
                   "VALUES(?,?,?,?,?,?,?,?)", (event.id, event.aggregate_type, attempt_id, run_id, event.event_type,
                                               event.schema_version, encode_payload(event.payload), event.occurred_at))
        db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
    return tuple(c[3] for c in candidates)


def _cancel_created(db: sqlite3.Connection, run_id: str, task_id: str, attempt_id: str) -> None:
    """A reserved attempt that never reached ``running`` (not produced by today's engine, handled for safety)."""
    event = EventEnvelope.create(aggregate_type="task_attempt", aggregate_id=attempt_id,
                                 event_type="task_attempt.state_changed",
                                 payload={"before": "created", "after": "cancelled", "task_id": task_id})
    cursor = db.execute("UPDATE task_attempts SET state='cancelled' WHERE id=? AND task_id=? AND state='created'",
                        (attempt_id, task_id))
    if cursor.rowcount != 1:
        raise StateConflict("Attempt state mismatch")
    db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
               "VALUES(?,?,?,?,?,?,?,?)", (event.id, event.aggregate_type, attempt_id, run_id, event.event_type,
                                           event.schema_version, encode_payload(event.payload), event.occurred_at))
    db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
