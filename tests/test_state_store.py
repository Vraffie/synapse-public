import sqlite3

import pytest

from synapse.domain.workflow import State, plan_transition
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import StateConflict, transition_task
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "synapse.db")
    apply_migrations(conn, ROOT / "migrations")
    conn.execute(
        "INSERT INTO workflow_versions(workflow_id,version,definition_json,definition_sha256,published_at) "
        "VALUES('wf','1','{}','abc','2026-01-01T00:00:00Z')"
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


def test_atomic_transition_emits_one_event_and_outbox(db):
    change = plan_transition("task", State.CREATED, State.READY, 0)
    assert transition_task(db, change, principal="daemon") == 1
    assert db.execute("SELECT state,state_version FROM tasks WHERE id='task'").fetchone() == ("ready", 1)
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_stale_version_rejected_without_duplicate_events(db):
    change = plan_transition("task", State.CREATED, State.READY, 0)
    transition_task(db, change, principal="daemon")
    with pytest.raises(StateConflict):
        transition_task(db, change, principal="daemon")
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_outbox_insert_failure_rolls_back_task_and_event(db):
    db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'forced failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        transition_task(db, plan_transition("task", State.CREATED, State.READY, 0), principal="daemon")
    assert db.execute("SELECT state,state_version FROM tasks WHERE id='task'").fetchone() == ("created", 0)
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_idempotent_transition_commits_state_event_outbox_and_response(db):
    from synapse.persistence.idempotency import execute_idempotent
    from synapse.persistence.state_store import write_task_transition

    change = plan_transition("task", State.CREATED, State.READY, 0)

    def command(connection):
        return {"version": write_task_transition(connection, change, principal="daemon")}

    def execute():
        return execute_idempotent(
            db, principal="daemon", operation="transition", key="command-1",
            request={"task": "task", "target": "ready", "version": 0}, execute=command,
        )

    db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'forced failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        execute()
    assert db.execute("SELECT state,state_version FROM tasks").fetchone() == ("created", 0)
    for table in ("events", "outbox", "idempotency_keys"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    db.execute("DROP TRIGGER fail_outbox")

    assert execute().replayed is False
    replay = execute()
    assert replay.replayed and replay.response == {"version": 1}
    assert db.execute("SELECT state,state_version FROM tasks").fetchone() == ("ready", 1)
    for table in ("events", "outbox", "idempotency_keys"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_concurrent_transitions_only_commit_one_winner(db):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import closing
    from threading import Barrier

    path = db.execute("PRAGMA database_list").fetchone()[2]
    change = plan_transition("task", State.CREATED, State.READY, 0)
    ready = Barrier(2)

    def transition():
        with closing(open_database(path)) as connection:
            ready.wait(timeout=5)
            try:
                return transition_task(connection, change, principal="daemon")
            except StateConflict:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: transition(), range(2)))
    assert results.count(1) == results.count(None) == 1
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
