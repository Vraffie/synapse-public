"""Daemon-started mock runs with approval gates: stored specs, parking, re-drive, expiry."""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

import pytest

from synapse.application.approval_service import ApprovalAccessDenied, ApprovalService
from synapse.daemon.mock_runtime import MockRuntime
from synapse.infrastructure.local_approver import LocalApproverAuthority, provision_approver_token
from synapse.persistence import mock_scenarios as ms
from synapse.persistence.approval_expiry import expire_due_approvals
from synapse.persistence.idempotency import IdempotencyConflict
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.workflow_store import create_run, publish_workflow

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "b" * 64
STEPS = {"review": {"operation": "push", "target": "repo", "candidate_sha256": DIGEST, "policy_version": "1"}}


class Env:
    def __init__(self, tmp_path):
        directory = tmp_path / "state"
        directory.mkdir(mode=0o700)
        self.fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        self.token = provision_approver_token(self.fd)
        self.authority = LocalApproverAuthority(self.fd)
        self.path = tmp_path / "db"
        self.db = open_database(self.path)
        apply_migrations(self.db, ROOT / "migrations")
        publish_workflow(self.db, workflow_id="wf", version="1", nodes=[{"id": "build", "dependencies": []}, {"id": "review", "dependencies": ["build"]}], principal="operator", idempotency_key="publish")
        self.run_id = create_run(self.db, workflow_id="wf", version="1", principal="operator", idempotency_key="create").response["run_id"]
        self.blobs = tmp_path / "blobs"
        self.blobs.mkdir(mode=0o700)
        self.runtime = self.new_runtime()

    def new_runtime(self):
        runtime = MockRuntime(self.db, self.blobs)
        runtime._park_poll = 0.01
        return runtime

    def restart(self):
        self.db.close()
        self.db = open_database(self.path)
        self.runtime = self.new_runtime()
        self.runtime.recover_interrupted()

    def states(self):
        run = self.db.execute("SELECT state FROM runs WHERE id=?", (self.run_id,)).fetchone()[0]
        return run, dict(self.db.execute("SELECT node_id,state FROM tasks WHERE run_id=?", (self.run_id,)))

    async def until(self, predicate, timeout=3):
        for _ in range(int(timeout / 0.01)):
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timeout; state=%r" % (self.states(),))

    async def parked(self):
        await self.until(lambda: self.states()[1].get("review") == "awaiting_approval")

    async def stop(self):
        self.runtime._active.cancel()
        await asyncio.gather(self.runtime._active, return_exceptions=True)

    def decide(self, decision="approved", token=None):
        aid = self.db.execute("SELECT id FROM approval_requests").fetchone()[0]
        return ApprovalService(self.db, self.authority).decide(
            token or self.token, approval_id=aid, expected_version=0, decision=decision, operation="push",
            target="repo", candidate_sha256=DIGEST, policy_version="1", idempotency_key="d-" + decision, correlation_id="t")

    def expire(self):
        self.db.execute("DROP TRIGGER approval_binding_immutable")
        self.db.execute("UPDATE approval_requests SET expires_at=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))

    def close(self):
        self.authority.close()
        os.close(self.fd)
        self.db.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_gated_run_parks_stays_running_then_resumes_after_decision(env):
    async def go():
        env.runtime.start(env.run_id, "s", approval_steps=STEPS)
        await env.parked()
        assert env.states() == ("running", {"build": "completed", "review": "awaiting_approval"})
        await asyncio.sleep(0.1)  # still parked, no recovery
        before = env.db.execute("SELECT COUNT(*) FROM events").fetchone()
        await asyncio.sleep(0.1)
        assert env.db.execute("SELECT COUNT(*) FROM events").fetchone() == before
        assert env.states()[0] == "running" and not env.runtime._active.done()
        env.decide()
        await asyncio.wait_for(env.runtime._active, 3)
    asyncio.run(go())
    assert env.states() == ("succeeded", {"build": "completed", "review": "completed"})
    assert ms.mock_kind(env.db, env.run_id) == ms.SCENARIO_KIND
    assert env.db.execute("SELECT requester_subject FROM approval_requests").fetchone() == ("daemon:workflow",)


def test_wrong_token_cannot_decide(env):
    async def go():
        env.runtime.start(env.run_id, "s", approval_steps=STEPS)
        await env.parked()
        with pytest.raises(ApprovalAccessDenied):
            env.decide(token="x" * 43)
        assert env.states()[1]["review"] == "awaiting_approval"
        await env.stop()
    asyncio.run(go())


def test_restart_redrives_parked_run_without_resupplied_spec(env):
    async def first():
        env.runtime.start(env.run_id, "s", approval_steps=STEPS)
        await env.parked()
        await env.stop()
    asyncio.run(first())
    env.restart()
    assert env.runtime._pending_redrive == env.run_id and env.states()[0] == "running"

    async def second():
        assert env.runtime.resume_parked()
        await asyncio.sleep(0.1)
        assert env.states()[1]["review"] == "awaiting_approval" and env.states()[0] == "running"
        env.decide()
        await asyncio.wait_for(env.runtime._active, 3)
    asyncio.run(second())
    assert env.states() == ("succeeded", {"build": "completed", "review": "completed"})
    assert env.db.execute("SELECT COUNT(*) FROM approval_requests").fetchone() == (1,)


def test_restart_after_decision_resumes_on_redrive(env):
    async def first():
        env.runtime.start(env.run_id, "s", approval_steps=STEPS)
        await env.parked()
        await env.stop()
    asyncio.run(first())
    env.decide()
    env.restart()

    async def second():
        env.runtime.resume_parked()
        await asyncio.wait_for(env.runtime._active, 3)
    asyncio.run(second())
    assert env.states()[0] == "succeeded"


def test_expiry_sweep_fails_task_and_run_completes_as_failed(env):
    async def go():
        env.runtime.start(env.run_id, "s", approval_steps=STEPS)
        await env.parked()
        env.expire()
        assert expire_due_approvals(env.db) != []
        assert env.states()[1]["review"] == "failed"
        await asyncio.wait_for(env.runtime._active, 3)
    asyncio.run(go())
    assert env.states() == ("failed", {"build": "completed", "review": "failed"})


def test_rejection_fails_task(env):
    async def go():
        env.runtime.start(env.run_id, "s", approval_steps=STEPS)
        await env.parked()
        env.decide("rejected")
        await asyncio.wait_for(env.runtime._active, 3)
    asyncio.run(go())
    assert env.states() == ("failed", {"build": "completed", "review": "failed"})


def test_cancel_while_parked(env):
    async def go():
        env.runtime.start(env.run_id, "s", approval_steps=STEPS)
        await env.parked()
        env.runtime.cancel(env.run_id, "c")
        await asyncio.wait_for(env.runtime._cancelling, 3)
    asyncio.run(go())
    run, tasks = env.states()
    assert run == "cancelled" and tasks["review"] == "cancelled"


def test_other_blocked_work_still_recovers_conservatively(env):
    async def go():
        env.runtime.start(env.run_id, "s", {"build": ["fail", "fail"]}, STEPS)
        await asyncio.wait_for(env.runtime._active, 3)
    asyncio.run(go())
    run, tasks = env.states()
    assert run == "recovering" and tasks["build"] == "failed"


@pytest.mark.parametrize("bad", [
    {}, {"nope": STEPS["review"]}, {"review": {**STEPS["review"], "extra": 1}},
    {"review": {k: v for k, v in STEPS["review"].items() if k != "target"}},
    {"review": {**STEPS["review"], "candidate_sha256": "A" * 64}},
    {"review": {**STEPS["review"], "operation": "bad op"}},
    {"review": {**STEPS["review"], "ttl_seconds": 0}},
    {"review": {**STEPS["review"], "ttl_seconds": 86401}},
    {"review": {**STEPS["review"], "ttl_seconds": True}},
    {"review": "push"},
])
def test_invalid_specs_rejected_without_state_change(env, bad):
    async def go():
        with pytest.raises(ms.ScenarioError):
            env.runtime.start(env.run_id, "s", approval_steps=bad)
    asyncio.run(go())
    assert env.states()[0] == "queued"
    assert env.db.execute("SELECT COUNT(*) FROM mock_run_config").fetchone() == (0,)
    assert env.db.execute("SELECT COUNT(*) FROM events WHERE type=?", (ms.STEPS_EVENT_TYPE,)).fetchone() == (0,)


def test_steps_are_immutable_and_replay_safe(env):
    async def go():
        env.runtime.start(env.run_id, "s", approval_steps=STEPS)
        await env.parked()
        await env.stop()
        with pytest.raises(IdempotencyConflict):
            env.runtime.start(env.run_id, "s", approval_steps={"review": {**STEPS["review"], "target": "other"}})
        assert env.runtime.start(env.run_id, "s", approval_steps=STEPS).replayed
    asyncio.run(go())
    with pytest.raises(Exception):
        env.db.execute("UPDATE events SET payload_json='{}' WHERE type=?", (ms.STEPS_EVENT_TYPE,))
    assert ms.load_approval_steps(env.db, env.run_id) == {"review": {**STEPS["review"], "ttl_seconds": 3600}}


def test_fixed_runs_unaffected_and_no_steps_means_no_approval(env):
    async def go():
        env.runtime.start(env.run_id, "s")
        await env.runtime._active
    asyncio.run(go())
    assert env.states()[0] == "succeeded" and ms.mock_kind(env.db, env.run_id) == ms.FIXED_KIND
    assert env.db.execute("SELECT COUNT(*) FROM approval_requests").fetchone() == (0,)


def test_daemon_startup_hook_runs_before_serving(env):
    from synapse.daemon.__main__ import _with_expiry_sweeps
    calls = []

    async def serve():
        calls.append("serve")
    asyncio.run(_with_expiry_sweeps(env.db, serve(), interval=0.01, startup=lambda: calls.append("startup")))
    assert calls == ["startup", "serve"]
