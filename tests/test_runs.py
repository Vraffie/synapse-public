from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from synapse.domain.contracts import RunId
from synapse.domain.runs import RunChange, RunState
from synapse.domain.workflow import InvalidTransition
from synapse.persistence.event_store import pending_outbox, read_events
from synapse.persistence.idempotency import execute_idempotent
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.run_store import write_run_transition
from synapse.persistence.state_store import StateConflict


@pytest.mark.parametrize("state", [RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED])
def test_terminal_runs_cannot_resume(state):
    with pytest.raises(InvalidTransition):
        RunChange(RunId("run"), state, RunState.RUNNING, 0)


@pytest.mark.parametrize("state", [RunState.PAUSED, RunState.RECOVERING, RunState.AWAITING_APPROVAL])
def test_runs_can_resume_after_interruption(state):
    RunChange(RunId("run"), RunState.RUNNING, state, 0)
    RunChange(RunId("run"), state, RunState.RUNNING, 1)


def test_run_command_rolls_back_on_outbox_failure_and_replays(tmp_path):
    with closing(open_database(tmp_path / "runs.db")) as db:
        apply_migrations(db, Path(__file__).resolve().parents[1] / "migrations")
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        change = RunChange(RunId("run"), RunState.CREATED, RunState.QUEUED, 0)

        def execute(key):
            return execute_idempotent(
                db, principal="daemon", operation="queue_run", key=key,
                request={"run_id": change.run_id, "version": change.expected_version},
                execute=lambda conn: write_run_transition(conn, change, principal="daemon"),
            )

        db.execute(
            "CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox "
            "BEGIN SELECT RAISE(ABORT, 'outbox failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            execute("first")
        assert db.execute("SELECT state,state_version FROM runs").fetchone() == ("created", 0)
        assert read_events(db) == []
        assert pending_outbox(db) == []
        db.execute("DROP TRIGGER fail_outbox")
        assert execute("first").replayed is False
        assert execute("first").replayed is True
        with pytest.raises(StateConflict):
            execute("stale")
        assert db.execute("SELECT state,state_version FROM runs").fetchone() == ("queued", 1)
        event, = read_events(db, run_id="run")
        assert event["type"] == "run.state_changed"
        assert event["payload"] == {
            "before": "created", "after": "queued", "version": 1, "principal": "daemon",
        }
        assert pending_outbox(db) == [event["id"]]
