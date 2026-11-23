from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from synapse.persistence.attempt_store import reserve_attempt
from synapse.persistence.idempotency import execute_idempotent
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import StateConflict


@pytest.fixture
def db(tmp_path):
    with closing(open_database(tmp_path / "attempts.db")) as conn:
        apply_migrations(conn, Path(__file__).resolve().parents[1] / "migrations")
        conn.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        conn.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        conn.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) VALUES('profile','mock','Mock','mock','now')")
        conn.execute("INSERT INTO tasks(id,run_id,node_id,state,state_version,assigned_profile_id,created_at,updated_at) VALUES('task','run','node','assigned',2,'profile','now','now')")
        yield conn


def reserve(conn, attempt, key, version=2):
    return execute_idempotent(
        conn, principal="daemon", operation="reserve", key=key,
        request={"task": "task", "attempt": attempt, "version": version},
        execute=lambda db: reserve_attempt(
            db, task_id="task", attempt_id=attempt,
            expected_task_version=version, principal="daemon",
        ),
    )


def test_reservation_is_atomic_and_replays(db):
    assert reserve(db, "attempt", "key").response == 1
    assert reserve(db, "attempt", "key").replayed
    assert db.execute("SELECT state,state_version,attempt_count FROM tasks").fetchone() == ("assigned", 3, 1)
    assert db.execute("SELECT attempt_no,state,profile_id FROM task_attempts").fetchone() == (1, "created", "profile")
    assert db.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)
    assert db.execute("SELECT COUNT(*) FROM outbox").fetchone() == (1,)
    with pytest.raises(StateConflict):
        reserve(db, "another", "new", version=3)


def test_failed_outbox_rolls_back_reservation(db):
    db.execute("CREATE TRIGGER fail_attempt_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="failed"):
        reserve(db, "attempt", "key")
    assert db.execute("SELECT state_version,attempt_count FROM tasks").fetchone() == (2, 0)
    for table in ("task_attempts", "events", "idempotency_keys"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_competing_reservations_have_one_winner(db):
    path = db.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)

    def run(name):
        with closing(open_database(path)) as conn:
            barrier.wait(timeout=5)
            try:
                return reserve(conn, name, name).response
            except StateConflict:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("first", "second")))
    assert results.count(1) == results.count(None) == 1
    assert db.execute("SELECT COUNT(*) FROM task_attempts").fetchone() == (1,)



def test_unknown_attempt_state_blocks_new_reservation(db):
    db.execute(
        "INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) "
        "VALUES('previous','task',1,'profile','unrecognized')"
    )
    with pytest.raises(StateConflict, match="unresolved"):
        reserve(db, "next", "key")
    assert db.execute("SELECT attempt_count FROM tasks").fetchone() == (0,)


def test_unassigned_profile_rejected_without_mutation(db):
    db.execute("UPDATE tasks SET assigned_profile_id=NULL")
    with pytest.raises(ValueError, match="assigned profile"):
        reserve(db, "attempt", "key")
    assert db.execute("SELECT COUNT(*) FROM task_attempts").fetchone() == (0,)


def test_restart_marks_interrupted_attempt_unknown_once(db):
    from synapse.persistence.attempt_store import mark_interrupted_attempts_unknown

    reserve(db, "attempt", "key")
    path = db.execute("PRAGMA database_list").fetchone()[2]
    with closing(open_database(path)) as reopened:
        reopened.execute("BEGIN IMMEDIATE")
        assert mark_interrupted_attempts_unknown(reopened, run_id="run", principal="daemon") == ("attempt",)
        reopened.execute("COMMIT")
        reopened.execute("BEGIN IMMEDIATE")
        assert mark_interrupted_attempts_unknown(reopened, run_id="run", principal="daemon") == ()
        reopened.execute("COMMIT")
        assert reopened.execute("SELECT state FROM task_attempts").fetchone() == ("unknown",)
        assert reopened.execute("SELECT COUNT(*) FROM events").fetchone() == (2,)
        assert reopened.execute("SELECT COUNT(*) FROM outbox").fetchone() == (2,)
        with pytest.raises(StateConflict, match="unresolved"):
            reserve(reopened, "next", "next", version=3)


def test_recovery_outbox_failure_rolls_back_attempt_state(db):
    from synapse.persistence.attempt_store import mark_interrupted_attempts_unknown

    reserve(db, "attempt", "key")
    db.execute("CREATE TRIGGER fail_recovery BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'failed'); END")
    db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="failed"):
            mark_interrupted_attempts_unknown(db, run_id="run", principal="daemon")
    finally:
        db.execute("ROLLBACK")
    assert db.execute("SELECT state FROM task_attempts").fetchone() == ("created",)
    assert db.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)


def test_running_requires_current_reserved_attempt(db):
    from synapse.domain.workflow import State, plan_transition
    from synapse.persistence.state_store import transition_task

    db.execute("UPDATE runs SET state='running'")
    with pytest.raises(StateConflict, match="current reserved attempt"):
        transition_task(db, plan_transition("task", State.ASSIGNED, State.RUNNING, 2), principal="daemon")
    assert db.execute("SELECT state,state_version FROM tasks").fetchone() == ("assigned", 2)
    assert db.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
    reserve(db, "attempt", "key")
    assert transition_task(db, plan_transition("task", State.ASSIGNED, State.RUNNING, 3), principal="daemon") == 4
    assert db.execute("SELECT state FROM tasks").fetchone() == ("running",)


@pytest.mark.parametrize("change", ["unknown", "failed", "profile", "number", "second_unresolved"])
def test_running_rejects_stale_or_unresolved_attempt(db, change):
    from synapse.domain.workflow import State, plan_transition
    from synapse.persistence.state_store import transition_task

    db.execute("UPDATE runs SET state='running'")
    reserve(db, "attempt", "key")
    if change in ("unknown", "failed"):
        db.execute("UPDATE task_attempts SET state=?", (change,))
    elif change == "profile":
        db.execute("UPDATE task_attempts SET profile_id=NULL")
    elif change == "number":
        db.execute("UPDATE tasks SET attempt_count=2")
    else:
        db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('other','task',2,'profile','unknown')")
    with pytest.raises(StateConflict, match="current reserved attempt"):
        transition_task(db, plan_transition("task", State.ASSIGNED, State.RUNNING, 3), principal="daemon")
    assert db.execute("SELECT state,state_version FROM tasks").fetchone() == ("assigned", 3)
    assert db.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)


@pytest.mark.parametrize("run_state", ["paused", "cancelled", "failed", "queued"])
def test_running_rejects_run_that_is_no_longer_running(db, run_state):
    from synapse.domain.workflow import State, plan_transition
    from synapse.persistence.state_store import transition_task

    db.execute("UPDATE runs SET state='running'")
    reserve(db, "attempt", "key")
    db.execute("UPDATE runs SET state=?", (run_state,))
    with pytest.raises(StateConflict, match="running run"):
        transition_task(db, plan_transition("task", State.ASSIGNED, State.RUNNING, 3), principal="daemon")
    assert db.execute("SELECT state,state_version FROM tasks").fetchone() == ("assigned", 3)
    assert db.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)
