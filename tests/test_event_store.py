from pathlib import Path

import pytest

from synapse.domain.workflow import State, plan_transition
from synapse.persistence.event_store import pending_outbox, read_events, record_dispatch_result
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import transition_task

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "events.db")
    apply_migrations(conn, ROOT / "migrations")
    conn.execute(
        "INSERT INTO workflow_versions VALUES('wf','1','{}','hash','2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO runs(id,workflow_id,workflow_version,state,created_at,updated_at) "
        "VALUES('run','wf','1','created','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) "
        "VALUES('task','run','node','created','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
    )
    yield conn
    conn.close()


def test_event_cursor_and_outbox_ack(db):
    transition_task(db, plan_transition("task", State.CREATED, State.READY, 0), principal="daemon")
    transition_task(db, plan_transition("task", State.READY, State.BLOCKED, 1), principal="daemon")
    first = read_events(db, limit=1)
    second = read_events(db, after_sequence=first[0]["sequence"])
    assert len(first) == len(second) == 1
    assert second[0]["sequence"] > first[0]["sequence"]
    assert read_events(db, after_sequence=second[0]["sequence"]) == []
    ids = pending_outbox(db)
    assert len(ids) == 2
    assert record_dispatch_result(db, ids[0], success=True)
    assert not record_dispatch_result(db, ids[0], success=True)
    assert pending_outbox(db) == ids[1:]


def test_outbox_retry_not_immediately_eligible(db):
    transition_task(db, plan_transition("task", State.CREATED, State.READY, 0), principal="daemon")
    event_id = pending_outbox(db)[0]
    assert record_dispatch_result(db, event_id, success=False, retry_seconds=60)
    assert pending_outbox(db) == []
    assert db.execute("SELECT attempts FROM outbox WHERE event_id=?", (event_id,)).fetchone()[0] == 1


def test_invalid_replay_limits(db):
    with pytest.raises(ValueError):
        read_events(db, after_sequence=-1)
    with pytest.raises(ValueError):
        pending_outbox(db, limit=0)


def test_event_log_rejects_updates_and_deletes(db):
    import sqlite3

    transition_task(db, plan_transition("task", State.CREATED, State.READY, 0), principal="daemon")
    event_id = read_events(db)[0]["id"]
    with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
        db.execute("UPDATE events SET payload_json='{}' WHERE id=?", (event_id,))
    with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
        db.execute("DELETE FROM events WHERE id=?", (event_id,))
    with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
        db.execute(
            "INSERT OR REPLACE INTO events "
            "(id,aggregate_type,aggregate_id,type,schema_version,payload_json,occurred_at) "
            "VALUES (?, 'task', 'task', 'replacement', 1, '{}', 'now')", (event_id,)
        )
    assert read_events(db)[0]["id"] == event_id
    assert record_dispatch_result(db, event_id, success=True)
