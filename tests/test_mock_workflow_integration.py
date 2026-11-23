"""Offline integration from workflow publication through verified completion."""
import asyncio
from pathlib import Path

from synapse.domain.runs import RunChange, RunState
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.infrastructure.daemon_database import daemon_database
from synapse.orchestration.mock_engine import MockWorkflowEngine
from synapse.persistence.event_store import read_events
from synapse.persistence.run_snapshot import read_run_snapshot
from synapse.persistence.run_store import write_run_transition
from synapse.persistence.workflow_store import create_run, publish_workflow
from synapse.providers.mock import MockOutcome, MockProvider, MockScenario

ROOT = Path(__file__).resolve().parents[1]


def test_published_dag_runs_to_verified_completion_and_survives_reopen(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    blobs_path = tmp_path / "blobs"
    blobs_path.mkdir(mode=0o700)
    provider = MockProvider({"draft": MockScenario(("draft",), MockOutcome.SUCCEEDED),
                             "corrected": MockScenario(("corrected",), MockOutcome.SUCCEEDED),
                             "review": MockScenario(("reviewed",), MockOutcome.SUCCEEDED)})
    with daemon_database(state / "db.sqlite", ROOT / "migrations") as db:
        db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,max_concurrency,created_at) VALUES('mock','mock','Mock','mock',2,'now')")
        workflow = dict(workflow_id="workflow", version="1", principal="operator", idempotency_key="publish", nodes=[
            {"id": "edit", "dependencies": []}, {"id": "review", "dependencies": ["edit"]}])
        assert not publish_workflow(db, **workflow).replayed
        assert publish_workflow(db, **workflow).replayed
        command = dict(workflow_id="workflow", version="1", principal="operator", idempotency_key="create")
        created = create_run(db, **command)
        assert not created.replayed
        assert create_run(db, **command).response == created.response
        run_id, tasks = created.response["run_id"], created.response["tasks"]
        before = read_run_snapshot(db, run_id)
        assert before["run"]["state"] == "queued"
        assert before["dependencies"] == [{"task_id": tasks["review"], "depends_on_id": tasks["edit"]}]
        db.execute("BEGIN IMMEDIATE")
        write_run_transition(db, RunChange(run_id, RunState.QUEUED, RunState.RUNNING, 1), principal="operator")
        db.execute("COMMIT")
        reviews = []
        async def verify(task, candidate):
            reviews.append((task, candidate))
            return candidate != b"draft"
        engine = MockWorkflowEngine(db, provider, ArtifactBlobStore(blobs_path, max_bytes=1024), verify, verifier_id="independent-reviewer")
        result = asyncio.run(engine.run(run_id, "mock", {tasks["edit"]: ("draft", "corrected"), tasks["review"]: ("review",)}))
        assert result == "succeeded"
        assert reviews == [(tasks["edit"], b"draft"), (tasks["edit"], b"corrected"), (tasks["review"], b"reviewed")]
        assert db.execute("SELECT COUNT(*) FROM verification_results WHERE outcome='approved' AND candidate_artifact_id IS NOT NULL AND task_attempt_id IS NOT NULL").fetchone() == (2,)
        events = read_events(db, after_sequence=before["last_sequence"], limit=1000)
        assert any(e["type"] == "verification.recorded" for e in events)
        assert any(e["type"] == "run.state_changed" and e["payload"]["after"] == "succeeded" for e in events)
        assert db.execute("SELECT COUNT(*) FROM events").fetchone() == db.execute("SELECT COUNT(*) FROM outbox").fetchone()
    with daemon_database(state / "db.sqlite", ROOT / "migrations") as reopened:
        after = read_run_snapshot(reopened, run_id)
        assert after["run"]["state"] == "succeeded"
        assert {task["node_id"]: (task["state"], task["attempt_count"]) for task in after["tasks"]} == {"edit": ("completed", 2), "review": ("completed", 1)}
        assert after["last_sequence"] > before["last_sequence"]
        assert len(read_events(reopened, after_sequence=before["last_sequence"], limit=1000)) == len(events)
