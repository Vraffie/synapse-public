from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import json
from threading import Barrier

import pytest

from synapse.persistence.idempotency import execute_idempotent
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import StateConflict
from synapse.persistence.task_claim import claim_task


@pytest.fixture
def db(tmp_path):
    with closing(open_database(tmp_path / "claims.db")) as conn:
        apply_migrations(conn, Path(__file__).resolve().parents[1] / "migrations")
        conn.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        conn.execute("INSERT INTO runs VALUES('run','wf','1','running',0,'now','now')")
        conn.execute("INSERT INTO profiles(id,provider,display_name,config_ref,max_concurrency,created_at) VALUES('profile','mock','Mock','mock',1,'now')")
        for task, state in (("dependency", "completed"), ("one", "ready"), ("two", "ready")):
            conn.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES(?,'run',?,?,'now','now')", (task, task, state))
        conn.execute("INSERT INTO task_dependencies VALUES('one','dependency')")
        yield conn


def claim(conn, task, key):
    return execute_idempotent(
        conn, principal="daemon", operation="claim", key=key,
        request={"task": task, "version": 0, "profile": "profile"},
        execute=lambda db: claim_task(db, task_id=task, expected_version=0,
                                      profile_id="profile", principal="daemon"),
    )


def test_claim_is_atomic_and_replays(db):
    assert claim(db, "one", "key").response == 1
    assert claim(db, "one", "key").replayed
    assert db.execute("SELECT state,state_version,assigned_profile_id FROM tasks WHERE id='one'").fetchone() == ("assigned", 1, "profile")
    assert db.execute("SELECT type FROM events").fetchone() == ("task.state_changed",)
    payload = json.loads(db.execute("SELECT payload_json FROM events").fetchone()[0])
    assert payload == {"before": "ready", "after": "assigned", "profile_id": "profile",
                       "version": 1, "principal": "daemon"}
    assert db.execute("SELECT COUNT(*) FROM outbox").fetchone() == (1,)
    with pytest.raises(StateConflict, match="capacity"):
        claim(db, "two", "second")


def test_dependencies_and_run_state_are_rechecked(db):
    db.execute("UPDATE tasks SET state='failed' WHERE id='dependency'")
    with pytest.raises(StateConflict, match="dependencies"):
        claim(db, "one", "key")
    db.execute("UPDATE runs SET state='paused'")
    with pytest.raises(StateConflict, match="Run"):
        claim(db, "two", "key2")
    assert db.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)


def test_outbox_failure_rolls_back_assignment(db):
    db.execute("CREATE TRIGGER fail_claim BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="failed"):
        claim(db, "one", "key")
    assert db.execute("SELECT state,state_version,assigned_profile_id FROM tasks WHERE id='one'").fetchone() == ("ready", 0, None)
    for table in ("events", "outbox", "idempotency_keys"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_competing_claims_respect_profile_capacity(db):
    path = db.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)

    def run(task):
        with closing(open_database(path)) as conn:
            barrier.wait(timeout=5)
            try:
                return claim(conn, task, task).response
            except StateConflict:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("one", "two")))
    assert results.count(1) == results.count(None) == 1
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE state='assigned'").fetchone() == (1,)


def test_cross_run_dependency_does_not_unlock_task(db):
    db.execute("INSERT INTO runs VALUES('other','wf','1','running',0,'now','now')")
    db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES('foreign','other','foreign','completed','now','now')")
    db.execute("INSERT INTO task_dependencies VALUES('two','foreign')")
    with pytest.raises(StateConflict, match="outside"):
        claim(db, "two", "key")


def test_global_capacity_rechecked_in_transaction(db):
    db.execute("UPDATE profiles SET max_concurrency=10 WHERE id='profile'")
    for number in range(6):
        task_id = f"active-{number}"
        db.execute("INSERT INTO tasks(id,run_id,node_id,state,assigned_profile_id,created_at,updated_at) VALUES(?,'run',?,'running','profile','now','now')", (task_id, task_id))
    with pytest.raises(StateConflict, match="capacity"):
        claim(db, "one", "key")
    assert db.execute("SELECT state FROM tasks WHERE id='one'").fetchone() == ("ready",)


@pytest.mark.parametrize("attempt_state", ["running", "unknown", "unrecognized"])
def test_unresolved_attempt_holds_capacity_after_task_leaves_running(db, attempt_state):
    db.execute(
        "UPDATE tasks SET state='blocked',assigned_profile_id='profile' WHERE id='two'"
    )
    db.execute(
        "INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('attempt','two',1,'profile',?)",
        (attempt_state,),
    )
    with pytest.raises(StateConflict, match="capacity"):
        claim(db, "one", "key")
    assert db.execute("SELECT state FROM tasks WHERE id='one'").fetchone() == ("ready",)


