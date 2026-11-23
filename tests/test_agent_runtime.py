"""AgentRuntime: real-provider tasks through the engine, using a fake CLI (no real claude, no network)."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from synapse.application.approval_service import ApprovalService
from synapse.daemon.agent_config import AgentConfigError, load_claude_cli_profile
from synapse.daemon.agent_runtime import AgentDisabled, AgentRuntime
from synapse.daemon.mock_runtime import MockRuntime
from synapse.infrastructure.local_approver import LocalApproverAuthority, provision_approver_token
from synapse.persistence import agent_tasks as at
from synapse.persistence import mock_scenarios as ms
from synapse.persistence.idempotency import IdempotencyConflict
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import StateConflict
from synapse.persistence.workflow_store import create_run, publish_workflow
from synapse.providers.claude_cli import ClaudeCliProfile

ROOT = Path(__file__).resolve().parents[1]
FAKE = '''#!{py}
import json, os, sys, time
mode = sys.stdin.read()
open("spawns.log", "a").write(mode + "\\n")
def emit(o): sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
def text(t): emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": t}}]}}}})
def ok(): emit({{"type": "result", "subtype": "success", "is_error": False, "usage": {{"input_tokens": 3, "output_tokens": 4}}}})
emit({{"type": "system", "subtype": "init"}})
if mode.startswith("ok"):
    text("hello " + "x" * 40000 if mode == "ok-long" else "hello")
    emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Edit", "input": {{"secret": "TOPSECRET"}}}}]}}}})
    ok()
elif mode == "noisy":
    for i in range(3000):
        text("progress step %d" % i)
    text("FAILED tests/test_a.py::test_one - AssertionError: boom")
    for i in range(3000, 6000):
        text("progress step %d" % i)
    text("FAILED tests/test_b.py::test_two - ValueError: bad")
    ok()
elif mode in ("errors", "errors-fail"):
    for i in range(3000):
        text("error: distinct failure number %d in module_%d" % (i, i))
    if mode == "errors-fail":
        sys.exit(3)
    ok()
elif mode == "nul":
    text("start\\x00binary")
    text("FAILED tests/test_n.py::test_nul - AssertionError: after nul")
    ok()
elif mode == "huge":
    for i in range(130):
        text(("%04d " % i) + "y" * 40000)
    ok()
elif mode == "noisy-fail":
    text("FAILED tests/test_c.py::test_three - KeyError: k")
    sys.exit(3)
elif mode == "usage-full":
    text("hi")
    emit({{"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0131914,
           "usage": {{"input_tokens": 2, "output_tokens": 4, "cache_read_input_tokens": 3474,
                      "cache_creation_input_tokens": 3200}}}})
elif mode == "usage-none":
    text("hi")
    emit({{"type": "result", "subtype": "success", "is_error": False}})
elif mode == "usage-junk":
    text("hi")
    emit({{"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": True,
           "usage": {{"input_tokens": "7", "output_tokens": -1, "cache_read_input_tokens": True}}}})
elif mode == "usage-big":
    text("hi")
    emit({{"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 1e300,
           "usage": {{"input_tokens": 2, "output_tokens": 2 ** 70, "cache_read_input_tokens": 3474}}}})
elif mode == "fail":
    sys.exit(3)
elif mode == "noresult":
    text("hi")
elif mode == "hang":
    open("pid", "w").write(str(os.getpid()))
    time.sleep(60)
'''


def digest(prompt):
    return hashlib.sha256(prompt.encode()).hexdigest()


class Env:
    def __init__(self, tmp_path, nodes=("work",), enabled=True):
        directory = tmp_path / "state"
        directory.mkdir(mode=0o700)
        self.state = directory
        self.fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        self.token = provision_approver_token(self.fd)
        self.authority = LocalApproverAuthority(self.fd)
        self.path = tmp_path / "db"
        self.db = open_database(self.path)
        apply_migrations(self.db, ROOT / "migrations")
        self.nodes = nodes
        wf = [{"id": n, "dependencies": [nodes[i - 1]] if i else []} for i, n in enumerate(nodes)]
        publish_workflow(self.db, workflow_id="wf", version="1", nodes=wf, principal="operator", idempotency_key="publish")
        self.run_id = create_run(self.db, workflow_id="wf", version="1", principal="operator", idempotency_key="create").response["run_id"]
        self.blobs = tmp_path / "blobs"
        self.blobs.mkdir(mode=0o700)
        exe = tmp_path / "bin" / "claude"
        exe.parent.mkdir()
        exe.write_text(FAKE.format(py=sys.executable))
        exe.chmod(0o755)
        self.exe = exe
        self.worktrees = {}
        for n in nodes:
            wt = tmp_path / ("wt-" + n)
            (wt / ".git").mkdir(parents=True)
            self.worktrees[n] = wt
        self.profile = ClaudeCliProfile(executable=str(exe), allowed_tools=("Read", "Edit", "Bash(git status)"),
                                        state_dir=str(directory), enabled=enabled, term_grace_seconds=0.3, permission_mode="plan")
        self.runtime = self.new_runtime()

    def new_runtime(self, db=None):
        runtime = AgentRuntime(db or self.db, self.blobs, profile=self.profile,
                               worktree_check=lambda run_id, task_id, path: ("a" * 40, True))
        runtime._park_poll = 0.01
        runtime._cancel_timeout = 10
        return runtime

    def spec(self, node="work", prompt="ok", **over):
        spec = {"prompt": prompt, "worktree": str(self.worktrees[node]), "allowed_tools": ["Read", "Edit"],
                "permission_mode": "acceptEdits", "timeout_seconds": 20}
        if "approval_step" in over:  # an approval must bind the worktree identity
            spec.update({"repo": "/srv/repo", "base_ref": "main", "worktree_head": "a" * 40})
        spec.update(over)
        return spec

    def specs(self, **over):
        return {n: self.spec(n, **over) for n in self.nodes}

    def spawns(self, node="work"):
        log = self.worktrees[node] / "spawns.log"
        return log.read_text().split() if log.exists() else []

    def states(self):
        run = self.db.execute("SELECT state FROM runs WHERE id=?", (self.run_id,)).fetchone()[0]
        return run, dict(self.db.execute("SELECT node_id,state FROM tasks WHERE run_id=?", (self.run_id,)))

    def attempts(self):
        return [r[0] for r in self.db.execute("SELECT a.state FROM task_attempts a JOIN tasks t ON t.id=a.task_id WHERE t.run_id=? ORDER BY a.attempt_no", (self.run_id,))]

    def summaries(self):
        rows = self.db.execute("SELECT sha256 FROM artifacts WHERE provenance_json LIKE '%agent_summary%'").fetchall()
        return [json.loads(self.runtime.blobs.read(r[0])) for r in rows]

    async def until(self, predicate, timeout=15):
        for _ in range(int(timeout / 0.02)):
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("timeout; state=%r" % (self.states(),))

    def decide(self, prompt="ok", decision="approved"):
        aid, bound = self.db.execute("SELECT id,candidate_manifest_sha256 FROM approval_requests").fetchone()
        return ApprovalService(self.db, self.authority).decide(
            self.token, approval_id=aid, expected_version=0, decision=decision, operation="agent.run",
            target="repo", candidate_sha256=bound, policy_version="1",
            idempotency_key="d-" + decision, correlation_id="t")

    def close(self):
        subprocess.run(["pkill", "-9", "-f", str(self.exe)], check=False)
        self.authority.close()
        os.close(self.fd)
        self.db.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


GATE = {"operation": "agent.run", "target": "repo", "policy_version": "1"}


async def finish(env, timeout=30):
    await asyncio.wait_for(env.runtime._active, timeout)


def test_success_completes_with_bounded_summary_and_usage(env):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="ok-long"))
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("succeeded", {"work": "completed"})
    assert env.attempts() == ["completed"] and env.spawns() == ["ok-long"]
    (summary,) = env.summaries()
    assert summary["outcome"] == "succeeded" and summary["tool_names"] == ["Edit"]
    assert summary["output_truncated"] is True and len(summary["output_text"]) <= 16 * 1024
    assert summary["output_bytes"] > 40000 and summary["usage"] == {"input_tokens": 3, "output_tokens": 4, "source": "claude-cli"}
    assert "TOPSECRET" not in json.dumps(summary)
    usage = env.db.execute("SELECT payload_json FROM events WHERE type='agent.usage_observed'").fetchall()
    assert len(usage) == 1 and json.loads(usage[0][0])["input_tokens"] == 3
    assert env.db.execute("SELECT outcome FROM verification_results").fetchall() == [("approved",)]
    # The prompt text lives only in a private blob; events carry hash and length.
    assert env.db.execute("SELECT 1 FROM events WHERE payload_json LIKE '%ok-long%'").fetchone() is None
    spec = at.load_task_specs(env.db, env.run_id)["work"]
    assert spec["prompt_sha256"] == digest("ok-long") and spec["prompt_bytes"] == 7
    assert env.runtime.blobs.read(spec["prompt_sha256"]) == b"ok-long"
    assert at.is_agent_run(env.db, env.run_id) and ms.mock_kind(env.db, env.run_id) is None
    assert env.db.execute("SELECT DISTINCT json_extract(payload_json,'$.principal') FROM events WHERE type='task.state_changed' AND json_extract(payload_json,'$.after')<>'completed'").fetchall() == [("agent-engine",)]


def _only(env, kind):
    rows = env.db.execute("SELECT id,sha256,byte_size FROM artifacts WHERE json_extract(provenance_json,'$.kind')=?", (kind,)).fetchall()
    return rows


def test_raw_output_is_stored_and_summary_carries_reduction_and_raw_hash(env):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="noisy"))
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("succeeded", {"work": "completed"})
    (summary,) = env.summaries()
    (raw_row,) = _only(env, "agent_raw_output")
    raw = env.runtime.blobs.with_max_bytes(4 * 1024 * 1024).read(raw_row[1])
    # The raw hash in the summary is recoverable: it names the stored raw artifact, byte for byte.
    assert summary["raw_sha256"] == raw_row[1] == hashlib.sha256(raw).hexdigest()
    assert summary["raw_artifact_id"] == raw_row[0] and summary["raw_complete"] is True
    assert summary["output_bytes"] == len(raw) == raw_row[2] > 60000
    assert raw.count(b"progress step") == 6000
    # The forwarded text is much smaller yet keeps every failure line.
    reduced = summary["output_text"]
    assert len(reduced.encode()) <= 16 * 1024 and "progress step 4000" not in reduced and "similar lines" in reduced
    assert "FAILED tests/test_a.py::test_one - AssertionError: boom" in reduced
    assert "FAILED tests/test_b.py::test_two - ValueError: bad" in reduced
    assert summary["reduction"]["kept_failures"] == 2 and summary["output_truncated"] is True
    assert env.db.execute("SELECT outcome FROM verification_results").fetchall() == [("approved",)]


def test_failed_attempt_still_stores_raw_and_keeps_failure_lines(env):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="noisy-fail"))
        await finish(env)
    asyncio.run(go())
    assert env.states()[1] == {"work": "failed"}
    (summary,) = env.summaries()
    (raw_row,) = _only(env, "agent_raw_output")
    assert summary["raw_sha256"] == raw_row[1] and "test_c.py::test_three" in summary["output_text"]


def test_no_output_stores_no_raw_artifact(env):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="fail"))
        await finish(env)
    asyncio.run(go())
    (summary,) = env.summaries()
    assert summary["raw_artifact_id"] is None and summary["output_bytes"] == 0
    assert summary["raw_sha256"] == hashlib.sha256(b"").hexdigest() and _only(env, "agent_raw_output") == []


def test_succeeded_attempt_fails_closed_when_raw_cannot_be_stored(env, monkeypatch):
    class Full:
        def put(self, chunks):
            raise OSError("disk full")
    monkeypatch.setattr(type(env.runtime.blobs), "with_max_bytes", lambda self, n: Full())
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="ok"))
        await finish(env)
    asyncio.run(go())
    assert env.states()[1] == {"work": "blocked"}
    assert env.db.execute("SELECT count(*) FROM verification_results").fetchone() == (0,)


def test_two_tasks_run_in_dependency_order(tmp_path):
    e = Env(tmp_path, nodes=("a", "b"))
    try:
        async def go():
            e.runtime.start(e.run_id, "s", e.specs())
            await finish(e)
        asyncio.run(go())
        assert e.states() == ("succeeded", {"a": "completed", "b": "completed"})
        assert e.spawns("a") == ["ok"] and e.spawns("b") == ["ok"]
    finally:
        e.close()


def test_failure_fails_task_and_is_not_retried(env):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="fail"))
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("failed", {"work": "failed"})
    assert env.attempts() == ["failed"] and env.spawns() == ["fail"]
    assert env.summaries()[0]["detail"] == "exit_nonzero"


def test_timeout_fails_task_and_kills_process(env):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="hang", timeout_seconds=0.6))
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("failed", {"work": "failed"})
    assert env.attempts() == ["failed"] and env.spawns() == ["hang"]
    assert env.summaries()[0]["outcome"] == "timed_out"
    pid = int((env.worktrees["work"] / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cancel_kills_process_group_and_marks_cancelled(env):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="hang"))
        await env.until(lambda: (env.worktrees["work"] / "pid").exists() and env.states()[1]["work"] == "running")
        env.runtime.cancel(env.run_id, "c")
        await asyncio.wait_for(env.runtime._cancelling, 15)
    asyncio.run(go())
    assert env.states() == ("cancelled", {"work": "cancelled"})
    assert env.attempts() == ["cancelled"] and env.spawns() == ["hang"]
    pid = int((env.worktrees["work"] / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_unknown_outcome_blocks_and_never_retries(env):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="noresult"))
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("recovering", {"work": "blocked"})
    assert env.attempts() == ["unknown"] and env.spawns() == ["noresult"]
    assert env.summaries()[0]["outcome"] == "unknown"


def test_graceful_stop_records_unknown_and_restart_never_respawns(env):
    async def first():
        env.runtime.start(env.run_id, "s", env.specs(prompt="hang"))
        await env.until(lambda: (env.worktrees["work"] / "pid").exists() and env.states()[1]["work"] == "running")
        env.runtime._active.cancel()
        await asyncio.gather(env.runtime._active, return_exceptions=True)
    asyncio.run(first())
    assert env.states() == ("running", {"work": "blocked"}) and env.attempts() == ["unknown"]
    env.db.close()
    env.db = open_database(env.path)
    env.runtime = env.new_runtime()
    env.runtime.recover_interrupted()
    assert env.states() == ("recovering", {"work": "blocked"})
    assert env.spawns() == ["hang"] and not env.runtime.resume_parked()


def test_hard_crash_image_recovers_to_unknown_blocked_without_respawn(env, tmp_path):
    image = tmp_path / "crash.db"

    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="hang"))
        # The attempt is recorded "running" before the spawn by design; wait for the fake CLI's
        # own pid file (written after its spawn log) so the spawn is observable.
        await env.until(lambda: (env.worktrees["work"] / "pid").exists() and env.attempts() == ["running"]
                        and env.states()[1]["work"] == "running")
        dst = sqlite3.connect(image)
        env.db.backup(dst)  # what a SIGKILLed daemon would leave behind
        dst.close()
        env.runtime._active.cancel()
        await asyncio.gather(env.runtime._active, return_exceptions=True)
    asyncio.run(go())
    db = open_database(image)
    try:
        assert db.execute("SELECT state FROM task_attempts").fetchall() == [("running",)]
        runtime = AgentRuntime(db, env.blobs, profile=env.profile)
        runtime.recover_interrupted()
        assert db.execute("SELECT state FROM task_attempts").fetchall() == [("unknown",)]
        assert db.execute("SELECT state FROM tasks").fetchall() == [("blocked",)]
        assert db.execute("SELECT state FROM runs WHERE id=?", (env.run_id,)).fetchone() == ("recovering",)
        assert not runtime.resume_parked()
        # A second restart is idempotent and still never re-spawns.
        runtime.recover_interrupted()
        assert env.spawns() == ["hang"]
    finally:
        db.close()


def test_gated_task_parks_without_spawning_then_runs_after_approval(env):
    async def go():
        env.runtime.start(env.run_id, "s", {"work": env.spec(approval_step=GATE)})
        await env.until(lambda: env.states()[1]["work"] == "awaiting_approval")
        await asyncio.sleep(0.3)
        assert env.spawns() == [] and env.states()[0] == "running"
        env.decide()
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("succeeded", {"work": "completed"})
    assert env.spawns() == ["ok"] and env.attempts() == ["completed", "completed"]
    step = at.load_task_specs(env.db, env.run_id)["work"]["approval_step"]
    assert step["candidate_sha256"] == at.spec_hash(at.load_task_specs(env.db, env.run_id)["work"]) != digest("ok") and step["ttl_seconds"] == 3600


def test_rejected_approval_never_spawns_process(env):
    async def go():
        env.runtime.start(env.run_id, "s", {"work": env.spec(approval_step=GATE)})
        await env.until(lambda: env.states()[1]["work"] == "awaiting_approval")
        env.decide(decision="rejected")
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("failed", {"work": "failed"})
    assert env.spawns() == []


def test_cancel_while_parked(env):
    async def go():
        env.runtime.start(env.run_id, "s", {"work": env.spec(approval_step=GATE)})
        await env.until(lambda: env.states()[1]["work"] == "awaiting_approval")
        env.runtime.cancel(env.run_id, "c")
        await asyncio.wait_for(env.runtime._cancelling, 10)
    asyncio.run(go())
    assert env.states() == ("cancelled", {"work": "cancelled"}) and env.spawns() == []


def test_restart_redrives_parked_run_from_stored_specs(env):
    async def first():
        env.runtime.start(env.run_id, "s", {"work": env.spec(approval_step=GATE)})
        await env.until(lambda: env.states()[1]["work"] == "awaiting_approval")
        env.runtime._active.cancel()
        await asyncio.gather(env.runtime._active, return_exceptions=True)
    asyncio.run(first())
    env.db.close()
    env.db = open_database(env.path)
    env.runtime = env.new_runtime()
    env.runtime.recover_interrupted()
    assert env.runtime._pending_redrive == env.run_id and env.states()[0] == "running"

    async def second():
        assert env.runtime.resume_parked()
        await asyncio.sleep(0.2)
        assert env.spawns() == []
        env.decide()
        await finish(env)
    asyncio.run(second())
    assert env.states() == ("succeeded", {"work": "completed"}) and env.spawns() == ["ok"]


@pytest.mark.parametrize("bad", [
    {"permission_mode": "bypassPermissions"}, {"permission_mode": "auto"}, {"permission_mode": "yolo"},
    {"allowed_tools": ["Read", "Write"]}, {"allowed_tools": []}, {"allowed_tools": "Read"},
    {"timeout_seconds": 0}, {"timeout_seconds": 3601}, {"timeout_seconds": True}, {"timeout_seconds": float("nan")},
    {"prompt": ""}, {"prompt": "x" * (at.MAX_PROMPT_BYTES + 1)}, {"prompt": "a\x00b"}, {"prompt": 5},
    {"worktree": "relative/path"}, {"worktree": "/nonexistent/worktree"}, {"extra": 1},
    {"approval_step": {**GATE, "candidate_sha256": "a" * 64}}, {"approval_step": {"operation": "x"}},
    {"approval_step": {**GATE, "ttl_seconds": 0}},
])
def test_invalid_specs_rejected_without_state_change(env, bad):
    async def go():
        with pytest.raises(at.AgentSpecError):
            env.runtime.start(env.run_id, "s", {"work": env.spec(**bad)})
    asyncio.run(go())
    assert env.states()[0] == "queued"
    assert env.db.execute("SELECT COUNT(*) FROM mock_run_config").fetchone() == (0,)
    assert env.db.execute("SELECT COUNT(*) FROM events WHERE type=?", (at.EVENT_TYPE,)).fetchone() == (0,)
    assert env.spawns() == []


def test_missing_and_unknown_nodes_and_shared_worktree_rejected(tmp_path):
    e = Env(tmp_path, nodes=("a", "b"))
    try:
        async def go():
            for specs in ({"a": e.spec("a")}, {**e.specs(), "c": e.spec("a")}, {"a": e.spec("a"), "b": e.spec("a")}, {}):
                with pytest.raises(at.AgentSpecError):
                    e.runtime.start(e.run_id, "s", specs)
        asyncio.run(go())
        assert e.states()[0] == "queued"
    finally:
        e.close()


def test_start_refused_without_enabled_profile(tmp_path):
    e = Env(tmp_path, enabled=False)
    try:
        async def go():
            with pytest.raises(AgentDisabled):
                e.runtime.start(e.run_id, "s", e.specs())
            with pytest.raises(AgentDisabled):
                AgentRuntime(e.db, e.blobs).start(e.run_id, "s", e.specs())
        asyncio.run(go())
        assert e.states()[0] == "queued" and e.spawns() == []
    finally:
        e.close()


def test_worktree_must_not_overlap_state_dir(env):
    inner = env.state / "wt"
    (inner / ".git").mkdir(parents=True)

    async def go():
        with pytest.raises(at.AgentSpecError):
            env.runtime.start(env.run_id, "s", {"work": env.spec(worktree=str(inner))})
    asyncio.run(go())


def test_specs_immutable_and_start_replay_safe(env):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="hang"))
        await env.until(lambda: env.states()[1]["work"] == "running")
        await env.until(lambda: env.spawns() == ["hang"])  # child really spawned (scratch setup precedes Popen)
        with pytest.raises(IdempotencyConflict):
            env.runtime.start(env.run_id, "s", env.specs(prompt="ok"))
        replay = env.runtime.start(env.run_id, "s", env.specs(prompt="hang"))
        assert replay.replayed
        with pytest.raises(StateConflict):
            env.runtime.start(env.run_id, "other", env.specs(prompt="hang"))
        env.runtime._active.cancel()
        await asyncio.gather(env.runtime._active, return_exceptions=True)
    asyncio.run(go())
    with pytest.raises(Exception):
        env.db.execute("UPDATE events SET payload_json='{}' WHERE type=?", (at.EVENT_TYPE,))
    assert env.spawns() == ["hang"]


def test_mock_guards_refuse_agent_runs(env):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="hang"))
        await env.until(lambda: env.states()[1]["work"] == "running")
        mock = MockRuntime(env.db, env.blobs)
        with pytest.raises(StateConflict):
            mock.cancel(env.run_id, "mc")
        mock.recover_interrupted()  # must skip the agent run, not recover it
        assert env.states() == ("running", {"work": "running"})
        env.runtime.cancel(env.run_id, "c")
        await asyncio.wait_for(env.runtime._cancelling, 15)
    asyncio.run(go())


# --- operator config ---------------------------------------------------------

def write_config(tmp_path, data, mode=0o600, name="cfg.json"):
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    path.chmod(mode)
    return path


def good(env, **over):
    cfg = {"executable": str(env.exe), "allowed_tools": ["Read", "Edit"], "enabled": True}
    cfg.update(over)
    return cfg


def test_config_loads_and_derives_state_dir(env, tmp_path):
    profile = load_claude_cli_profile(write_config(tmp_path, good(env, max_timeout_seconds=120)), env.state)
    assert profile.enabled and profile.allowed_tools == ("Read", "Edit") and profile.state_dir == str(env.state.resolve())
    assert profile.permission_mode == "plan" and profile.max_timeout_seconds == 120
    off = load_claude_cli_profile(write_config(tmp_path, good(env, enabled=False), name="off.json"), env.state)
    assert off.enabled is False


@pytest.mark.parametrize("over", [
    {"enabled": "yes"}, {"enabled": 1}, {"allowed_tools": []}, {"allowed_tools": "Read"}, {"allowed_tools": ["bad tool!"]},
    {"executable": "relative/claude"}, {"executable": "/nonexistent/claude"}, {"state_dir": "/tmp"},
    {"permission_mode": "bypassPermissions"}, {"api_key": "k"}, {"extra_args": []}, {"max_timeout_seconds": 0},
    {"max_timeout_seconds": 1e9},
])
def test_config_rejects_invalid(env, tmp_path, over):
    with pytest.raises(AgentConfigError):
        load_claude_cli_profile(write_config(tmp_path, good(env, **over)), env.state)


def test_config_rejects_unsafe_files(env, tmp_path):
    with pytest.raises(AgentConfigError):
        load_claude_cli_profile(tmp_path / "missing.json", env.state)
    with pytest.raises(AgentConfigError):
        load_claude_cli_profile(write_config(tmp_path, good(env), mode=0o666), env.state)
    with pytest.raises(AgentConfigError):
        load_claude_cli_profile(write_config(tmp_path, "not json"), env.state)
    with pytest.raises(AgentConfigError):
        load_claude_cli_profile(write_config(tmp_path, '{"enabled": true, "enabled": false, "executable": "/x", "allowed_tools": ["Read"]}'), env.state)
    with pytest.raises(AgentConfigError):
        load_claude_cli_profile(write_config(tmp_path, '{"enabled": NaN, "executable": "/x", "allowed_tools": ["Read"]}'), env.state)
    with pytest.raises(AgentConfigError):
        load_claude_cli_profile(write_config(tmp_path, "[]"), env.state)
    real = write_config(tmp_path, good(env), name="real.json")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(AgentConfigError):
        load_claude_cli_profile(link, env.state)
    with pytest.raises(AgentConfigError):
        load_claude_cli_profile(write_config(tmp_path, " " * (70 * 1024) + "{}", name="big.json"), env.state)


# ------------------------------------------------------------------ abandon
def _recovering(env):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="noresult"))
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("recovering", {"work": "blocked"})
    return env.db.execute("SELECT state_version FROM runs WHERE id=?", (env.run_id,)).fetchone()[0]


def test_abandon_fails_run_cancels_tasks_keeps_unknown_attempt_and_replays(env):
    from synapse.persistence.agent_abandon import ASSERTION
    version = _recovering(env)
    first = env.runtime.abandon(env.run_id, "a1", version, ASSERTION)
    assert first.response == {"run_id": env.run_id, "state": "failed", "cancelled_tasks": 1} and first.replayed is False
    assert env.states() == ("failed", {"work": "cancelled"})
    assert env.attempts() == ["unknown"]  # the unresolved outcome is never rewritten
    events = env.db.execute("SELECT payload_json FROM events WHERE run_id=? AND type='agent.run_abandoned'", (env.run_id,)).fetchall()
    assert len(events) == 1 and json.loads(events[0][0])["assertion"] == ASSERTION
    assert env.runtime.abandon(env.run_id, "a1", version, ASSERTION).replayed is True
    assert env.spawns() == ["noresult"]  # nothing was re-spawned
    with pytest.raises(StateConflict):  # a new key on a no-longer-recovering run
        env.runtime.abandon(env.run_id, "a2", version, ASSERTION)


def test_abandon_requires_exact_assertion_version_and_recovering_state(env):
    from synapse.persistence.agent_abandon import ASSERTION
    version = _recovering(env)
    with pytest.raises(ValueError):
        env.runtime.abandon(env.run_id, "a1", version, "yes")
    with pytest.raises(StateConflict):
        env.runtime.abandon(env.run_id, "a2", version + 1, ASSERTION)
    assert env.states() == ("recovering", {"work": "blocked"})  # refusals changed nothing
    with pytest.raises(IdempotencyConflict):
        env.runtime.abandon(env.run_id, "a3", version, ASSERTION)
        env.runtime.abandon(env.run_id, "a3", version + 5, ASSERTION)


def test_abandon_refuses_a_running_run(env):
    from synapse.persistence.agent_abandon import ASSERTION
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="hang"))
        await env.until(lambda: (env.worktrees["work"] / "pid").exists() and env.states()[1]["work"] == "running")
        with pytest.raises(StateConflict):
            env.runtime.abandon(env.run_id, "a1", 0, ASSERTION)
        env.runtime.cancel(env.run_id, "c")
        await asyncio.wait_for(env.runtime._cancelling, 15)
    asyncio.run(go())
    assert env.states() == ("cancelled", {"work": "cancelled"})


def _recovering_run_with_a_parked_gate(env, tmp_path):
    """Two independent tasks: `a` parks on an approval gate while `b` ends with no outcome (unknown)."""
    nodes = [{"id": "a", "dependencies": []}, {"id": "b", "dependencies": []}]
    publish_workflow(env.db, workflow_id="wf2", version="1", nodes=nodes, principal="operator", idempotency_key="publish2")
    run_id = create_run(env.db, workflow_id="wf2", version="1", principal="operator", idempotency_key="create2").response["run_id"]
    wts = {}
    for n in ("a", "b"):
        wts[n] = tmp_path / ("wt2-" + n)
        (wts[n] / ".git").mkdir(parents=True)
    specs = {"a": env.spec("work", approval_step=GATE, worktree=str(wts["a"])), "b": env.spec("work", prompt="noresult", worktree=str(wts["b"]))}

    async def go():
        env.runtime.start(run_id, "s2", specs)
        await env.until(lambda: env.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()[0] == "recovering")
    asyncio.run(go())
    return run_id


def test_abandon_resolves_the_parked_approval_gate_and_revokes_the_request(env, tmp_path):
    from synapse.persistence.agent_abandon import ASSERTION
    run_id = _recovering_run_with_a_parked_gate(env, tmp_path)
    task_states = dict(env.db.execute("SELECT node_id,state FROM tasks WHERE run_id=?", (run_id,)))
    assert task_states == {"a": "awaiting_approval", "b": "blocked"}  # the precondition the reviewer suspected
    version = env.db.execute("SELECT state_version FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    env.runtime.abandon(run_id, "ab", version, ASSERTION)
    assert env.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone() == ("failed",)
    assert {s for (s,) in env.db.execute("SELECT state FROM tasks WHERE run_id=?", (run_id,))} <= {"failed", "cancelled"}
    gate = env.db.execute("SELECT resolved_at FROM task_approval_gates WHERE run_id=?", (run_id,)).fetchone()
    approval = env.db.execute("SELECT state FROM approval_requests WHERE run_id=?", (run_id,)).fetchone()
    assert gate[0] is not None and approval == ("revoked",)  # no live, decidable approval is left behind
    payload = json.loads(env.db.execute("SELECT payload_json FROM events WHERE run_id=? AND type='agent.run_abandoned'", (run_id,)).fetchone()[0])
    assert payload["revoked_approvals"] == [env.db.execute("SELECT id FROM approval_requests WHERE run_id=?", (run_id,)).fetchone()[0]]
    assert env.db.execute("SELECT state FROM tasks WHERE run_id=? AND node_id='a'", (run_id,)).fetchone() == ("failed",)


# ------------------------------------------------------------------ usage ledger
def _ledger(env):
    return env.db.execute("SELECT attempt_id,profile_id,source,input_tokens,output_tokens,cache_read_tokens,"
                          "cache_write_tokens,cost_usd,confidence,source_event_id FROM usage_observations").fetchall()


def test_reported_usage_is_persisted_with_its_event_in_the_ledger(env):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="usage-full"))
        await finish(env)
    asyncio.run(go())
    ((attempt, profile, source, i, o, cr, cw, cost, confidence, event_id),) = _ledger(env)
    assert (profile, source, i, o, cr, cw, cost, confidence) == (
        at.AGENT_PROFILE, "claude-cli", 2, 4, 3474, 3200, 0.0131914, "reported")
    row = env.db.execute("SELECT payload_json,aggregate_id FROM events WHERE id=?", (event_id,)).fetchone()
    payload = json.loads(row[0])
    assert row[1] == attempt and payload["cache_read_tokens"] == 3474 and payload["cache_write_tokens"] == 3200
    assert payload["cost_usd"] == 0.0131914
    assert env.db.execute("SELECT 1 FROM outbox WHERE event_id=?", (event_id,)).fetchone() is not None


def test_an_oversized_field_does_not_drop_the_rest_of_the_record(env):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="usage-big"))
        await finish(env)
    asyncio.run(go())
    ((attempt, profile, source, i, o, cr, cw, cost, confidence, event_id),) = _ledger(env)
    # output_tokens (2**70) and cost (1e300) cannot be stored: they are unknown, the rest persists, the task succeeds
    assert (i, o, cr, cw, cost, confidence) == (2, None, 3474, None, None, "reported")
    assert json.loads(env.db.execute("SELECT payload_json FROM events WHERE id=?", (event_id,)).fetchone()[0])["input_tokens"] == 2
    assert env.states() == ("succeeded", {"work": "completed"})


@pytest.mark.parametrize("mode", ["usage-none", "usage-junk"])
def test_missing_or_unparseable_usage_records_nothing_never_zero(env, mode):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt=mode))
        await finish(env)
    asyncio.run(go())
    assert _ledger(env) == []
    assert env.db.execute("SELECT COUNT(*) FROM events WHERE type='agent.usage_observed'").fetchone() == (0,)
    assert env.states() == ("succeeded", {"work": "completed"})  # losing usage never fails the task


def _register_profile(env, profile_id, enabled):
    env.db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,enabled,max_concurrency,created_at) "
                   "VALUES(?,'claude','p','registry:x',?,1,'now')", (profile_id, enabled))
    env.db.commit()
    from synapse.daemon.profile_registry import ProfileRegistry, ProfileSpec
    env.runtime.registry = ProfileRegistry(
        (ProfileSpec(profile_id, "claude", str(env.exe), ("Read", "Edit"), 1, bool(enabled)),), primary=profile_id)


def test_named_profile_still_enabled_at_spawn_runs(env):
    _register_profile(env, "reader", 1)

    async def go():
        env.runtime.start(env.run_id, "s", env.specs(profile_id="reader"))
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("succeeded", {"work": "completed"}) and env.spawns() == ["ok"]


@pytest.mark.parametrize("registered,detail", [(0, "profile_disabled"), (None, "profile_unknown")])
def test_profile_disabled_or_removed_after_queueing_fails_closed(env, registered, detail):
    if registered is not None:
        _register_profile(env, "reader", registered)

    async def go():
        env.runtime.start(env.run_id, "s", env.specs(profile_id="reader"))
        await finish(env)
    asyncio.run(go())
    assert env.spawns() == []  # nothing was ever spawned
    run, tasks = env.states()
    assert tasks == {"work": "failed"} and run != "succeeded"
    (summary,) = env.summaries()
    assert summary["outcome"] == "failed" and summary["detail"] == detail


def test_default_profile_unaffected_by_profile_table(env):
    env.db.execute("UPDATE profiles SET enabled=0 WHERE id<>?", (at.AGENT_PROFILE,))

    async def go():
        env.runtime.start(env.run_id, "s", env.specs())  # no profile_id
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("succeeded", {"work": "completed"})


def _profile_run(env, runtime=None, **over):
    runtime = runtime or env.runtime

    async def go():
        runtime.start(env.run_id, "s", env.specs(profile_id="reader", **over))
        await asyncio.wait_for(runtime._active, 30)
    asyncio.run(go())


def test_non_registry_row_cannot_satisfy_a_named_profile(env):
    for pid, provider, ref in ((at.AGENT_PROFILE, "claude", "builtin:" + at.AGENT_KIND),
                               ("builtin-like", "claude", "builtin:x"), ("mocky", "mock", "registry:x")):
        env.db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,enabled,max_concurrency,created_at) "
                       "VALUES(?,?,'p',?,1,1,'now')", (pid, provider, ref))
    assert env.runtime._profile_problem(at.AGENT_PROFILE) == "profile_not_registry"
    assert env.runtime._profile_problem("builtin-like") == "profile_not_registry"
    assert env.runtime._profile_problem("mocky") == "profile_not_registry"
    assert env.runtime._profile_problem("absent") == "profile_unknown"


def _bare_engine(check):
    from synapse.orchestration.agent_engine import AgentWorkflowEngine
    engine = object.__new__(AgentWorkflowEngine)
    engine._profile_check = check
    return engine


def test_profile_check_unavailable_failed_and_odd_values_refuse():
    spec = {"profile_id": "reader"}
    assert _bare_engine(None)._profile_problem(spec) == "profile_check_unavailable"

    def boom(_):
        raise RuntimeError("x")
    assert _bare_engine(boom)._profile_problem(spec) == "profile_check_failed"
    for odd in ("", False, 0, b"x", ["a"]):
        assert _bare_engine(lambda _p, odd=odd: odd)._profile_problem(spec) == "profile_check_invalid"
    assert _bare_engine(lambda _p: None)._profile_problem(spec) is None
    assert _bare_engine(lambda _p: "why")._profile_problem(spec) == "why"
    assert _bare_engine(None)._profile_problem({}) is None  # default: no check needed


def test_default_spec_never_calls_the_checker(env):
    calls = []
    env.runtime._profile_problem = lambda pid: calls.append(pid)

    async def go():
        env.runtime.start(env.run_id, "s", env.specs())
        await finish(env)
    asyncio.run(go())
    assert calls == [] and env.states() == ("succeeded", {"work": "completed"})


def test_profile_refusal_precedes_worktree_recheck(env):
    seen = []
    runtime = AgentRuntime(env.db, env.blobs, profile=env.profile,
                           worktree_check=lambda r, t, p: seen.append(p) or ("a" * 40, True))
    runtime._park_poll, runtime._cancel_timeout = 0.01, 10
    _profile_run(env, runtime, worktree_head="a" * 40, repo="/srv/repo", base_ref="main")  # no profile row
    assert seen == [] and env.spawns() == []
    (summary,) = env.summaries()
    assert summary["detail"] == "profile_unknown"


def test_named_profile_gated_task_resumes_after_approval(env):
    _register_profile(env, "reader", 1)

    async def go():
        env.runtime.start(env.run_id, "s", {"work": env.spec(approval_step=GATE, profile_id="reader")})
        await env.until(lambda: env.states()[1]["work"] == "awaiting_approval")
        env.decide()
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("succeeded", {"work": "completed"}) and env.spawns() == ["ok"]


def test_gated_task_profile_disabled_before_spawn_fails_with_approval_consumed(env):
    _register_profile(env, "reader", 1)

    async def go():
        env.runtime.start(env.run_id, "s", {"work": env.spec(approval_step=GATE, profile_id="reader")})
        await env.until(lambda: env.states()[1]["work"] == "awaiting_approval")
        env.db.execute("UPDATE profiles SET enabled=0 WHERE id='reader'")
        env.db.commit()
        env.decide()
        await finish(env)
    asyncio.run(go())
    assert env.spawns() == [] and env.states()[1] == {"work": "failed"}
    (summary,) = env.summaries()
    assert summary["detail"] == "profile_disabled"


def _second_exe(env):
    """A wrapper that marks the worktree, then runs the normal fake: proves which executable was spawned."""
    wrapper = env.exe.parent / "claude-second"
    wrapper.write_text('#!/bin/sh\necho second >> "$PWD/second.log"\nexec %s "$@"\n' % env.exe)
    wrapper.chmod(0o755)
    return wrapper


def _selected(env):
    return [json.loads(r[0]) for r in env.db.execute(
        "SELECT payload_json FROM events WHERE type='agent.profile_selected' ORDER BY rowid")]


def test_named_profile_spawns_its_own_executable_and_records_selection(env):
    from synapse.daemon.profile_registry import ProfileRegistry, ProfileSpec
    _register_profile(env, "reader", 1)
    second = _second_exe(env)
    env.runtime.registry = ProfileRegistry(
        (ProfileSpec("reader", "claude", str(second), ("Read", "Edit"), 1, True),), primary="reader")

    async def go():
        env.runtime.start(env.run_id, "s", env.specs(profile_id="reader"))
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("succeeded", {"work": "completed"})
    assert (env.worktrees["work"] / "second.log").read_text().split() == ["second"]  # not the default executable
    (event,) = _selected(env)
    assert event["profile_id"] == "reader" and event["requested"] == "reader" and event["reason"] == "requested"
    assert event["provider"] == "claude" and event["model"] is None and event["fell_back_from"] is None
    assert event["profile_sha256"] == "x"
    assert env.db.execute("SELECT assigned_profile_id FROM tasks WHERE run_id=?", (env.run_id,)).fetchone() == ("reader",)


def test_default_task_records_selection_without_a_requested_profile(env):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs())
        await finish(env)
    asyncio.run(go())
    (event,) = _selected(env)
    assert event["profile_id"] == at.AGENT_PROFILE and event["requested"] is None and event["reason"] == "default"
    assert not (env.worktrees["work"] / "second.log").exists()


def test_named_profile_without_a_registry_never_falls_back_to_the_default_executable(env):
    _register_profile(env, "reader", 1)
    env.runtime.registry = None  # the DB row exists but nothing can resolve an executable for it

    async def go():
        env.runtime.start(env.run_id, "s", env.specs(profile_id="reader"))
        await finish(env)
    asyncio.run(go())
    assert env.spawns() == []
    run, tasks = env.states()
    assert tasks == {"work": "failed"} and run != "succeeded"


def test_disabled_named_profile_is_not_stuck_unclaimable(env):
    _register_profile(env, "reader", 0)

    async def go():
        env.runtime.start(env.run_id, "s", env.specs(profile_id="reader"))
        await finish(env)  # finishes: it claims under the run's profile and fails closed before spawning
    asyncio.run(go())
    assert env.spawns() == [] and env.states()[1] == {"work": "failed"}
    assert _selected(env) == []  # refused before any selection was recorded


def _refused(env, detail):
    assert env.spawns() == [] and env.states()[1] == {"work": "failed"}
    (summary,) = env.summaries()
    assert summary["outcome"] == "failed" and summary["detail"] == detail
    assert _selected(env) == []  # refused before any selection was recorded


@pytest.mark.parametrize("narrow", [
    dict(allowed_tools=("Read",)),            # Edit removed after the task was accepted
    dict(permission_mode="plan"),             # mode ceiling lowered
    dict(max_timeout_seconds=5.0),            # timeout ceiling lowered
])
def test_profile_narrowed_after_acceptance_fails_closed_at_spawn(env, narrow):
    from dataclasses import replace
    _register_profile(env, "reader", 1)
    env.runtime.registry = type(env.runtime.registry)(
        (replace(env.runtime.registry.get("reader"), **narrow),), primary="reader")

    async def go():
        env.runtime.start(env.run_id, "s", env.specs(profile_id="reader"))  # Edit, acceptEdits, 20s
        await finish(env)
    asyncio.run(go())
    _refused(env, "profile_narrowed")


def test_assignment_mismatch_between_claim_and_spawn_fails_closed(env, monkeypatch):
    from synapse.orchestration.agent_engine import AgentWorkflowEngine
    _register_profile(env, "reader", 1)
    # Simulate "claimed under the run's row, the named profile is usable again at spawn".
    monkeypatch.setattr(AgentWorkflowEngine, "_profile_for", lambda self, run_id, task_id, default: default)

    async def go():
        env.runtime.start(env.run_id, "s", env.specs(profile_id="reader"))
        await finish(env)
    asyncio.run(go())
    _refused(env, "profile_assignment_mismatch")
    assert env.db.execute("SELECT assigned_profile_id FROM tasks WHERE run_id=?", (env.run_id,)).fetchone() == (at.AGENT_PROFILE,)


def test_selection_that_cannot_be_recorded_spawns_nothing(env, monkeypatch):
    from synapse.orchestration.agent_engine import AgentWorkflowEngine

    def boom(self, *args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(AgentWorkflowEngine, "_record_profile_selected", boom)

    async def go():
        env.runtime.start(env.run_id, "s", env.specs())
        await finish(env)
    asyncio.run(go())
    assert env.spawns() == [] and env.states()[1] == {"work": "failed"}
    assert env.summaries()[0]["detail"] == "profile_selection_not_recorded"


def test_cooling_profile_fails_the_task_closed_without_spawning_or_waiting(env):
    _register_profile(env, "reader", 1)
    env.runtime.cooldowns.set("reader", 300)

    async def go():
        env.runtime.start(env.run_id, "s", env.specs(profile_id="reader"))
        await finish(env)
    asyncio.run(go())
    _refused(env, "profile_cooling_down")


def test_cleared_cooldown_lets_the_profile_run_again(env):
    _register_profile(env, "reader", 1)
    env.runtime.cooldowns.set("reader", 300)
    env.runtime.cooldowns.clear("reader")

    async def go():
        env.runtime.start(env.run_id, "s", env.specs(profile_id="reader"))
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("succeeded", {"work": "completed"})


# ------------------------------------------------------------------ M5.3 review fixes (A's findings on #1)
def _run(env, prompt):
    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt=prompt))
        await finish(env)
    asyncio.run(go())


def test_a_large_reduction_is_clipped_so_a_succeeded_attempt_still_gets_a_summary(env):
    _run(env, "errors")
    assert env.states() == ("succeeded", {"work": "completed"})  # before the fix: run recovering, task blocked
    (summary,) = env.summaries()
    (raw_row,) = _only(env, "agent_raw_output")
    assert summary["clipped"] is True and summary["output_truncated"] is True
    assert len(summary["output_text"].encode()) <= 32 * 1024 and "[clipped" in summary["output_text"]
    assert summary["reduction"]["over_budget"] is True and summary["reduction"]["kept_failures"] >= 100
    assert summary["raw_artifact_id"] == raw_row[0] and summary["raw_sha256"] == raw_row[1]  # digest kept, full text in raw
    assert b"module_2999" in env.runtime.blobs.with_max_bytes(4 * 1024 * 1024).read(raw_row[1])


def test_a_large_reduction_on_a_failed_attempt_still_records_its_summary(env):
    _run(env, "errors-fail")
    assert env.states()[1] == {"work": "failed"}
    (summary,) = env.summaries()  # before the fix: no summary, only a log line
    assert summary["outcome"] != "succeeded" and summary["clipped"] is True and summary["raw_artifact_id"]
    assert len(summary["output_text"].encode()) <= 32 * 1024


def test_a_nul_byte_does_not_hide_failure_lines_and_the_raw_artifact_keeps_the_real_bytes(env):
    _run(env, "nul")
    (summary,) = env.summaries()
    (raw_row,) = _only(env, "agent_raw_output")
    raw = env.runtime.blobs.with_max_bytes(4 * 1024 * 1024).read(raw_row[1])
    assert b"\x00" in raw and hashlib.sha256(raw).hexdigest() == summary["raw_sha256"]
    assert summary["nul_replaced"] is True and "binary output omitted" not in summary["output_text"]
    assert "test_n.py::test_nul - AssertionError: after nul" in summary["output_text"]


def test_output_over_the_4_mib_cap_fails_the_attempt_and_stores_a_capped_raw_artifact(env):
    _run(env, "huge")
    assert env.states()[1] == {"work": "failed"}  # the provider's output limit stops it; never "succeeded"
    (summary,) = env.summaries()
    (raw_row,) = _only(env, "agent_raw_output")
    assert summary["outcome"] != "succeeded" and summary["output_truncated"] is True
    assert 0 < raw_row[2] <= 4 * 1024 * 1024 and summary["raw_sha256"] == raw_row[1]
    assert summary["raw_artifact_id"] == raw_row[0]
    assert summary["detail"] == "output_limit"


def test_failed_attempt_that_cannot_store_raw_output_keeps_a_capped_summary_without_raw_id(env, monkeypatch):
    class Full:
        def put(self, chunks):
            raise OSError("disk full")
    monkeypatch.setattr(type(env.runtime.blobs), "with_max_bytes", lambda self, n: Full())
    _run(env, "noisy-fail")
    assert env.states()[1] == {"work": "failed"}
    (summary,) = env.summaries()
    assert summary["raw_artifact_id"] is None and summary["raw_sha256"] is None and summary["raw_complete"] is False
    assert "test_c.py::test_three" in summary["output_text"] and _only(env, "agent_raw_output") == []


def test_a_stored_raw_artifact_with_the_wrong_digest_fails_a_succeeded_attempt_closed(env, monkeypatch):
    import synapse.orchestration.agent_engine as engine
    real = engine.publish_artifact
    def lying(db, blobs, **kwargs):
        result = real(db, blobs, **kwargs)
        if kwargs["provenance"]["kind"] == "agent_raw_output":
            result.response["sha256"] = "0" * 64
        return result
    monkeypatch.setattr(engine, "publish_artifact", lying)
    _run(env, "ok")
    assert env.states()[1] == {"work": "blocked"}
    assert env.db.execute("SELECT count(*) FROM verification_results").fetchone() == (0,)


def test_a_capped_capture_is_reported_as_incomplete_in_the_summary_fields(env):
    from synapse.orchestration.agent_engine import AgentWorkflowEngine
    engine = object.__new__(AgentWorkflowEngine)  # only the storage seam is under test, no provider needed
    engine.db, engine._principal = env.db, "agent-engine"
    engine.raw_blobs = env.runtime.blobs.with_max_bytes(4 * 1024 * 1024)
    async def go():
        return await engine._store_raw_and_reduce(env.run_id, "work", "attempt-x", b"line one\nline two\n", True)
    fields = asyncio.run(go())
    assert fields["raw_complete"] is False and fields["output_truncated"] is True and fields["raw_artifact_id"]
    assert fields["raw_sha256"] == hashlib.sha256(b"line one\nline two\n").hexdigest()
