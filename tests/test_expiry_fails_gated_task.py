"""The expiry sweep fails a task parked on the approval it expires (same transaction)."""
from datetime import datetime, timezone
import sqlite3

import pytest

from synapse.persistence import approval_expiry as mod
from synapse.persistence.approval_expiry import expire_due_approvals
from synapse.persistence.migrations import open_database
from synapse.persistence.state_store import StateConflict
from test_workflow_approval_gates import Env, env  # noqa: F401  (fixture reuse)


def counts_by_type(db, kind):
    return db.execute("SELECT COUNT(*) FROM events WHERE type=?", (kind,)).fetchone()[0]


def test_sweep_fails_parked_task_and_run_then_finishes(env):
    assert env.run() == "blocked"
    env.expire()
    assert expire_due_approvals(env.db) == [env.approval_id()]
    assert env.task()[0] == "failed"
    assert env.db.execute("SELECT resolved_at IS NOT NULL FROM task_approval_gates").fetchone() == (1,)
    assert env.db.execute("SELECT state FROM approval_requests").fetchone() == ("expired",)
    assert counts_by_type(env.db, "task.approval_resolved") == 1
    assert env.db.execute("SELECT COUNT(*) FROM audit_entries WHERE action='approval.expire'").fetchone() == (1,)
    assert env.task("free")[0] == "completed"
    # The engine only has to close the run out; it never re-resolves the gate.
    assert env.run() == "failed"
    assert counts_by_type(env.db, "task.approval_resolved") == 1


def test_sweep_replay_is_idempotent(env):
    env.run()
    env.expire()
    expire_due_approvals(env.db)
    before = env.counts()
    for _ in range(3):
        assert expire_due_approvals(env.db) == []
    assert env.counts() == before


def test_sweep_restart_idempotent(env, tmp_path):
    env.run()
    env.expire()
    expire_due_approvals(env.db)
    before = env.counts()
    env.db.close()
    env.db = open_database(tmp_path / "wf.db")
    assert expire_due_approvals(env.db) == []
    assert env.counts() == before and env.task()[0] == "failed"


def test_task_failure_and_expiry_are_atomic(env, monkeypatch):
    env.run()
    env.expire()
    def boom(*a, **k):
        raise sqlite3.OperationalError("injected")
    monkeypatch.setattr(mod, "write_task_approval_resolution", boom)
    before = env.counts()
    with pytest.raises(sqlite3.OperationalError):
        expire_due_approvals(env.db)
    assert env.db.in_transaction is False
    assert env.db.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)
    assert env.task() == ("awaiting_approval", 5, 1) and env.counts() == before
    monkeypatch.undo()
    assert expire_due_approvals(env.db) == [env.approval_id()]
    assert env.task()[0] == "failed"


def test_undecided_unexpired_approval_leaves_task_parked(env):
    env.run()
    assert expire_due_approvals(env.db) == []
    assert env.task() == ("awaiting_approval", 5, 1)


def test_decided_approval_is_never_touched_by_sweep(env):
    env.run()
    env.decide()
    env.expire()
    assert expire_due_approvals(env.db) == []  # approved rows are not swept
    assert env.task()[0] == "awaiting_approval"  # the engine fails it on its next pass
    assert env.run() == "failed"


def test_terminal_run_still_expires_approval_without_task_change(env):
    env.run()
    env.db.execute("UPDATE runs SET state='cancelled'")
    env.expire()
    assert expire_due_approvals(env.db) == [env.approval_id()]
    assert env.task()[0] == "awaiting_approval"


def test_heals_previously_expired_unresolved_gate(env):
    env.run()
    env.expire()
    # Simulate an approval expired by the pre-fix sweep: approval expired, gate unresolved.
    env.db.execute("UPDATE approval_requests SET state='expired', state_version=state_version+1")
    assert expire_due_approvals(env.db) == []
    assert env.task()[0] == "failed"
    before = env.counts()
    assert expire_due_approvals(env.db) == []
    assert env.counts() == before


def test_triggers_not_weakened_direct_failure_still_refused(env):
    env.run()
    with pytest.raises(sqlite3.DatabaseError):
        env.db.execute("UPDATE tasks SET state='failed' WHERE id='gated'")
    assert env.task()[0] == "awaiting_approval"
