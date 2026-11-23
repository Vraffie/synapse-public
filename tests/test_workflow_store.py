from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from synapse.persistence.idempotency import IdempotencyConflict
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.workflow_store import create_run, publish_workflow

ROOT = Path(__file__).resolve().parents[1]
NODES = [{"id": "build", "dependencies": []}, {"id": "review", "dependencies": ["build"]}]


@pytest.fixture
def db(tmp_path):
    with closing(open_database(tmp_path / "db")) as conn:
        apply_migrations(conn, ROOT / "migrations")
        yield conn


def publish(db, nodes=NODES, key="publish"):
    return publish_workflow(db, workflow_id="workflow", version="1", nodes=nodes,
                            principal="operator", idempotency_key=key)


def create(db, key="run"):
    return create_run(db, workflow_id="workflow", version="1", principal="operator", idempotency_key=key)


def test_publish_and_run_creation_replay_atomically(db):
    assert not publish(db).replayed
    assert publish(db).replayed
    result = create(db)
    assert not result.replayed and create(db).response == result.response and create(db).replayed
    run_id, tasks = result.response["run_id"], result.response["tasks"]
    assert result.response["state"] == "queued"
    assert db.execute("SELECT state,state_version FROM runs WHERE id=?", (run_id,)).fetchone() == ("queued", 1)
    assert db.execute("SELECT node_id,state FROM tasks WHERE run_id=? ORDER BY node_id", (run_id,)).fetchall() == [("build", "created"), ("review", "created")]
    assert db.execute("SELECT task_id,depends_on_id FROM task_dependencies").fetchall() == [(tasks["review"], tasks["build"])]
    assert db.execute("SELECT type FROM events ORDER BY sequence").fetchall() == [("workflow.published",), ("run.created",), ("task.created",), ("task.created",), ("run.state_changed",)]
    assert db.execute("SELECT count(*) FROM outbox").fetchone() == (5,)
    with pytest.raises(IdempotencyConflict):
        publish(db, [{"id": "different", "dependencies": []}])


@pytest.mark.parametrize("nodes", [[], [{"id": "a", "dependencies": ["missing"]}],
    [{"id": "a", "dependencies": ["a"]}],
    [{"id": "a", "dependencies": ["b"]}, {"id": "b", "dependencies": ["a"]}],
    [{"id": "a", "dependencies": []}, {"id": "a", "dependencies": []}],
    [{"id": "a", "dependencies": ["a", "a"]}]])
def test_invalid_graph_is_rejected_without_writes(db, nodes):
    with pytest.raises(ValueError):
        publish(db, nodes)
    for table in ("workflow_versions", "events", "outbox", "idempotency_keys"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_run_outbox_failure_rolls_back_all_rows(db):
    publish(db)
    db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        create(db)
    for table in ("runs", "tasks", "task_dependencies"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM events").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM idempotency_keys").fetchone() == (1,)
    db.execute("DROP TRIGGER fail_outbox")
    assert not create(db).replayed


def test_competing_identical_creation_replays_single_run(db):
    publish(db)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)
    def invoke(_):
        with closing(open_database(path)) as conn:
            barrier.wait(timeout=5)
            return create(conn)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = pool.map(invoke, range(2))
    assert one.response == two.response
    assert sorted([one.replayed, two.replayed]) == [False, True]
    assert db.execute("SELECT count(*) FROM runs").fetchone() == (1,)


def test_upgrade_preserves_published_workflow_and_blocks_replacement(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    for source in sorted((ROOT / "migrations").glob("000[1-6]_*.sql")):
        (directory / source.name).write_bytes(source.read_bytes())
    with closing(open_database(tmp_path / "upgrade.db")) as conn:
        assert apply_migrations(conn, directory) == [1, 2, 3, 4, 5, 6]
        conn.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        source = ROOT / "migrations" / "0007_immutable_workflows.sql"
        (directory / source.name).write_bytes(source.read_bytes())
        assert apply_migrations(conn, directory) == [7]
        for recursive in (0, 1):
            conn.execute(f"PRAGMA recursive_triggers={recursive}")
            for sql in ("UPDATE workflow_versions SET version='2' WHERE workflow_id='wf'", "DELETE FROM workflow_versions WHERE workflow_id='wf'", "INSERT OR REPLACE INTO workflow_versions VALUES('wf','1','{}','hash','now')"):
                with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                    conn.execute(sql)
        assert conn.execute("SELECT workflow_id,version FROM workflow_versions").fetchall() == [("wf", "1")]


def test_run_creation_rejects_corrupt_legacy_workflow_without_writes(db):
    db.execute("INSERT INTO workflow_versions VALUES('workflow','1','{}','incorrect','now')")
    with pytest.raises(ValueError, match="hash mismatch"):
        create(db)
    for table in ("runs", "tasks", "events", "outbox", "idempotency_keys"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
