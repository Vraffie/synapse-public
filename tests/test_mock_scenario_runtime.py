"""Catalog-driven mock scenarios run by the daemon-owned MockRuntime (scenario-v1)."""
import asyncio
from contextlib import closing
from pathlib import Path

import pytest

from synapse.daemon.mock_runtime import MockRuntime
from synapse.persistence import mock_scenarios as ms
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.workflow_store import create_run, publish_workflow

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path):
    with closing(open_database(tmp_path / "db")) as db:
        apply_migrations(db, ROOT / "migrations")
        publish_workflow(db, workflow_id="wf", version="1", nodes=[{"id": "build", "dependencies": []}, {"id": "review", "dependencies": ["build"]}], principal="operator", idempotency_key="publish")
        run_id = create_run(db, workflow_id="wf", version="1", principal="operator", idempotency_key="create").response["run_id"]
        blobs = tmp_path / "blobs"
        blobs.mkdir(mode=0o700)
        yield db, run_id, MockRuntime(db, blobs)


def states(db, run_id):
    run = db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    tasks = dict(db.execute("SELECT node_id,state FROM tasks WHERE run_id=?", (run_id,)))
    return run, tasks


def drive(runtime, run_id, scenarios, key="start"):
    async def go():
        result = runtime.start(run_id, key, scenarios)
        await runtime._active
        return result
    return asyncio.run(go())


def test_default_scenarios_succeed_and_record_kind(env):
    db, run_id, runtime = env
    drive(runtime, run_id, {"build": "succeed"})
    assert states(db, run_id) == ("succeeded", {"build": "completed", "review": "completed"})
    assert ms.mock_kind(db, run_id) == ms.SCENARIO_KIND
    assert db.execute("SELECT fixture FROM mock_run_config").fetchone() == ("fixed-v1",)
    assert db.execute("SELECT COUNT(*) FROM events WHERE type=?", (ms.EVENT_TYPE,)).fetchone() == (1,)


def test_failure_exhausts_attempts_and_fails_run(env):
    db, run_id, runtime = env
    drive(runtime, run_id, {"build": ["fail"] * ms.MOCK_MAX_ATTEMPTS})
    run, tasks = states(db, run_id)
    # Exhausted provider failure blocks the run; the daemon recovers it conservatively.
    assert run == "recovering" and tasks["build"] == "failed" and tasks["review"] != "completed"


def test_failure_then_success_retries_deterministically(env):
    db, run_id, runtime = env
    drive(runtime, run_id, {"build": ["fail", "succeed"]})
    assert states(db, run_id)[0] == "succeeded"
    assert db.execute("SELECT COUNT(*) FROM task_attempts a JOIN tasks t ON t.id=a.task_id WHERE t.node_id='build'").fetchone() == (2,)


def test_rejected_candidate_then_correction(env):
    db, run_id, runtime = env
    drive(runtime, run_id, {"review": ["reject", "succeed"]})
    assert states(db, run_id)[0] == "succeeded"
    outcomes = [r[0] for r in db.execute("SELECT outcome FROM verification_results ORDER BY rowid")]
    assert "changes_required" in outcomes and outcomes.count("approved") == 2


def test_cancellation_of_slow_scenario(env):
    db, run_id, runtime = env
    async def go():
        runtime.start(run_id, "start", {"build": "slow"})
        await asyncio.sleep(0)
        runtime.cancel(run_id, "cancel")
        await asyncio.wait_for(runtime._cancelling, 3)
    asyncio.run(go())
    run, tasks = states(db, run_id)
    assert run == "cancelled" and set(tasks.values()) == {"cancelled"}


def test_start_replay_is_idempotent_and_does_not_relaunch(env):
    db, run_id, runtime = env
    first = drive(runtime, run_id, {"build": "succeed"})
    attempts = db.execute("SELECT COUNT(*) FROM task_attempts").fetchone()
    async def replay():
        assert runtime.start(run_id, "start", {"build": "succeed"}).replayed
    asyncio.run(replay())
    assert not first.replayed
    assert db.execute("SELECT COUNT(*) FROM task_attempts").fetchone() == attempts


@pytest.mark.parametrize("bad", [
    {}, [], {"build": "rm -rf"}, {"nope": "succeed"}, {"build": []}, {"build": ["succeed"] * 99},
    {"build": 3}, {"bad id!": "succeed"},
])
def test_invalid_scenarios_fail_before_any_state_change(env, bad):
    db, run_id, runtime = env
    async def go():
        with pytest.raises(ValueError):
            runtime.start(run_id, "start", bad)
    asyncio.run(go())
    assert states(db, run_id)[0] == "queued"
    assert db.execute("SELECT COUNT(*) FROM mock_run_config").fetchone() == (0,)
    assert runtime._active is None


def test_duplicated_scenario_event_makes_run_not_understood(env):
    db, run_id, runtime = env
    drive(runtime, run_id, {"build": "succeed"})
    canonical = ms.load_scenarios(db, run_id)
    db.execute("BEGIN IMMEDIATE")
    ms.record_scenarios(db, run_id, canonical)
    db.execute("COMMIT")
    assert ms.load_scenarios(db, run_id) is None and ms.mock_kind(db, run_id) is None


def test_fixed_runs_are_unchanged(env):
    db, run_id, runtime = env
    drive(runtime, run_id, None)
    assert ms.mock_kind(db, run_id) == ms.FIXED_KIND
    assert states(db, run_id)[0] == "succeeded"
