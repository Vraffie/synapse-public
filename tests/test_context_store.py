from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from synapse.persistence.context_store import write_context, write_context_idempotently
from synapse.persistence.idempotency import IdempotencyConflict
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import StateConflict


@pytest.fixture
def db(tmp_path):
    with closing(open_database(tmp_path / "context.db")) as conn:
        apply_migrations(conn, Path(__file__).resolve().parents[1] / "migrations")
        conn.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        conn.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        yield conn


def command(db, expected_version, content, key):
    return write_context_idempotently(
        db, scope="run", scope_id="run", key="summary",
        expected_version=expected_version, content=content,
        principal="daemon", idempotency_key=key,
    )


def test_context_cas_history_and_replay(db):
    assert command(db, 0, {"step": 1}, "create").response == 1
    assert command(db, 0, {"step": 1}, "create").replayed
    assert command(db, 1, {"step": 2}, "update").response == 2
    with pytest.raises(StateConflict):
        command(db, 1, {"step": 3}, "stale")
    assert db.execute("SELECT version FROM context_documents").fetchone() == (2,)
    assert db.execute("SELECT version FROM context_revisions ORDER BY version").fetchall() == [(1,), (2,)]
    assert db.execute("SELECT COUNT(*) FROM events WHERE run_id='run'").fetchone() == (2,)
    document_id = db.execute("SELECT id FROM context_documents").fetchone()[0]
    assert db.execute("SELECT DISTINCT aggregate_id FROM events").fetchall() == [(document_id,)]
    assert db.execute("SELECT COUNT(*) FROM outbox").fetchone() == (2,)


def test_context_outbox_failure_rolls_back_all_writes(db):
    db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'fail'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fail"):
        command(db, 0, {"step": 1}, "key")
    for table in ("context_documents", "context_revisions", "events", "idempotency_keys"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_project_scope_and_invalid_json_are_rejected(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="task and run"):
            write_context(db, scope="project", scope_id="run", key="key",
                          expected_version=0, content={}, principal="daemon")
        with pytest.raises(TypeError):
            write_context(db, scope="run", scope_id="run", key="key",
                          expected_version=0, content={1: "invalid"}, principal="daemon")
    finally:
        db.execute("ROLLBACK")


def test_competing_context_writes_commit_only_one_revision(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    command(db, 0, {"step": 0}, "initial")
    path = db.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)

    def update(value):
        with closing(open_database(path)) as conn:
            barrier.wait(timeout=5)
            try:
                return command(conn, 1, {"step": value}, f"update-{value}").response
            except StateConflict:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, (1, 2)))
    assert results.count(2) == results.count(None) == 1
    assert db.execute("SELECT COUNT(*) FROM context_revisions").fetchone() == (2,)
    assert db.execute("SELECT COUNT(*) FROM outbox").fetchone() == (2,)



def test_idempotency_key_binds_scope_and_context_key(db):
    command(db, 0, {"step": 1}, "same")
    with pytest.raises(IdempotencyConflict):
        write_context_idempotently(
            db, scope="run", scope_id="run", key="different",
            expected_version=0, content={"step": 1},
            principal="daemon", idempotency_key="same",
        )
    assert db.execute("SELECT COUNT(*) FROM context_documents").fetchone() == (1,)
