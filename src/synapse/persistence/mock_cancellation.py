"""Durable cancellation intents and stopped-worker reconciliation for mocks."""
import sqlite3

from synapse.domain.contracts import EventEnvelope, RunId, encode_payload, validate_id
from synapse.domain.runs import RunChange, RunState
from synapse.domain.workflow import State, StateChange
from synapse.persistence.idempotency import execute_idempotent
from synapse.persistence.mock_scenarios import mock_kind
from synapse.persistence.run_store import write_run_transition
from synapse.persistence.state_store import StateConflict, write_task_transition


def request_mock_cancellation(connection: sqlite3.Connection, *, run_id: str, key: str):
    validate_id(run_id)
    validate_id(key)
    def write(db):
        row = db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if row != ('running',) or mock_kind(db, run_id) is None:
            raise StateConflict('Only a running built-in mock run can be cancelled')
        if db.execute('SELECT 1 FROM mock_cancellation_requests WHERE run_id=?', (run_id,)).fetchone():
            raise StateConflict('Mock cancellation already requested')
        event = EventEnvelope.create(aggregate_type='run', aggregate_id=run_id,
            event_type='mock.cancellation_requested', payload={'run_id': run_id, 'principal': 'local-operator'})
        db.execute('INSERT INTO mock_cancellation_requests VALUES(?,?)', (run_id, event.occurred_at))
        db.execute('INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)',
            (event.id, event.aggregate_type, run_id, run_id, event.event_type, event.schema_version, encode_payload(event.payload), event.occurred_at))
        db.execute('INSERT INTO outbox(event_id) VALUES(?)', (event.id,))
        return {'run_id': run_id, 'state': 'cancellation_requested'}
    return execute_idempotent(connection, principal='local-operator', operation='mock.cancel',
                              key=key, request={'run_id': run_id}, execute=write)


def finalize_mock_cancellation(connection: sqlite3.Connection, *, run_id: str, no_workers_survive: bool):
    """Finalize only after all in-process fixed mock workers have stopped.

    The daemon caller must hold exclusive ownership and establish the worker
    stop before invoking this. Unknown synthetic outcomes are explicitly
    discarded; mock workers have no external side effects.
    """
    if no_workers_survive is not True:
        raise ValueError('Explicit stopped-worker assurance is required')
    validate_id(run_id)
    if connection.in_transaction:
        raise RuntimeError('Cancellation finalization requires an autocommit connection')
    connection.execute('BEGIN IMMEDIATE')
    try:
        row = connection.execute("SELECT r.state,r.state_version FROM runs r JOIN mock_run_config c ON c.run_id=r.id JOIN mock_cancellation_requests x ON x.run_id=r.id WHERE r.id=?", (run_id,)).fetchone()
        if row is None or mock_kind(connection, run_id) is None:
            raise StateConflict('Mock cancellation intent is missing')
        if row[0] == 'cancelled':
            unresolved = connection.execute(
                "SELECT 1 FROM tasks t LEFT JOIN task_attempts a ON a.task_id=t.id "
                "WHERE t.run_id=? AND (t.state NOT IN ('completed','failed','cancelled') "
                "OR (a.id IS NOT NULL AND a.state NOT IN ('completed','failed','cancelled'))) LIMIT 1", (run_id,),
            ).fetchone()
            if unresolved:
                raise StateConflict('Cancelled run contains unresolved work')
            connection.execute('COMMIT')
            return
        if row[0] not in ('running', 'recovering'):
            raise StateConflict('Mock run cannot be cancelled')
        foreign = connection.execute(
            "SELECT 1 FROM tasks t LEFT JOIN profiles tp ON tp.id=t.assigned_profile_id "
            "WHERE t.run_id=? AND ((t.assigned_profile_id IS NOT NULL "
            "AND (tp.provider IS NULL OR tp.provider<>'mock')) "
            "OR (t.state IN ('assigned','running','verifying') AND t.assigned_profile_id IS NULL)) UNION ALL "
            "SELECT 1 FROM task_attempts a JOIN tasks t ON t.id=a.task_id "
            "LEFT JOIN profiles ap ON ap.id=a.profile_id WHERE t.run_id=? "
            "AND (ap.provider IS NULL OR ap.provider<>'mock') LIMIT 1", (run_id, run_id),
        ).fetchone()
        if foreign:
            raise StateConflict('Cannot reconcile non-mock or inconsistent attempt')
        attempts = connection.execute("SELECT a.id,a.task_id,a.state,p.provider FROM task_attempts a JOIN tasks t ON t.id=a.task_id LEFT JOIN profiles p ON p.id=a.profile_id WHERE t.run_id=? AND a.state NOT IN ('completed','failed','cancelled') ORDER BY a.id", (run_id,)).fetchall()
        for attempt_id, task_id, state, provider in attempts:
            if provider != 'mock' or state not in ('created', 'starting', 'running', 'unknown'):
                raise StateConflict('Cannot reconcile non-mock or inconsistent attempt')
            event = EventEnvelope.create(aggregate_type='task_attempt', aggregate_id=attempt_id,
                event_type='task_attempt.mock_cancelled_after_stop',
                payload={'task_id': task_id, 'before': state, 'after': 'cancelled', 'outcome': 'discarded'})
            cursor = connection.execute("UPDATE task_attempts SET state='cancelled' WHERE id=? AND state=?", (attempt_id, state))
            if cursor.rowcount != 1:
                raise StateConflict('Attempt changed during cancellation')
            connection.execute('INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)',
                (event.id, event.aggregate_type, attempt_id, run_id, event.event_type, event.schema_version, encode_payload(event.payload), event.occurred_at))
            connection.execute('INSERT INTO outbox(event_id) VALUES(?)', (event.id,))
        tasks = connection.execute('SELECT id,state,state_version FROM tasks WHERE run_id=? ORDER BY id', (run_id,)).fetchall()
        for task_id, state, version in tasks:
            state = State(state)
            if state in (State.COMPLETED, State.FAILED, State.CANCELLED):
                continue
            if state == State.VERIFYING:
                version = write_task_transition(connection, StateChange(task_id, state, State.BLOCKED, version), principal='mock-cancellation')
                state = State.BLOCKED
            write_task_transition(connection, StateChange(task_id, state, State.CANCELLED, version), principal='mock-cancellation')
        write_run_transition(connection, RunChange(RunId(run_id), RunState(row[0]), RunState.CANCELLED, row[1]), principal='mock-cancellation')
        connection.execute('COMMIT')
    except BaseException:
        if connection.in_transaction:
            connection.execute('ROLLBACK')
        raise
