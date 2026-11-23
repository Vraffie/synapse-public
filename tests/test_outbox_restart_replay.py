"""Regression coverage for durable outbox restart and event replay cursors.

These tests exercise the real SQLite migration, not an in-memory mock.
They intentionally do not assert concurrent dispatcher safety: the current
dispatcher contract is single-owner and has no durable claim/lease.
"""
from contextlib import closing
from pathlib import Path

from synapse.domain.workflow import State, plan_transition
from synapse.persistence.dispatcher import dispatch_batch
from synapse.persistence.event_store import pending_outbox, read_events
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import transition_task


MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def _seed(connection):
    connection.execute(
        "INSERT INTO workflow_versions VALUES('wf','1','{}','hash','2026-01-01T00:00:00Z')"
    )
    for run_id, task_id in (("run-a", "task-a"), ("run-b", "task-b")):
        connection.execute(
            "INSERT INTO runs(id,workflow_id,workflow_version,state,created_at,updated_at) "
            "VALUES(?,'wf','1','created','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
            (run_id,),
        )
        connection.execute(
            "INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) "
            "VALUES(?,?,'node','created','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
            (task_id, run_id),
        )


def test_restart_does_not_redeliver_acknowledged_event(tmp_path):
    path = tmp_path / "restart.sqlite"
    published = []
    with closing(open_database(path)) as connection:
        apply_migrations(connection, MIGRATIONS)
        _seed(connection)
        transition_task(
            connection, plan_transition("task-a", State.CREATED, State.READY, 0),
            principal="daemon",
        )
        transition_task(
            connection, plan_transition("task-b", State.CREATED, State.READY, 0),
            principal="daemon",
        )
        ids = pending_outbox(connection)
        assert len(ids) == 2
        summary = dispatch_batch(connection, lambda event: published.append(event["id"]), limit=1)
        assert (summary.delivered, summary.retried, summary.missing) == (1, 0, 0)
        assert published == ids[:1]

    with closing(open_database(path)) as restarted:
        assert pending_outbox(restarted) == ids[1:]
        summary = dispatch_batch(restarted, lambda event: published.append(event["id"]))
        assert (summary.delivered, summary.retried, summary.missing) == (1, 0, 0)
        assert pending_outbox(restarted) == []
        assert restarted.execute(
            "SELECT attempts, dispatched_at IS NOT NULL FROM outbox WHERE event_id=?",
            (ids[0],),
        ).fetchone() == (1, 1)
    assert published == ids


def test_replay_cursor_is_stable_across_restart_and_new_events(tmp_path):
    path = tmp_path / "replay.sqlite"
    with closing(open_database(path)) as connection:
        apply_migrations(connection, MIGRATIONS)
        _seed(connection)
        transition_task(
            connection, plan_transition("task-a", State.CREATED, State.READY, 0),
            principal="daemon",
        )
        transition_task(
            connection, plan_transition("task-b", State.CREATED, State.READY, 0),
            principal="daemon",
        )
        first = read_events(connection, run_id="run-a", limit=1)
        assert len(first) == 1
        cursor = first[0]["sequence"]
        assert read_events(connection, run_id="run-a", after_sequence=cursor) == []

    with closing(open_database(path)) as restarted:
        transition_task(
            restarted, plan_transition("task-a", State.READY, State.BLOCKED, 1),
            principal="daemon",
        )
        subsequent = read_events(restarted, run_id="run-a", after_sequence=cursor)
        assert len(subsequent) == 1
        assert subsequent[0]["sequence"] > cursor
        assert subsequent[0]["payload"]["after"] == "blocked"
        assert read_events(restarted, run_id="run-b", after_sequence=cursor)[0]["aggregate_id"] == "task-b"
        assert read_events(restarted, run_id="run-a", after_sequence=subsequent[0]["sequence"]) == []
