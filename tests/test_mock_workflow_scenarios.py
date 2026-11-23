"""Scenario-level tests of published mock workflows, not live providers."""
import asyncio
from pathlib import Path
import subprocess
import sys

import pytest

from synapse.domain.contracts import RunId
from synapse.domain.runs import RunChange, RunState
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.infrastructure.daemon_database import daemon_database
from synapse.orchestration.mock_engine import MockWorkflowEngine
from synapse.persistence.mock_recovery import recover_mock_run
from synapse.persistence.run_store import write_run_transition
from synapse.persistence.workflow_store import create_run, publish_workflow
from synapse.providers.mock import MockOutcome, MockProvider, MockScenario

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {
    "good": MockScenario(("good",), MockOutcome.SUCCEEDED),
    "bad": MockScenario(("bad",), MockOutcome.SUCCEEDED),
    "failure": MockScenario((), MockOutcome.FAILED),
    "slow": MockScenario(tuple("x" for _ in range(300)), MockOutcome.SUCCEEDED),
}


def prepare(db, nodes):
    db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,max_concurrency,created_at) VALUES('mock','mock','mock','mock',6,'now')")
    publish_workflow(db, workflow_id="wf", version="1", nodes=nodes, principal="operator", idempotency_key="publish")
    result = create_run(db, workflow_id="wf", version="1", principal="operator", idempotency_key="run").response
    db.execute("BEGIN IMMEDIATE")
    write_run_transition(db, RunChange(RunId(result["run_id"]), RunState.QUEUED, RunState.RUNNING, 1), principal="operator")
    db.execute("COMMIT")
    return result


CASES = [
    pytest.param({"a": []}, {"a": ("good",)}, 2, "succeeded", {"a": ("completed", 1)}, id="single-success"),
    pytest.param({"a": [], "b": ["a"]}, {"a": ("good",), "b": ("good",)}, 2, "succeeded", {"a": ("completed", 1), "b": ("completed", 1)}, id="dependency-success"),
    pytest.param({"a": []}, {"a": ("bad", "good")}, 2, "succeeded", {"a": ("completed", 2)}, id="review-correction"),
    pytest.param({"a": []}, {"a": ("bad",)}, 1, "blocked", {"a": ("changes_required", 1)}, id="review-rejection"),
    pytest.param({"a": []}, {"a": ("failure", "good")}, 2, "succeeded", {"a": ("completed", 2)}, id="provider-retry"),
    pytest.param({"a": []}, {"a": ("failure",)}, 2, "failed", {"a": ("failed", 2)}, id="provider-exhaustion"),
    pytest.param({"a": [], "b": ["a"]}, {"a": ("failure",), "b": ("good",)}, 1, "blocked", {"a": ("failed", 1), "b": ("waiting_dependencies", 0)}, id="failed-dependency"),
]


@pytest.mark.parametrize("graph,choices,max_attempts,expected,expected_tasks", CASES)
def test_mock_scenario_outcomes(tmp_path, graph, choices, max_attempts, expected, expected_tasks):
    nodes = [{"id": name, "dependencies": deps} for name, deps in graph.items()]
    with daemon_database(tmp_path / "state.db", ROOT / "migrations") as db:
        run = prepare(db, nodes)
        async def verify(_task, candidate):
            return candidate == b"good"
        engine = MockWorkflowEngine(db, MockProvider(SCENARIOS), ArtifactBlobStore(tmp_path, max_bytes=1024),
                                    verify, verifier_id="reviewer", max_attempts=max_attempts)
        result = asyncio.run(engine.run(run["run_id"], "mock", {run["tasks"][name]: scenario for name, scenario in choices.items()}))
        assert result == expected
        actual = db.execute("SELECT node_id,state,attempt_count FROM tasks WHERE run_id=? ORDER BY node_id", (run["run_id"],)).fetchall()
        assert {name: (state, count) for name, state, count in actual} == expected_tasks
        assert db.execute("SELECT COUNT(*) FROM events").fetchone() == db.execute("SELECT COUNT(*) FROM outbox").fetchone()
        if expected == "succeeded":
            assert db.execute("SELECT COUNT(*) FROM verification_results WHERE outcome='approved'").fetchone() == (len(graph),)
        if expected == "blocked":
            assert db.execute("SELECT state FROM runs WHERE id=?", (run["run_id"],)).fetchone() == ("running",)


