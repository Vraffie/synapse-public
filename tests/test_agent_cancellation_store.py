"""Direct tests of durable agent-cancellation intent and honest finalization.

``is_agent_run`` needs a fully configured real-agent run; it is replaced here
by a flag so the cancellation rules themselves are exercised in isolation.
"""
from contextlib import closing
from pathlib import Path

import pytest

from synapse.persistence import agent_cancellation as ac
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import StateConflict


@pytest.fixture
def is_agent(monkeypatch):
    flag = {"v": True}
    monkeypatch.setattr(ac, "is_agent_run", lambda db, run_id: flag["v"])
    return flag


@pytest.fixture
def db(tmp_path):
    with closing(open_database(tmp_path / "c.db")) as c:
        apply_migrations(c, Path(__file__).resolve().parents[1] / "migrations")
        c.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        c.execute("INSERT INTO runs VALUES('run','wf','1','running',0,'now','now')")
        yield c


def add_task(db, tid, state, version=0):
    db.execute("INSERT INTO tasks(id,run_id,node_id,state,state_version,created_at,updated_at) "
               "VALUES(?,?,?,?,?,'now','now')", (tid, "run", "n" + tid, state, version))


def add_attempt(db, tid, state, no=1):
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state,started_at) "
               "VALUES(?,?,?,?,?,'now')", (f"{tid}-a{no}", tid, no, "p", state))


def states(db):
    return (db.execute("SELECT state FROM runs WHERE id='run'").fetchone()[0],
            dict(db.execute("SELECT id,state FROM tasks WHERE run_id='run'").fetchall()))


def test_request_records_intent_once_and_is_idempotent_per_key(db, is_agent):
    assert not ac.cancellation_requested(db, "run")
    first = ac.request_agent_cancellation(db, run_id="run", key="k1")
    assert first.response == {"run_id": "run", "state": "cancellation_requested"}
    assert first.replayed is False
    assert ac.cancellation_requested(db, "run")
    again = ac.request_agent_cancellation(db, run_id="run", key="k1")
    assert again.response == first.response and again.replayed is True
    assert db.execute("SELECT COUNT(*) FROM events WHERE type=?", (ac.EVENT_TYPE,)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_second_request_with_new_key_conflicts(db, is_agent):
    ac.request_agent_cancellation(db, run_id="run", key="k1")
    with pytest.raises(StateConflict, match="already requested"):
        ac.request_agent_cancellation(db, run_id="run", key="k2")


@pytest.mark.parametrize("state", ["created", "paused", "completed", "failed", "cancelled"])
def test_only_running_runs_can_be_cancelled(db, is_agent, state):
    db.execute("UPDATE runs SET state=?", (state,))
    with pytest.raises(StateConflict, match="Only a running"):
        ac.request_agent_cancellation(db, run_id="run", key="k")
    assert not ac.cancellation_requested(db, "run")


def test_non_agent_run_rejected(db, is_agent):
    is_agent["v"] = False
    with pytest.raises(StateConflict):
        ac.request_agent_cancellation(db, run_id="run", key="k")


def test_unknown_run_rejected(db, is_agent):
    with pytest.raises(StateConflict):
        ac.request_agent_cancellation(db, run_id="ghost", key="k")


def test_ids_are_validated(db, is_agent):
    for bad in ("", "   ", "x" * 257, None):
        with pytest.raises(ValueError):
            ac.request_agent_cancellation(db, run_id=bad, key="k")
        with pytest.raises(ValueError):
            ac.request_agent_cancellation(db, run_id="run", key=bad)
        with pytest.raises(ValueError):
            ac.finalize_agent_cancellation(db, run_id=bad)


def test_finalize_without_intent_refuses_and_changes_nothing(db, is_agent):
    add_task(db, "t", "running")
    with pytest.raises(StateConflict, match="intent is missing"):
        ac.finalize_agent_cancellation(db, run_id="run")
    assert states(db) == ("running", {"t": "running"})
    assert not db.in_transaction


def test_finalize_unknown_run_refuses(db, is_agent):
    with pytest.raises(StateConflict, match="intent is missing"):
        ac.finalize_agent_cancellation(db, run_id="ghost")
    assert not db.in_transaction


def test_finalize_refuses_inside_open_transaction(db, is_agent):
    db.execute("BEGIN")
    with pytest.raises(RuntimeError, match="autocommit"):
        ac.finalize_agent_cancellation(db, run_id="run")
    db.execute("ROLLBACK")


def test_finalize_cancels_open_tasks_and_run_but_keeps_terminal_tasks(db, is_agent):
    add_task(db, "ready", "ready")
    add_task(db, "run1", "running")
    add_task(db, "done", "completed")
    add_task(db, "bad", "failed")
    add_attempt(db, "run1", "cancelled")
    add_attempt(db, "done", "completed")
    ac.request_agent_cancellation(db, run_id="run", key="k")
    ac.finalize_agent_cancellation(db, run_id="run")
    assert states(db) == ("cancelled", {"ready": "cancelled", "run1": "cancelled",
                                        "done": "completed", "bad": "failed"})


def test_verifying_task_goes_through_blocked_before_cancel(db, is_agent):
    add_task(db, "v", "verifying")
    ac.request_agent_cancellation(db, run_id="run", key="k")
    ac.finalize_agent_cancellation(db, run_id="run")
    assert states(db)[1] == {"v": "cancelled"}
    path = [r[0] for r in db.execute(
        "SELECT payload_json FROM events WHERE aggregate_id='v' ORDER BY sequence")]
    joined = " ".join(path)
    assert "blocked" in joined and joined.index("blocked") < joined.rindex("cancelled")


@pytest.mark.parametrize("attempt_state", ["created", "running", "unknown"])
def test_unresolved_attempt_blocks_finalization_atomically(db, is_agent, attempt_state):
    add_task(db, "a", "running")
    add_task(db, "b", "ready")
    add_attempt(db, "a", attempt_state)
    ac.request_agent_cancellation(db, run_id="run", key="k")
    with pytest.raises(StateConflict, match="no established outcome"):
        ac.finalize_agent_cancellation(db, run_id="run")
    assert states(db) == ("running", {"a": "running", "b": "ready"})
    assert not db.in_transaction


def test_recovering_run_can_be_finalized(db, is_agent):
    db.execute("UPDATE runs SET state='recovering'")
    add_task(db, "a", "ready")
    db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
               "VALUES('e','run','run','run',?,1,'{}','now')", (ac.EVENT_TYPE,))
    ac.finalize_agent_cancellation(db, run_id="run")
    assert states(db) == ("cancelled", {"a": "cancelled"})


def test_finalize_is_idempotent_once_cancelled(db, is_agent):
    add_task(db, "a", "ready")
    ac.request_agent_cancellation(db, run_id="run", key="k")
    ac.finalize_agent_cancellation(db, run_id="run")
    before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    ac.finalize_agent_cancellation(db, run_id="run")
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


@pytest.mark.parametrize("state", ["created", "paused", "completed", "failed"])
def test_finalize_refuses_runs_in_other_states(db, is_agent, state):
    db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
               "VALUES('e','run','run','run',?,1,'{}','now')", (ac.EVENT_TYPE,))
    db.execute("UPDATE runs SET state=?", (state,))
    with pytest.raises(StateConflict, match="cannot be cancelled"):
        ac.finalize_agent_cancellation(db, run_id="run")
    assert states(db)[0] == state
