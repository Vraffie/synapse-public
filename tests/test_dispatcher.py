"""Dispatcher contract tests using the actual versioned schema."""
from contextlib import closing
from pathlib import Path

import sqlite3

import pytest

from synapse.domain.workflow import State, plan_transition
from synapse.persistence.dispatcher import dispatch_batch
from synapse.persistence.event_store import pending_outbox
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import transition_task

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "dispatch.db")
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
    transition_task(conn, plan_transition("task", State.CREATED, State.READY, 0),
                    principal="daemon")
    yield conn
    conn.close()


def test_success_is_delivered_once(db):
    seen = []
    result = dispatch_batch(db, seen.append)
    assert (result.delivered, result.retried, result.missing) == (1, 0, 0)
    assert seen[0]["type"] == "task.state_changed"
    assert seen[0]["payload"]["after"] == "ready"
    assert dispatch_batch(db, seen.append).delivered == 0
    assert len(seen) == 1


def test_failed_publish_is_scheduled_for_retry(db):
    def fail(_event):
        raise ConnectionError("subscriber unavailable")
    result = dispatch_batch(db, fail, retry_seconds=60)
    assert (result.delivered, result.retried) == (0, 1)
    assert pending_outbox(db) == []
    assert db.execute("SELECT attempts FROM outbox").fetchone()[0] == 1


def test_crash_after_publish_before_ack_causes_redelivery(db):
    class SimulatedCrash(BaseException):
        pass

    seen = []
    event_id = pending_outbox(db)[0]

    def publish_before_crash(event):
        seen.append(event["id"])
        raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        dispatch_batch(db, publish_before_crash)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    with closing(open_database(path)) as reopened:
        result = dispatch_batch(reopened, lambda event: seen.append(event["id"]))
        assert result.delivered == 1
        assert pending_outbox(reopened) == []
    assert seen == [event_id, event_id]


def test_invalid_batch_limit_does_not_dispatch(db):
    with pytest.raises(ValueError):
        dispatch_batch(db, lambda _: None, limit=0)
    assert len(pending_outbox(db)) == 1


def test_no_external_call_while_transaction_active(db):
    db.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError):
            dispatch_batch(db, lambda _: None)
    finally:
        db.execute("ROLLBACK")


def test_dispatch_rejects_implicit_write_transactions(db):
    path = db.execute("PRAGMA database_list").fetchone()[2]
    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(ValueError, match="autocommit"):
            dispatch_batch(connection, lambda _: pytest.fail("unexpected delivery"))
    assert len(pending_outbox(db)) == 1


def test_missing_event_is_reported_without_acknowledging(db):
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("INSERT INTO outbox(event_id) VALUES('missing-event')")
    db.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(RuntimeError, match="Outbox references missing event: missing-event"):
        dispatch_batch(db, lambda event: pytest.fail("unexpected publication"))
    assert db.execute(
        "SELECT dispatched_at, attempts FROM outbox WHERE event_id='missing-event'"
    ).fetchone() == (None, 0)
