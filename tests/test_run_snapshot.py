from contextlib import closing
from pathlib import Path

import pytest

from synapse.domain.workflow import State, plan_transition
from synapse.persistence.event_store import read_events
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.run_snapshot import read_run_snapshot
from synapse.persistence.state_store import transition_task


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "snapshot.db"
    with closing(open_database(path)) as conn:
        apply_migrations(conn, Path(__file__).resolve().parents[1] / "migrations")
        conn.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        conn.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        conn.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES('task','run','node','created','now','now')")
        yield conn, path


def test_snapshot_contains_state_and_replay_cursor(db):
    conn, _ = db
    transition_task(conn, plan_transition("task", State.CREATED, State.READY, 0), principal="daemon")
    snapshot = read_run_snapshot(conn, "run")
    assert snapshot["run"]["state"] == "created"
    assert snapshot["tasks"][0]["state"] == "ready"
    assert snapshot["last_sequence"] == read_events(conn)[-1]["sequence"]
    assert read_events(conn, after_sequence=snapshot["last_sequence"], run_id="run") == []


def test_concurrent_commit_is_replayed_after_consistent_snapshot(db):
    conn, path = db
    with closing(open_database(path)) as writer:
        did_write = False

        def trace(sql):
            nonlocal did_write
            if not did_write and sql.startswith("SELECT id,node_id"):
                did_write = True
                transition_task(writer, plan_transition("task", State.CREATED, State.READY, 0), principal="daemon")

        conn.set_trace_callback(trace)
        try:
            snapshot = read_run_snapshot(conn, "run")
        finally:
            conn.set_trace_callback(None)
        assert did_write
        assert snapshot["tasks"][0]["state"] == "created"
        assert snapshot["last_sequence"] == 0
        events = read_events(conn, after_sequence=snapshot["last_sequence"], run_id="run")
        assert len(events) == 1 and events[0]["payload"]["after"] == "ready"


def test_missing_run_and_nested_transaction_are_rejected(db):
    conn, _ = db
    with pytest.raises(ValueError, match="does not exist"):
        read_run_snapshot(conn, "missing")
    assert not conn.in_transaction
    conn.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError, match="own read transaction"):
            read_run_snapshot(conn, "run")
        assert conn.in_transaction
    finally:
        conn.execute("ROLLBACK")


def test_snapshot_rejects_cross_run_dependency_without_leaking_id(db):
    conn, _ = db
    conn.execute("INSERT INTO runs VALUES('other','wf','1','created',0,'now','now')")
    conn.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES('foreign','other','node','created','now','now')")
    conn.execute("INSERT INTO task_dependencies VALUES('task','foreign')")
    with pytest.raises(RuntimeError, match="cross-run"):
        read_run_snapshot(conn, "run")
    assert not conn.in_transaction


def test_snapshot_includes_immutable_mock_fixture_and_profile(db):
    conn, _ = db
    assert read_run_snapshot(conn, 'run')['mock_config'] is None
    conn.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) VALUES('mock','mock','Mock','fixture','now')")
    conn.execute("INSERT INTO mock_run_config VALUES('run','mock','fixed-v1')")
    assert read_run_snapshot(conn, 'run')['mock_config'] == {'fixture': 'fixed-v1', 'profile_id': 'mock', 'provider': 'mock'}