def test_unresolved_attempt_without_profile_fails_closed(db):
    db.execute(
        "INSERT INTO task_attempts(id,task_id,attempt_no,state) VALUES('attempt','two',1,'unknown')"
    )
    with pytest.raises(StateConflict, match="unknown profile"):
        claim(db, "one", "key")


def test_assigned_task_with_attempt_uses_one_slot(db):
    db.execute("UPDATE profiles SET max_concurrency=2 WHERE id='profile'")
    db.execute("UPDATE tasks SET state='assigned',assigned_profile_id='profile' WHERE id='two'")
    db.execute(
        "INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('attempt','two',1,'profile','running')"
    )
    assert claim(db, "one", "key").response == 1



def test_ready_task_with_unresolved_attempt_cannot_be_claimed(db):
    db.execute("UPDATE profiles SET max_concurrency=2 WHERE id='profile'")
    db.execute(
        "INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) "
        "VALUES('previous','one',1,'profile','unknown')"
    )
    with pytest.raises(StateConflict, match="unresolved"):
        claim(db, "one", "key")
    assert db.execute("SELECT state FROM tasks WHERE id='one'").fetchone() == ("ready",)


def test_generic_transition_cannot_bypass_claim_checks(db):
    from synapse.domain.workflow import State, plan_transition
    from synapse.persistence.state_store import transition_task, write_task_transition

    change = plan_transition("one", State.READY, State.ASSIGNED, 0)
    # Even a task with unmet dependencies cannot be assigned through either
    # generic entry point, and no partial state or event is persisted.
    db.execute("UPDATE tasks SET state='failed' WHERE id='dependency'")
    with pytest.raises(ValueError, match="requires claim_task"):
        transition_task(db, change, principal="daemon")
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(ValueError, match="requires claim_task"):
        write_task_transition(db, change, principal="daemon")
    db.execute("ROLLBACK")
    assert db.execute("SELECT state,state_version,assigned_profile_id FROM tasks WHERE id='one'").fetchone() == ("ready", 0, None)
    assert db.execute("SELECT count(*) FROM events").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM outbox").fetchone() == (0,)
    # Restoring eligibility allows the dedicated transactional path to claim it.
    db.execute("UPDATE tasks SET state='completed' WHERE id='dependency'")
    assert claim(db, "one", "allowed").response == 1


def _old_run_holding_slot(db, run_state, attempt_state="unknown", with_attempt=True):
    db.execute("INSERT INTO runs VALUES('old','wf','1',?,0,'now','now')", (run_state,))
    db.execute("INSERT INTO tasks(id,run_id,node_id,state,assigned_profile_id,created_at,updated_at) "
               "VALUES('held','old','held','running','profile','now','now')")
    if with_attempt:
        db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) "
                   "VALUES('held-attempt','held',1,'profile',?)", (attempt_state,))


@pytest.mark.parametrize("run_state", ["failed", "cancelled", "succeeded"])
@pytest.mark.parametrize("with_attempt", [True, False])
def test_finished_run_releases_profile_slot(db, run_state, with_attempt):
    _old_run_holding_slot(db, run_state, with_attempt=with_attempt)
    assert claim(db, "one", "key").response == 1
    if with_attempt:  # the honest record stays
        assert db.execute("SELECT state FROM task_attempts WHERE id='held-attempt'").fetchone() == ("unknown",)


def test_finished_run_attempt_without_profile_does_not_fail_closed(db):
    _old_run_holding_slot(db, "failed")
    db.execute("UPDATE task_attempts SET profile_id=NULL")
    assert claim(db, "one", "key").response == 1


@pytest.mark.parametrize("run_state", ["running", "paused"])
def test_unfinished_run_still_holds_slot(db, run_state):
    _old_run_holding_slot(db, run_state)
    with pytest.raises(StateConflict, match="capacity"):
        claim(db, "one", "key")


def test_global_cap_ignores_finished_run_but_holds_for_live_run(db):
    db.execute("UPDATE profiles SET max_concurrency=10 WHERE id='profile'")
    db.execute("INSERT INTO runs VALUES('dead','wf','1','cancelled',0,'now','now')")
    for number in range(6):
        db.execute("INSERT INTO tasks(id,run_id,node_id,state,assigned_profile_id,created_at,updated_at) "
                   "VALUES(?,'dead',?,'running','profile','now','now')", (f"d{number}", f"d{number}"))
    assert claim(db, "one", "key").response == 1
    db.execute("UPDATE runs SET state='running' WHERE id='dead'")
    with pytest.raises(StateConflict, match="capacity"):
        claim(db, "two", "key2")


def test_competing_claims_hold_caps_with_finished_run_noise(db):
    _old_run_holding_slot(db, "failed")
    test_competing_claims_respect_profile_capacity(db)
