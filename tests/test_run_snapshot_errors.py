"""Error paths and edge cases of run snapshots / task queries."""
from contextlib import closing
from pathlib import Path

import pytest

from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.run_snapshot import (
    RunNotFound, read_run_snapshot, read_run_tasks,
)


@pytest.fixture
def conn(tmp_path):
    with closing(open_database(tmp_path / "s.db")) as c:
        apply_migrations(c, Path(__file__).resolve().parents[1] / "migrations")
        c.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        c.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        c.execute("INSERT INTO runs VALUES('other','wf','1','created',0,'now','now')")
        yield c


def add_task(c, tid, run="run"):
    c.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",
              (tid, run, "n-" + tid, "created", "now", "now"))


@pytest.mark.parametrize("fn", [read_run_snapshot, read_run_tasks])
def test_unknown_run_raises_run_not_found_which_is_value_error(conn, fn):
    with pytest.raises(RunNotFound) as info:
        fn(conn, "missing")
    assert isinstance(info.value, ValueError) and isinstance(info.value, LookupError)
    assert not conn.in_transaction


@pytest.mark.parametrize("fn", [read_run_snapshot, read_run_tasks])
@pytest.mark.parametrize("bad", ["../etc", ""])
def test_invalid_run_id_rejected(conn, fn, bad):
    with pytest.raises(ValueError):
        fn(conn, bad)
    assert not conn.in_transaction


@pytest.mark.parametrize("fn", [read_run_snapshot, read_run_tasks])
def test_open_transaction_is_refused_and_left_untouched(conn, fn):
    conn.execute("BEGIN")
    with pytest.raises(RuntimeError, match="own read transaction"):
        fn(conn, "run")
    assert conn.in_transaction  # caller's transaction must not be ended for them
    conn.execute("ROLLBACK")


def test_snapshot_without_tasks_or_config(conn):
    snap = read_run_snapshot(conn, "run")
    assert snap["tasks"] == [] and snap["dependencies"] == []
    assert snap["mock_config"] is None
    assert snap["last_sequence"] == 0
    assert read_run_tasks(conn, "run") == {"tasks": [], "next_cursor": None}


def test_snapshot_only_includes_requested_run_and_orders_tasks(conn):
    add_task(conn, "b")
    add_task(conn, "a")
    add_task(conn, "z", run="other")
    conn.execute("INSERT INTO task_dependencies(task_id,depends_on_id) VALUES('b','a')")
    snap = read_run_snapshot(conn, "run")
    assert [t["id"] for t in snap["tasks"]] == ["a", "b"]
    assert snap["dependencies"] == [{"task_id": "b", "depends_on_id": "a"}]
    assert [t["id"] for t in read_run_tasks(conn, "other")["tasks"]] == ["z"]


def test_cross_run_dependency_fails_closed_and_rolls_back(conn):
    add_task(conn, "a")
    add_task(conn, "z", run="other")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT INTO task_dependencies(task_id,depends_on_id) VALUES('a','z')")
    with pytest.raises(RuntimeError, match="cross-run"):
        read_run_snapshot(conn, "run")
    assert not conn.in_transaction


def test_dangling_dependency_fails_closed(conn):
    add_task(conn, "a")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT INTO task_dependencies(task_id,depends_on_id) VALUES('a','ghost')")
    with pytest.raises(RuntimeError, match="missing or cross-run"):
        read_run_snapshot(conn, "run")
    assert not conn.in_transaction


def test_read_run_tasks_groups_attempts_in_order(conn):
    add_task(conn, "t1")
    add_task(conn, "t2")
    conn.execute("PRAGMA foreign_keys=OFF")
    for no in (2, 1):
        conn.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state,started_at) "
                     "VALUES(?,?,?,?,?,?)", (f"t1-{no}", "t1", no, "p", "completed", "now"))
    tasks = {t["id"]: t for t in read_run_tasks(conn, "run")["tasks"]}
    assert [a["attempt_no"] for a in tasks["t1"]["attempts"]] == [1, 2]
    assert tasks["t2"]["attempts"] == []