def test_six_concurrent_workers_complete_seventh_waits(tmp_path):
    with daemon_database(tmp_path / "state.db", ROOT / "migrations") as db:
        run = prepare(db, [{"id": str(i), "dependencies": []} for i in range(7)])
        async def exercise():
            reached = asyncio.Event()
            release = asyncio.Event()
            streaming = set()
            provider = MockProvider(SCENARIOS)
            original_stream = provider.stream
            async def gated_stream(handle):
                async for event in original_stream(handle):
                    yield event
                    if event.kind == "started":
                        streaming.add(handle)
                        if len(streaming) == 6:
                            reached.set()
                        await release.wait()
            provider.stream = gated_stream
            async def verify(_task, candidate):
                return candidate == b"good"
            engine = MockWorkflowEngine(db, provider, ArtifactBlobStore(tmp_path, max_bytes=1024), verify, verifier_id="reviewer")
            running = asyncio.create_task(engine.run(run["run_id"], "mock", {task: ("good",) for task in run["tasks"].values()}))
            try:
                await asyncio.wait_for(reached.wait(), timeout=3)
                assert len(streaming) == len(engine.provider._active) == 6
                assert db.execute("SELECT COUNT(*) FROM task_attempts WHERE state='running'").fetchone() == (6,)
                assert db.execute("SELECT COUNT(*) FROM tasks WHERE state='running'").fetchone() == (6,)
                assert db.execute("SELECT COUNT(*) FROM tasks WHERE state='ready'").fetchone() == (1,)
            finally:
                release.set()
            assert await asyncio.wait_for(running, timeout=5) == "succeeded"
        asyncio.run(exercise())
        assert db.execute("SELECT COUNT(*) FROM tasks WHERE state='completed'").fetchone() == (7,)
        assert db.execute("SELECT COUNT(*) FROM verification_results WHERE outcome='approved'").fetchone() == (7,)


def test_cancel_single_mock_task_is_durable_and_never_verified(tmp_path):
    with daemon_database(tmp_path / "state.db", ROOT / "migrations") as db:
        run = prepare(db, [{"id": "a", "dependencies": []}])
        task_id = run["tasks"]["a"]
        async def exercise():
            async def verify(_task, _candidate):
                pytest.fail("cancelled candidate must not be verified")
            engine = MockWorkflowEngine(db, MockProvider(SCENARIOS), ArtifactBlobStore(tmp_path, max_bytes=1024), verify, verifier_id="reviewer")
            running = asyncio.create_task(engine.run(run["run_id"], "mock", {task_id: ("slow",)}))
            for _ in range(100):
                if task_id in engine.provider._active:
                    break
                await asyncio.sleep(0)
            assert await engine.cancel(task_id)
            assert await running == "cancelled"
        asyncio.run(exercise())
        assert db.execute("SELECT state FROM task_attempts WHERE task_id=?", (task_id,)).fetchone() == ("cancelled",)
        assert db.execute("SELECT COUNT(*) FROM verification_results").fetchone() == (0,)
    with daemon_database(tmp_path / "state.db", ROOT / "migrations") as reopened:
        assert reopened.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone() == ("cancelled",)
        assert reopened.execute("SELECT state FROM runs WHERE id=?", (run["run_id"],)).fetchone() == ("cancelled",)


def test_process_crash_during_review_recovers_conservatively_without_retry(tmp_path):
    with daemon_database(tmp_path / "state.db", ROOT / "migrations") as db:
        run = prepare(db, [{"id": "a", "dependencies": []}])
    task = run["tasks"]["a"]
    code = '''
import asyncio, os, sys
from pathlib import Path
from synapse.infrastructure.daemon_database import daemon_database
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.orchestration.mock_engine import MockWorkflowEngine
from synapse.providers.mock import MockProvider, MockOutcome, MockScenario
state = Path(sys.argv[1])
async def verify(task, candidate):
    os._exit(17)
with daemon_database(state / "state.db", Path(sys.argv[2])) as db:
    provider = MockProvider({"good": MockScenario(("good",), MockOutcome.SUCCEEDED)})
    engine = MockWorkflowEngine(db, provider, ArtifactBlobStore(state, max_bytes=1024), verify, verifier_id="reviewer")
    asyncio.run(engine.run(sys.argv[3], "mock", {sys.argv[4]: ("good",)}))
'''
    process = subprocess.run([sys.executable, "-c", code, str(tmp_path), str(ROOT / "migrations"),
                              run["run_id"], task], timeout=10)
    assert process.returncode == 17
    with daemon_database(tmp_path / "state.db", ROOT / "migrations") as db:
        assert db.execute("SELECT state FROM tasks WHERE id=?", (task,)).fetchone() == ("verifying",)
        assert db.execute("SELECT COUNT(*) FROM verification_results").fetchone() == (0,)
        unknown, blocked = recover_mock_run(db, run_id=run["run_id"], principal="daemon", no_workers_survive=True)
        assert unknown == () and blocked == (task,)
        assert recover_mock_run(db, run_id=run["run_id"], principal="daemon", no_workers_survive=True) == ((), ())
        async def verify(_task, _candidate):
            pytest.fail("recovery must not auto-retry a blocked task")
        engine = MockWorkflowEngine(db, MockProvider(SCENARIOS), ArtifactBlobStore(tmp_path, max_bytes=1024), verify, verifier_id="reviewer")
        assert asyncio.run(engine.run(run["run_id"], "mock", {task: ("good",)})) == "blocked"
        assert db.execute("SELECT COUNT(*) FROM task_attempts WHERE task_id=?", (task,)).fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM verification_results").fetchone() == (0,)
