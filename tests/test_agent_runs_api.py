"""Real agent runs through the local API (fake CLI executable, real git, real SQLite; no real claude)."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import httpx
import pytest

from synapse.api.app import create_app
from synapse.daemon.agent_runtime import AgentRuntime
from synapse.daemon.agent_service import AgentRunService, worktree_checker
from synapse.persistence import agent_tasks as at
from synapse.infrastructure.worktrees import WorktreeManager
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.run_snapshot import read_run_snapshot
from synapse.persistence.workflow_store import create_run, publish_workflow
from synapse.providers.claude_cli import ClaudeCliProfile

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 43
FAKE = '''#!{py}
import json, os, sys, time
mode = sys.stdin.read()
open("spawns.log", "a").write(mode.split()[0] + "\\n")
def emit(o): sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
def text(t): emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": t}}]}}}})
emit({{"type": "system", "subtype": "init"}})
if mode.startswith("ok"):
    text("hello from fake")
    emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Edit", "input": {{"secret": "TOPSECRET"}}}}]}}}})
    emit({{"type": "result", "subtype": "success", "is_error": False, "usage": {{"input_tokens": 3, "output_tokens": 4}}}})
elif mode.startswith("hang"):
    time.sleep(60)
elif mode.startswith("fail"):
    sys.exit(3)
'''
GOOD = {"repo": None, "base_ref": "main", "prompt": "ok SENTINEL-PROMPT-TEXT", "allowed_tools": ["Read", "Edit"],
        "timeout_seconds": 20}


def git(cwd, *args):
    env = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


class Env:
    def __init__(self, tmp_path, *, configured=True, enabled=True, policy=None):
        self.tmp = tmp_path
        self.state = tmp_path / "state"
        self.state.mkdir(mode=0o700)
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("hi\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "-m", "init")
        self.other = tmp_path / "other"
        self.other.mkdir()
        git(self.other, "init", "-q", "-b", "main")
        self.repo, self.other = self.repo.resolve(), self.other.resolve()
        self.root = (tmp_path / "wt").resolve()
        self.db = open_database(tmp_path / "db")
        apply_migrations(self.db, ROOT / "migrations")
        self.blobs = tmp_path / "blobs"
        self.blobs.mkdir(mode=0o700)
        exe = tmp_path / "bin" / "claude"
        exe.parent.mkdir()
        exe.write_text(FAKE.format(py=sys.executable))
        exe.chmod(0o755)
        self.exe = exe
        profile = ClaudeCliProfile(executable=str(exe), allowed_tools=("Read", "Edit", "Bash(git status)"), state_dir=str(self.state),
                                   enabled=enabled, term_grace_seconds=0.3, permission_mode="plan")
        self.manager = WorktreeManager([str(self.repo)], str(self.root)) if configured else None
        self.runtime = AgentRuntime(self.db, self.blobs, profile=profile if configured else None, approval_policy=policy,
                                    worktree_check=worktree_checker(self.manager) if configured else None)
        self.runtime._park_poll = 0.01
        self.runtime._cancel_timeout = 10
        self.service = AgentRunService(self.db, self.runtime, self.manager)
        self.app = create_app(token=TOKEN, read_events=lambda *_: [], start_agent_run=self.service.start,
                              read_agent_run=self.service.show, cancel_agent_run=self.service.cancel,
                              abandon_agent_run=self.runtime.abandon,

                              remove_agent_worktree=self.service.remove_worktree,
                              read_run=lambda rid: read_run_snapshot(self.db, rid))

    def body(self, **over):
        body = {**GOOD, "repo": str(self.repo)}
        body.update(over)
        return {k: v for k, v in body.items() if v is not ...}

    def counts(self):
        return {"runs": self.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                "worktrees": sorted(p.name for p in self.root.iterdir() if not p.name.startswith(".")) if self.root.exists() else []}

    def close(self):
        subprocess.run(["pkill", "-9", "-f", str(self.exe)], check=False)
        self.db.close()


def headers(key="k1", **extra):
    return {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": key, "X-Correlation-ID": "trace", **extra}


def drive(env, scenario):
    """Run ``scenario(client)`` on one event loop so the app, runtime and connection share a thread."""
    async def main():
        transport = httpx.ASGITransport(app=env.app, client=None)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            return await scenario(client)
    return asyncio.run(main())


async def wait_state(env, run_id, states, timeout=20):
    for _ in range(int(timeout / 0.02)):
        row = env.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if row and row[0] in states:
            return row[0]
        await asyncio.sleep(0.02)
    raise AssertionError("timeout waiting for %s" % (states,))


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


# ------------------------------------------------------------------ happy path
def test_start_runs_agent_in_worktree_and_show_reports_result(env):
    async def go(c):
        r = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body())
        assert r.status_code == 201, r.text
        assert r.headers["idempotency-replayed"] == "false" and r.headers["x-correlation-id"] == "trace"
        receipt = r.json()
        assert set(receipt) == {"run_id", "task_id", "state", "worktree_path", "repo", "base_ref"}
        assert receipt["state"] == "running" and receipt["worktree_path"].startswith(str(env.root) + "/")
        assert await wait_state(env, receipt["run_id"], {"succeeded", "failed"}) == "succeeded"
        shown = (await c.get("/api/v1/runs/%s/agent" % receipt["run_id"], headers=headers())).json()
        return receipt, shown
    receipt, shown = drive(env, go)
    task = shown["tasks"][0]
    assert shown["run_state"] == "succeeded" and task["state"] == "completed" and task["task_id"] == receipt["task_id"]
    assert task["result"]["outcome"] == "succeeded" and task["result"]["output_text"] == "hello from fake"
    assert task["result"]["tool_names"] == ["Edit"] and "TOPSECRET" not in json.dumps(shown)
    assert task["spec"]["permission_mode"] == "acceptEdits" and task["spec"]["allowed_tools"] == ["Edit", "Read"]
    wt = task["worktree"]
    assert wt["exists"] is True and wt["branch"].startswith("synapse/") and len(wt["head"]) == 40
    assert wt["clean"] is False and wt["changed_paths"] >= 1 and wt["diff_truncated"] is False  # fake wrote spawns.log
    assert (Path(receipt["worktree_path"]) / "spawns.log").read_text() == "ok\n"
    assert "SENTINEL-PROMPT-TEXT" not in json.dumps(shown)  # the prompt is never echoed back
    assert (env.repo / "spawns.log").exists() is False  # the main checkout was never touched


def test_prompt_text_only_in_private_blob_never_in_db_events_or_audit(env):
    async def go(c):
        r = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body())
        await wait_state(env, r.json()["run_id"], {"succeeded", "failed"})
    drive(env, go)
    for table in [r[0] for r in env.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
        for row in env.db.execute('SELECT * FROM "%s"' % table):
            assert "SENTINEL-PROMPT-TEXT" not in repr(row) and TOKEN not in repr(row)
    blobs = [p.read_bytes() for p in env.blobs.rglob("*") if p.is_file()]
    assert sum(b"SENTINEL-PROMPT-TEXT" in b for b in blobs) == 1  # exactly the prompt artifact


def test_idempotent_replay_and_key_reuse_conflict(env):
    async def go(c):
        first = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body())
        again = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body())
        # defaults spelled out are the same request
        explicit = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body(permission_mode="acceptEdits", base_ref="main"))
        other_prompt = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body(prompt="ok different"))
        other_tools = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body(allowed_tools=["Read"]))
        await wait_state(env, first.json()["run_id"], {"succeeded", "failed"})
        late = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body())  # replay after completion
        return first, again, explicit, other_prompt, other_tools, late
    first, again, explicit, other_prompt, other_tools, late = drive(env, go)
    assert first.status_code == 201 and again.status_code == 200 and again.json() == first.json()
    assert again.headers["idempotency-replayed"] == "true" and explicit.json() == first.json() and late.json() == first.json()
    assert other_prompt.status_code == 409 and other_tools.status_code == 409
    assert env.counts()["runs"] == 1 and len(env.counts()["worktrees"]) == 1
    assert (Path(first.json()["worktree_path"]) / "spawns.log").read_text() == "ok\n"  # spawned once


# ---------------------------------------------------------------------- auth
def test_auth_and_header_requirements(env):
    async def go(c):
        out = {}
        out["none"] = await c.post("/api/v1/agent-runs", json=env.body())
        out["wrong"] = await c.post("/api/v1/agent-runs", headers=headers(Authorization="Bearer " + "x" * 43), json=env.body())
        out["origin"] = await c.post("/api/v1/agent-runs", headers=headers(Origin="https://evil"), json=env.body())
        out["nokey"] = await c.post("/api/v1/agent-runs", headers={"Authorization": "Bearer " + TOKEN, "X-Correlation-ID": "t"}, json=env.body())
        out["get"] = await c.get("/api/v1/runs/x/agent")
        out["cancel"] = await c.post("/api/v1/runs/x/agent-cancel", headers={"Idempotency-Key": "k", "X-Correlation-ID": "t"})
        out["badct"] = await c.post("/api/v1/agent-runs", headers=headers(), content="{}")
        return out
    out = drive(env, go)
    assert [out[k].status_code for k in ("none", "wrong", "origin", "get", "cancel")] == [401] * 5
    assert out["nokey"].status_code == 400 and out["badct"].status_code == 415
    assert env.counts() == {"runs": 0, "worktrees": []}


# ------------------------------------------------------------ validation matrix
BAD_BODIES = {
    "repo not allowlisted": {"repo": "OTHER"},
    "repo relative": {"repo": "repo"},
    "repo traversal": {"repo": "REPO/../repo"},
    "repo injection": {"repo": "REPO; touch PWN"},
    "repo subshell": {"repo": "$(touch PWN)"},
    "repo nonexistent": {"repo": "/nonexistent/repo"},
    "base_ref option": {"base_ref": "--output=PWN"},
    "base_ref shell": {"base_ref": "main; touch PWN"},
    "base_ref subshell": {"base_ref": "$(touch PWN)"},
    "base_ref traversal": {"base_ref": "../main"},
    "base_ref newline": {"base_ref": "main\nother"},
    "base_ref space": {"base_ref": "main other"},
    "base_ref reflog": {"base_ref": "main@{1}"},
    "base_ref unknown": {"base_ref": "no-such-branch"},
    "base_ref blank": {"base_ref": ""},
    "base_ref long": {"base_ref": "a" * 300},
    "tool not allowed": {"allowed_tools": ["Read", "Write"]},
    "no tools": {"allowed_tools": []},
    "tools type": {"allowed_tools": "Read"},
    "tool nonstring": {"allowed_tools": [1]},
    "mode bypass": {"permission_mode": "bypassPermissions"},
    "mode auto": {"permission_mode": "auto"},
    "mode unknown": {"permission_mode": "yolo"},
    "mode type": {"permission_mode": 1},
    "timeout zero": {"timeout_seconds": 0},
    "timeout negative": {"timeout_seconds": -1},
    "timeout huge": {"timeout_seconds": 99999},
    "timeout string": {"timeout_seconds": "20"},
    "timeout bool": {"timeout_seconds": True},
    "prompt empty": {"prompt": ""},
    "prompt blank": {"prompt": "   \n"},
    "prompt nul": {"prompt": "hi\u0000there"},
    "prompt too many chars": {"prompt": "x" * 32769},
    "prompt too many bytes": {"prompt": "é" * 20000},
    "prompt surrogate": {"prompt": "\ud800"},
    "prompt type": {"prompt": 5},
    "extra field": {"bypass": True},
    "prompt missing": {"prompt": ...},
    "repo missing": {"repo": ...},
    "tools missing": {"allowed_tools": ...},
    "approval bad operation": {"approval_step": {"operation": "bad op", "target": "x"}},
    "approval missing target": {"approval_step": {"operation": "agent.run"}},
    "approval extra": {"approval_step": {"operation": "a", "target": "b", "policy_version": "2"}},
    "approval ttl zero": {"approval_step": {"operation": "a", "target": "b", "ttl_seconds": 0}},
    "approval ttl huge": {"approval_step": {"operation": "a", "target": "b", "ttl_seconds": 10**7}},
    "approval type": {"approval_step": "yes"},
}


@pytest.mark.parametrize("name", sorted(BAD_BODIES))
def test_validation_matrix_refuses_before_any_side_effect(env, name):
    over = {k: (v.replace("REPO", str(env.repo)).replace("PWN", str(env.tmp / "pwned")) if isinstance(v, str) else v)
            for k, v in BAD_BODIES[name].items()}
    if over.get("repo") == "OTHER":
        over["repo"] = str(env.other)
    async def go(c):
        raw = json.dumps(env.body(**over), ensure_ascii=True)
        return await c.post("/api/v1/agent-runs", headers={**headers(), "Content-Type": "application/json"}, content=raw)
    r = drive(env, go)
    assert r.status_code == 422, (name, r.status_code, r.text)
    assert env.counts() == {"runs": 0, "worktrees": []}
    assert not (env.tmp / "pwned").exists() and not (env.tmp / "pwned").with_suffix("").exists()
    assert not list(env.tmp.glob("pwn*")) and not list(env.repo.glob("pwn*"))
    assert "SENTINEL" not in r.text and str(env.other) not in r.text
    assert env.db.execute("SELECT COUNT(*) FROM idempotency_keys WHERE operation='agent.run.start'").fetchone() == (0,)
    assert env.db.execute("SELECT COUNT(*) FROM workflow_versions").fetchone() == (0,)


def test_duplicate_json_key_and_nan_are_refused(env):
    async def go(c):
        h = {**headers(), "Content-Type": "application/json"}
        dup = await c.post("/api/v1/agent-runs", headers=h, content='{"repo":"a","repo":"b"}')
        nan = await c.post("/api/v1/agent-runs", headers=h, content=json.dumps(env.body()).replace("20", "NaN", 1))
        return dup, nan
    assert [r.status_code for r in drive(env, go)] == [422, 422]
    assert env.counts() == {"runs": 0, "worktrees": []}


def test_dangerous_repo_config_is_refused_without_side_effects(env):
    git(env.repo, "config", "filter.evil.smudge", "touch %s" % (env.tmp / "pwned"))
    async def go(c):
        return await c.post("/api/v1/agent-runs", headers=headers(), json=env.body())
    assert drive(env, go).status_code == 422
    assert env.counts() == {"runs": 0, "worktrees": []} and not (env.tmp / "pwned").exists()


# ------------------------------------------------------------------ not configured
@pytest.mark.parametrize("kwargs", [{"configured": False}, {"enabled": False}])
def test_not_configured_is_a_clear_503_with_no_side_effects(tmp_path, kwargs):
    e = Env(tmp_path, **kwargs)
    try:
        async def go(c):
            r = await c.post("/api/v1/agent-runs", headers=headers(), json=e.body())
            return r
        r = drive(e, go)
        assert r.status_code == 503 and "not configured" in r.json()["message"]
        assert e.db.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
    finally:
        e.close()


def test_repos_without_manager_is_not_configured(tmp_path):
    e = Env(tmp_path)
    try:
        e.service.manager = None
        async def go(c):
            return await c.post("/api/v1/agent-runs", headers=headers(), json=e.body())
        assert drive(e, go).status_code == 503
    finally:
        e.close()


# ---------------------------------------------------------------- cleanup on failure
@pytest.mark.parametrize("failure", ["state_conflict", "spec_error", "crash"])
def test_failure_after_worktree_cleans_up_and_retry_works(env, failure, monkeypatch):
    from synapse.persistence.agent_tasks import AgentSpecError
    from synapse.persistence.state_store import StateConflict
    original = env.runtime.start
    error = {"state_conflict": StateConflict("x"), "spec_error": AgentSpecError("x"), "crash": RuntimeError("SENTINEL-SECRET boom")}[failure]
    def broken(*a, **k):
        assert os.path.isdir(a[2]["agent"]["worktree"])  # the worktree really existed at that point
        raise error
    monkeypatch.setattr(env.runtime, "start", broken)
    async def go(c):
        fail = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body())
        state = {"worktrees": env.counts()["worktrees"],
                 "states": [r[0] for r in env.db.execute("SELECT state FROM runs")],
                 "branches": git(env.repo, "branch", "--list", "synapse/*").strip()}
        monkeypatch.setattr(env.runtime, "start", original)
        retry = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body())
        await wait_state(env, retry.json()["run_id"], {"succeeded", "failed"})
        return fail, state, retry
    fail, state, retry = drive(env, go)
    assert fail.status_code == {"state_conflict": 409, "spec_error": 422, "crash": 500}[failure]
    assert "SENTINEL" not in fail.text
    assert state == {"worktrees": [], "states": ["cancelled"], "branches": ""}  # no worktree, branch or live run left
    assert env.db.execute("SELECT COUNT(*) FROM idempotency_keys WHERE operation='agent.run.start'").fetchone() == (1,)
    assert retry.status_code == 201
    assert sorted(r[0] for r in env.db.execute("SELECT state FROM runs")) == ["cancelled", "succeeded"]
    assert env.db.execute("SELECT t.state FROM tasks t JOIN runs r ON r.id=t.run_id WHERE r.state='cancelled'").fetchone() == ("cancelled",)


def test_cleanup_is_idempotent_when_the_worktree_is_already_gone(env, monkeypatch):
    def broken(run_id, key, specs):
        env.manager.remove(run_id, run_id and next(iter(env.db.execute("SELECT id FROM tasks WHERE run_id=?", (run_id,))))[0], force=True)
        raise RuntimeError("boom")
    monkeypatch.setattr(env.runtime, "start", broken)
    async def go(c):
        return await c.post("/api/v1/agent-runs", headers=headers(), json=env.body())
    assert drive(env, go).status_code == 500
    assert env.counts()["worktrees"] == [] and [r[0] for r in env.db.execute("SELECT state FROM runs")] == ["cancelled"]


def test_worktree_creation_failure_leaves_nothing(env, monkeypatch):
    from synapse.infrastructure.worktrees import WorktreeError
    def broken(*a, **k):
        raise WorktreeError("git exploded")
    monkeypatch.setattr(env.manager, "create", broken)
    async def go(c):
        return await c.post("/api/v1/agent-runs", headers=headers(), json=env.body())
    r = drive(env, go)
    assert r.status_code == 422 and "git exploded" not in r.text
    assert env.counts()["worktrees"] == [] and [r[0] for r in env.db.execute("SELECT state FROM runs")] == ["cancelled"]


# ----------------------------------------------------------- busy / cancel / gate
def test_second_start_while_busy_is_409_and_creates_nothing(env):
    async def go(c):
        first = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body(prompt="hang"))
        second = await c.post("/api/v1/agent-runs", headers=headers("k2"), json=env.body(prompt="ok"))
        replay = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body(prompt="hang"))  # replays even while busy
        cancel = await c.post("/api/v1/runs/%s/agent-cancel" % first.json()["run_id"], headers=headers("c1"))
        await wait_state(env, first.json()["run_id"], {"cancelled"})
        return first, second, replay, cancel
    first, second, replay, cancel = drive(env, go)
    assert first.status_code == 201 and second.status_code == 409 and replay.json() == first.json()
    assert env.counts()["runs"] == 1 and cancel.status_code == 202


def test_cancel_stops_the_agent_and_replays(env):
    async def go(c):
        start = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body(prompt="hang", timeout_seconds=30))
        run_id = start.json()["run_id"]
        for _ in range(500):  # wait for the process to exist
            if (Path(start.json()["worktree_path"]) / "spawns.log").exists():
                break
            await asyncio.sleep(0.02)
        cancel = await c.post("/api/v1/runs/%s/agent-cancel" % run_id, headers=headers("c1"))
        replay = await c.post("/api/v1/runs/%s/agent-cancel" % run_id, headers=headers("c1"))
        again = await c.post("/api/v1/runs/%s/agent-cancel" % run_id, headers=headers("c2"))
        body = await c.post("/api/v1/runs/%s/agent-cancel" % run_id, headers=headers("c3"), content=b"x")
        await wait_state(env, run_id, {"cancelled", "recovering"})
        shown = (await c.get("/api/v1/runs/%s/agent" % run_id, headers=headers())).json()
        return run_id, cancel, replay, again, body, shown
    run_id, cancel, replay, again, body, shown = drive(env, go)
    assert cancel.status_code == 202 and cancel.json() == {"run_id": run_id, "state": "cancellation_requested"}
    assert replay.json() == cancel.json() and replay.headers["idempotency-replayed"] == "true"
    assert again.status_code in (202, 409) and body.status_code == 400
    assert env.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone() == ("cancelled",)
    assert shown["cancellation_requested"] is True and shown["tasks"][0]["state"] == "cancelled"
    assert shown["tasks"][0]["worktree"]["exists"] is True  # retained for the operator to inspect
    assert subprocess.run(["pgrep", "-f", str(env.exe)], capture_output=True).returncode == 1  # process is gone


def test_cancel_unknown_or_non_agent_run_is_409_and_show_is_404(env):
    publish_workflow(env.db, workflow_id="wf", version="1", nodes=[{"id": "n", "dependencies": []}], principal="operator", idempotency_key="p")
    plain = create_run(env.db, workflow_id="wf", version="1", principal="operator", idempotency_key="c").response["run_id"]
    async def go(c):
        return [await c.post("/api/v1/runs/%s/agent-cancel" % rid, headers=headers("c-" + rid)) for rid in ("nope", plain)] + \
               [await c.get("/api/v1/runs/%s/agent" % rid, headers=headers()) for rid in ("nope", plain)]
    codes = [r.status_code for r in drive(env, go)]
    assert codes == [409, 409, 404, 404]


def test_approval_step_parks_the_task_bound_to_the_prompt_hash(env):
    step = {"operation": "agent.run", "target": "repo/main", "ttl_seconds": 600}
    async def go(c):
        r = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body(approval_step=step))
        run_id = r.json()["run_id"]
        for _ in range(500):
            if env.db.execute("SELECT 1 FROM approval_requests").fetchone():
                break
            await asyncio.sleep(0.02)
        shown = (await c.get("/api/v1/runs/%s/agent" % run_id, headers=headers())).json()
        cancel = await c.post("/api/v1/runs/%s/agent-cancel" % run_id, headers=headers("c"))
        await wait_state(env, run_id, {"cancelled", "recovering"})
        return r, shown, cancel
    r, shown, cancel = drive(env, go)
    assert r.status_code == 201
    task = shown["tasks"][0]
    assert task["state"] == "awaiting_approval" and task["result"] is None  # attempt 1 is the spawn-free gate attempt
    assert task["spec"]["approval_step"] == step
    spec = at.load_task_specs(env.db, run_id := r.json()["run_id"])["agent"]
    assert spec["approval_step"]["candidate_sha256"] == at.spec_hash(spec) != hashlib.sha256(GOOD["prompt"].encode()).hexdigest()
    assert (spec["repo"], spec["base_ref"]) == (str(env.repo), "main") and len(spec["worktree_head"]) == 40
    assert env.db.execute("SELECT operation,target,candidate_manifest_sha256 FROM approval_requests").fetchone() == ("agent.run", "repo/main", at.spec_hash(spec))
    assert not (Path(r.json()["worktree_path"]) / "spawns.log").exists()  # nothing spawned before approval
    assert cancel.status_code == 202


# --------------------------------------------------------------------- contracts
def test_openapi_documents_and_validates_agent_endpoints(env):
    from jsonschema import Draft202012Validator
    async def go(c):
        schema = (await c.get("/openapi.json", headers=headers())).json()
        r = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body())
        run_id = r.json()["run_id"]
        shown = await c.get("/api/v1/runs/%s/agent" % run_id, headers=headers())
        await wait_state(env, run_id, {"succeeded", "failed"})
        done = await c.get("/api/v1/runs/%s/agent" % run_id, headers=headers())
        cancel = await c.post("/api/v1/runs/%s/agent-cancel" % run_id, headers=headers("c"))
        return schema, r, shown, done, cancel
    schema, r, shown, done, cancel = drive(env, go)
    def check(path, method, status, body):
        op = schema["paths"][path][method]
        Draft202012Validator(op["responses"][str(status)]["content"]["application/json"]["schema"]).validate(body)
        return op
    op = check("/api/v1/agent-runs", "post", 201, r.json())
    assert {p["name"] for p in op["parameters"]} == {"Idempotency-Key", "X-Correlation-ID"}
    assert op["requestBody"]["content"]["application/json"]["schema"]["properties"]["permission_mode"]["default"] == "acceptEdits"
    assert {"400", "401", "409", "413", "415", "422", "503"} <= set(op["responses"])
    check("/api/v1/agent-runs", "post", 200, r.json())
    flat = done.json()
    assert flat["outcome"] == "succeeded" and flat["summary_text"] == "hello from fake" and flat["tool_names"] == ["Edit"]
    assert flat["worktree"]["status"] in ("clean", "dirty") and isinstance(flat["worktree"]["diff"], str) and flat["worktree"]["truncated"] is False
    check("/api/v1/runs/{run_id}/agent", "get", 200, shown.json())
    check("/api/v1/runs/{run_id}/agent", "get", 200, done.json())
    assert {"404", "503"} <= set(schema["paths"]["/api/v1/runs/{run_id}/agent"]["get"]["responses"])
    if cancel.status_code == 202:
        check("/api/v1/runs/{run_id}/agent-cancel", "post", 202, cancel.json())
    else:
        assert cancel.status_code == 409  # the run had already finished


def test_errors_use_the_common_envelope_without_echoing_input(env):
    async def go(c):
        return await c.post("/api/v1/agent-runs", headers=headers(), json=env.body(prompt="SENTINEL-X", permission_mode="bypassPermissions"))
    r = drive(env, go)
    assert r.status_code == 422 and set(r.json()) == {"code", "message", "details", "correlation_id", "detail"}
    assert "SENTINEL-X" not in r.text and r.headers["cache-control"] == "no-store"


def test_hung_worktree_git_does_not_block_the_event_loop(env, monkeypatch):
    """F5: show() runs git in a worker thread, so a stuck git cannot block cancel/approvals."""
    import time
    real_status = env.manager.status
    async def go(c):
        start = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body(prompt="hang", timeout_seconds=30))
        run_id = start.json()["run_id"]
        for _ in range(500):
            if (Path(start.json()["worktree_path"]) / "spawns.log").exists():
                break
            await asyncio.sleep(0.02)
        monkeypatch.setattr(env.manager, "status", lambda *a, **k: (time.sleep(3), real_status(*a, **k))[1])
        t0 = time.monotonic()
        slow = asyncio.ensure_future(c.get("/api/v1/runs/%s/agent" % run_id, headers=headers()))
        await asyncio.sleep(0.2)
        cancel = await c.post("/api/v1/runs/%s/agent-cancel" % run_id, headers=headers("c1"))
        cancel_took = time.monotonic() - t0
        snap = await c.get("/api/v1/runs/%s" % run_id, headers=headers())
        assert not slow.done()  # the slow git is still running while other requests were served
        assert (await slow).status_code == 200
        await wait_state(env, run_id, {"cancelled", "recovering"})
        return cancel, cancel_took, snap
    cancel, took, snap = drive(env, go)
    assert cancel.status_code == 202 and took < 2.5 and snap.status_code == 200


def test_planted_filter_after_start_is_refused_on_show_and_never_runs(env, tmp_path):
    marker = tmp_path / "ran"
    async def go(c):
        start = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body())
        run_id = start.json()["run_id"]
        await wait_state(env, run_id, {"succeeded", "failed"})
        wt = start.json()["worktree_path"]
        subprocess.run(["git", "config", "filter.x.clean", f"touch {marker}; cat"], cwd=wt, check=True,
                       env={"PATH": os.environ["PATH"], "HOME": "/nonexistent"})
        Path(wt, ".gitattributes").write_text("* filter=x\n")
        Path(wt, "new.txt").write_text("x\n")
        return await c.get("/api/v1/runs/%s/agent" % run_id, headers=headers())
    shown = drive(env, go)
    assert shown.status_code == 200 and not marker.exists()
    assert shown.json()["tasks"][0]["worktree"] == {"exists": False}


# --------------------------------------------------------------------- abandon
def _abandon(c, run_id, version, key="ab1", assertion="no-agent-process-remains", **extra):
    return c.post("/api/v1/runs/%s/agent-abandon" % run_id, headers=headers(key),
                  json={"expected_run_version": version, "assertion": assertion, **extra})


def test_abandon_recovering_run_over_http(env):
    async def go(c):
        start = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body(prompt="noresult x"))
        run_id = start.json()["run_id"]
        await wait_state(env, run_id, {"recovering"})
        version = env.db.execute("SELECT state_version FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        wrong = await _abandon(c, run_id, version, assertion="yes")
        stale = await _abandon(c, run_id, version + 1, key="ab2")
        extra = await _abandon(c, run_id, version, key="ab3", surprise=1)
        ok = await _abandon(c, run_id, version)
        replay = await _abandon(c, run_id, version)
        gone = await _abandon(c, "no-such-run", 0, key="ab4")
        return run_id, wrong, stale, extra, ok, replay, gone
    run_id, wrong, stale, extra, ok, replay, gone = drive(env, go)
    assert wrong.status_code == 422 and extra.status_code == 422 and stale.status_code == 409 and gone.status_code == 409
    assert ok.status_code == 200 and ok.json() == {"run_id": run_id, "state": "failed", "cancelled_tasks": 1}
    assert replay.status_code == 200 and replay.headers["idempotency-replayed"] == "true"
    assert env.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone() == ("failed",)
    assert (env.root / run_id).exists()  # the worktree is kept for inspection


def test_abandon_is_documented_and_needs_auth(env):
    async def go(c):
        anon = await c.post("/api/v1/runs/x/agent-abandon", headers={"Idempotency-Key": "k", "X-Correlation-ID": "t"},
                            json={"expected_run_version": 0, "assertion": "no-agent-process-remains"})
        return anon, (await c.get("/openapi.json", headers=headers())).json()
    anon, schema = drive(env, go)
    assert anon.status_code in (401, 403) and "post" in schema["paths"]["/api/v1/runs/{run_id}/agent-abandon"]


def test_a_recovering_run_does_not_block_a_new_start(env):
    """Pins the REAL behaviour documented in agent-runs.md (the first draft of the doc claimed the opposite)."""
    async def go(c):
        first = await c.post("/api/v1/agent-runs", headers=headers("k1"), json=env.body(prompt="noresult x"))
        await wait_state(env, first.json()["run_id"], {"recovering"})
        second = await c.post("/api/v1/agent-runs", headers=headers("k2"), json=env.body())
        await wait_state(env, second.json()["run_id"], {"succeeded", "failed", "recovering"})
        return first, second
    first, second = drive(env, go)
    assert second.status_code == 201 and second.json()["run_id"] != first.json()["run_id"]
    assert env.db.execute("SELECT state FROM runs WHERE id=?", (first.json()["run_id"],)).fetchone() == ("recovering",)

# ------------------------------------------------------------ worktree removal
def _remove(c, run_id, force=False, key="r1"):
    return c.post("/api/v1/runs/%s/agent-worktree-remove%s" % (run_id, "?force=true" if force else ""), headers=headers(key))


def test_remove_worktree_refuses_dirty_then_force_removes_and_audits(env):
    async def go(c):
        start = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body())
        run_id, path = start.json()["run_id"], start.json()["worktree_path"]
        await wait_state(env, run_id, {"succeeded"})
        dirty = await _remove(c, run_id)  # the fake agent leaves spawns.log: uncommitted work
        still = Path(path).exists()
        forced = await _remove(c, run_id, force=True, key="r2")
        gone, again = not Path(path).exists(), await _remove(c, run_id, force=True, key="r3")
        return run_id, dirty, still, forced, gone, again
    run_id, dirty, still, forced, gone, again = drive(env, go)
    assert dirty.status_code == 409 and still is True and "uncommitted" in dirty.json()["detail"]
    assert forced.status_code == 200 and forced.json()["removed"] is True and forced.json()["run_id"] == run_id and gone is True
    assert again.status_code == 200 and again.json()["removed"] is False  # idempotent
    rows = env.db.execute("SELECT payload_json FROM events WHERE run_id=? AND type='agent.worktree_removed'", (run_id,)).fetchall()
    assert len(rows) == 1 and json.loads(rows[0][0])["forced"] is True  # only the real removal is audited
    assert subprocess.run(["git", "-C", str(env.repo), "branch", "--list", "synapse/*"], capture_output=True, text=True).stdout == ""


def test_remove_worktree_refuses_an_active_run_and_unknown_or_non_agent_runs(env):
    async def go(c):
        start = await c.post("/api/v1/agent-runs", headers=headers(), json=env.body(prompt="hang", timeout_seconds=30))
        run_id, path = start.json()["run_id"], start.json()["worktree_path"]
        active = await _remove(c, run_id, force=True)
        unknown = await _remove(c, "no-such-run")
        bad_force = await c.post("/api/v1/runs/%s/agent-worktree-remove?force=maybe" % run_id, headers=headers("r9"))
        body = await c.post("/api/v1/runs/%s/agent-worktree-remove" % run_id, headers=headers("r8"), content=b"x")
        await c.post("/api/v1/runs/%s/agent-cancel" % run_id, headers=headers("c1"))
        await wait_state(env, run_id, {"cancelled", "recovering"})
        return path, active, unknown, bad_force, body
    path, active, unknown, bad_force, body = drive(env, go)
    assert active.status_code == 409 and Path(path).exists()  # force never overrides an unfinished run
    assert unknown.status_code == 404 and bad_force.status_code == 422 and body.status_code == 400


def test_remove_worktree_needs_auth_and_is_documented(env):
    async def go(c):
        anon = await c.post("/api/v1/runs/x/agent-worktree-remove", headers={"Idempotency-Key": "k", "X-Correlation-ID": "t"})
        schema = (await c.get("/openapi.json", headers=headers())).json()
        return anon, schema
    anon, schema = drive(env, go)
    assert anon.status_code in (401, 403)
    assert "post" in schema["paths"]["/api/v1/runs/{run_id}/agent-worktree-remove"]


def test_remove_worktree_unconfigured_is_503(tmp_path):
    e = Env(tmp_path, configured=False)
    try:
        r = drive(e, lambda c: _remove(c, "r"))
        assert r.status_code == 503
    finally:
        e.close()


def _two_task_finished_run(env, monkeypatch):
    """A finished 'agent' run with two tasks and a real worktree each (multi-task start lives in a later PR)."""
    from synapse.persistence import agent_tasks as at_mod
    from synapse.persistence.workflow_store import create_run, publish_workflow
    publish_workflow(env.db, workflow_id="wf.two", version="1", nodes=[{"id": "a", "dependencies": []}, {"id": "b", "dependencies": []}],
                     principal="t", idempotency_key="wf-two")
    made = create_run(env.db, workflow_id="wf.two", version="1", principal="t", idempotency_key="run-two").response
    run_id, tasks = made["run_id"], made["tasks"]
    env.db.execute("UPDATE runs SET state='succeeded' WHERE id=?", (run_id,))
    paths = {n: env.manager.create(run_id, t, str(env.repo), "main") for n, t in tasks.items()}
    monkeypatch.setattr(at_mod, "is_agent_run", lambda db, rid: rid == run_id)
    return run_id, tasks, paths


def test_remove_is_all_or_nothing_for_a_dirty_sibling_task(env, monkeypatch):
    run_id, tasks, paths = _two_task_finished_run(env, monkeypatch)
    (Path(paths["b"]) / "wip.txt").write_text("unreviewed\n")  # task b is dirty; task a is clean
    with pytest.raises(Exception) as exc:
        asyncio.run(env.service.remove_worktree(run_id, force=False))
    assert getattr(exc.value, "status", None) == 409
    assert Path(paths["a"]).exists() and Path(paths["b"]).exists()  # nothing was deleted, not even the clean one
    assert env.db.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND type='agent.worktree_removed'", (run_id,)).fetchone() == (0,)
    result = asyncio.run(env.service.remove_worktree(run_id, force=True))
    assert result["removed"] is True and {t["task_id"]: t["removed"] for t in result["tasks"]} == {tasks["a"]: True, tasks["b"]: True}
    assert not Path(paths["a"]).exists() and not Path(paths["b"]).exists()
    ev = env.db.execute("SELECT payload_json FROM events WHERE run_id=? AND type='agent.worktree_removed'", (run_id,)).fetchall()
    assert len(ev) == 1 and sorted(json.loads(ev[0][0])["task_ids"]) == sorted(tasks.values()) and json.loads(ev[0][0])["forced"] is True
    again = asyncio.run(env.service.remove_worktree(run_id, force=True))
    assert again["removed"] is False and all(t["removed"] is False for t in again["tasks"])


def test_a_failure_after_a_partial_removal_still_audits_what_was_removed(env, monkeypatch):
    from synapse.infrastructure.worktrees import WorktreeError
    run_id, tasks, paths = _two_task_finished_run(env, monkeypatch)
    real = env.manager.remove
    order = sorted(tasks.values())

    def flaky(rid, tid, force=False):
        if tid == order[1]:
            raise WorktreeError("simulated failure after the first removal")
        return real(rid, tid, force)
    monkeypatch.setattr(env.manager, "remove", flaky)
    with pytest.raises(Exception) as exc:
        asyncio.run(env.service.remove_worktree(run_id, force=True))
    assert getattr(exc.value, "status", None) == 409
    first, second = order
    assert not any(Path(p).exists() for n, p in paths.items() if tasks[n] == first)  # the first was removed
    assert any(Path(p).exists() for n, p in paths.items() if tasks[n] == second)
    ev = env.db.execute("SELECT payload_json FROM events WHERE run_id=? AND type='agent.worktree_removed'", (run_id,)).fetchall()
    assert len(ev) == 1 and json.loads(ev[0][0])["task_ids"] == [first]  # audited exactly what was removed


# ------------------------------------------------------------------ multi-task runs
def _task(tid, prompt="ok x", deps=(), **extra):
    return {"id": tid, "prompt": prompt, "allowed_tools": ["Read", "Edit"], "depends_on": list(deps), **extra}


def _multi(env, *tasks, **extra):
    return {"repo": str(env.repo), "base_ref": "main", "tasks": list(tasks), "timeout_seconds": 20, **extra}


def test_multi_task_run_executes_a_dependency_graph_in_separate_worktrees(env):
    async def go(c):
        r = await c.post("/api/v1/agent-runs", headers=headers(), json=_multi(
            env, _task("a"), _task("b"), _task("c", "ok y", deps=("a", "b"))))
        assert r.status_code == 201, r.text
        run_id = r.json()["run_id"]
        await wait_state(env, run_id, {"succeeded", "failed", "recovering"})
        shown = (await c.get("/api/v1/runs/%s/agent" % run_id, headers=headers())).json()
        replay = await c.post("/api/v1/agent-runs", headers=headers(), json=_multi(
            env, _task("a"), _task("b"), _task("c", "ok y", deps=("a", "b"))))
        return r, shown, replay
    r, shown, replay = drive(env, go)
    body = r.json()
    assert set(body) == {"run_id", "state", "repo", "base_ref", "tasks"} and "worktree_path" not in body
    assert [t["id"] for t in body["tasks"]] == ["a", "b", "c"]
    paths = [t["worktree_path"] for t in body["tasks"]]
    assert len(set(paths)) == 3 and all(Path(p).is_dir() and p.startswith(str(env.root) + "/") for p in paths)
    assert shown["run_state"] == "succeeded" and sorted(t["node_id"] for t in shown["tasks"]) == ["a", "b", "c"]
    assert all(t["state"] == "completed" and t["result"]["outcome"] == "succeeded" for t in shown["tasks"])
    assert all((Path(p) / "spawns.log").exists() for p in paths)  # each task ran in its own worktree
    assert replay.status_code == 200 and replay.json() == body and replay.headers["idempotency-replayed"] == "true"


def test_dependent_task_does_not_start_when_a_prerequisite_has_no_outcome(env):
    async def go(c):
        r = await c.post("/api/v1/agent-runs", headers=headers(), json=_multi(
            env, _task("a", "noresult x"), _task("c", "ok y", deps=("a",))))
        run_id = r.json()["run_id"]
        await wait_state(env, run_id, {"recovering", "failed", "succeeded"})
        return r.json(), env.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    body, state = drive(env, go)
    assert state == "recovering"
    c_path = Path(next(t for t in body["tasks"] if t["id"] == "c")["worktree_path"])
    assert not (c_path / "spawns.log").exists()  # the dependent never ran


@pytest.mark.parametrize("name,tasks", [
    ("cycle", [_task("a", deps=("b",)), _task("b", deps=("a",))]),
    ("self", [_task("a", deps=("a",))]),
    ("unknown_dep", [_task("a", deps=("zzz",))]),
    ("duplicate_ids", [_task("a"), _task("a")]),
    ("bad_id", [_task("../x")]),
    ("duplicate_dep", [_task("a"), _task("b", deps=("a", "a"))]),
    ("tool_outside_operator_allowlist", [_task("a", allowed_tools=["Bash"])]),
    ("bad_mode", [_task("a", permission_mode="bypassPermissions")]),
])
def test_multi_task_validation_refuses_before_any_side_effect(env, name, tasks):
    r = drive(env, lambda c: c.post("/api/v1/agent-runs", headers=headers(), json=_multi(env, *tasks)))
    assert r.status_code in (400, 422), (name, r.text)
    assert env.counts() == {"runs": 0, "worktrees": []}


def test_multi_task_shape_rules(env):
    async def go(c):
        seven = await c.post("/api/v1/agent-runs", headers=headers("k7"), json=_multi(env, *[_task("t%d" % i) for i in range(7)]))
        both = await c.post("/api/v1/agent-runs", headers=headers("kb"), json={**_multi(env, _task("a")), "prompt": "ok"})
        neither = await c.post("/api/v1/agent-runs", headers=headers("kn"), json={"repo": str(env.repo), "base_ref": "main"})
        empty = await c.post("/api/v1/agent-runs", headers=headers("ke"), json=_multi(env))
        extra = await c.post("/api/v1/agent-runs", headers=headers("kx"), json=_multi(env, {**_task("a"), "surprise": 1}))
        return seven, both, neither, empty, extra
    for r in drive(env, go):
        assert r.status_code == 422
    assert env.counts() == {"runs": 0, "worktrees": []}


def test_cancel_stops_every_task_of_a_multi_task_run(env):
    async def go(c):
        r = await c.post("/api/v1/agent-runs", headers=headers(), json=_multi(
            env, _task("a", "hang x"), _task("b", "hang y"), _task("c", "ok z", deps=("a",)), timeout_seconds=30))
        run_id, paths = r.json()["run_id"], [t["worktree_path"] for t in r.json()["tasks"]]
        for _ in range(500):
            if all((Path(p) / "spawns.log").exists() for p in paths[:2]):
                break
            await asyncio.sleep(0.02)
        await c.post("/api/v1/runs/%s/agent-cancel" % run_id, headers=headers("c1"))
        await wait_state(env, run_id, {"cancelled", "recovering"})
        return run_id, paths
    run_id, paths = drive(env, go)
    assert env.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone() == ("cancelled",)
    assert {s for (s,) in env.db.execute("SELECT state FROM tasks WHERE run_id=?", (run_id,))} == {"cancelled"}
    assert not (Path(paths[2]) / "spawns.log").exists()  # the dependent never started
    assert subprocess.run(["pgrep", "-f", str(env.exe)], capture_output=True).returncode == 1


# ------------------------------------------------- failed prerequisites vs unknown outcomes
def _states(env, run_id):
    return dict(env.db.execute("SELECT node_id,state FROM tasks WHERE run_id=?", (run_id,)).fetchall())


def _spawned(body, node):
    path = Path(next(t for t in body["tasks"] if t["id"] == node)["worktree_path"]) / "spawns.log"
    return path.read_text().split() if path.exists() else []


def _run_state(env, run_id):
    return env.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()[0]


def _run_multi(env, *tasks):
    async def go(c):
        r = await c.post("/api/v1/agent-runs", headers=headers(), json=_multi(env, *tasks))
        assert r.status_code == 201, r.text
        await wait_state(env, r.json()["run_id"], {"succeeded", "failed", "recovering", "cancelled"})
        return r.json()
    return drive(env, go)


def test_failed_prerequisite_fails_the_run_and_cancels_its_dependents(env):
    body = _run_multi(env, _task("a", "fail x"), _task("b", "ok y", deps=("a",)), _task("c", "ok z", deps=("b",)))
    run_id = body["run_id"]
    assert _run_state(env, run_id) == "failed"
    assert _states(env, run_id) == {"a": "failed", "b": "cancelled", "c": "cancelled"}  # transitively
    assert _spawned(body, "a") == ["fail"] and _spawned(body, "b") == [] and _spawned(body, "c") == []  # nothing re-spawned
    attempts = env.db.execute("SELECT a.state FROM task_attempts a JOIN tasks t ON t.id=a.task_id WHERE t.run_id=?", (run_id,)).fetchall()
    assert attempts == [("failed",)]


def test_independent_success_stays_completed_when_another_branch_fails(env):
    body = _run_multi(env, _task("x", "ok x"), _task("a", "fail y"), _task("c", "ok z", deps=("a",)))
    run_id = body["run_id"]
    assert _run_state(env, run_id) == "failed"
    assert _states(env, run_id) == {"x": "completed", "a": "failed", "c": "cancelled"}


def test_diamond_with_one_failed_branch(env):
    body = _run_multi(env, _task("a", "ok a"), _task("b", "fail b", deps=("a",)), _task("d", "ok d", deps=("a",)),
                      _task("c", "ok c", deps=("b", "d")))
    run_id = body["run_id"]
    assert _run_state(env, run_id) == "failed"
    assert _states(env, run_id) == {"a": "completed", "b": "failed", "d": "completed", "c": "cancelled"}
    assert _spawned(body, "c") == []


def test_an_unknown_attempt_still_routes_to_recovery_not_failed(env):
    body = _run_multi(env, _task("a", "noresult x"), _task("b", "fail y"), _task("c", "ok z", deps=("a", "b")))
    run_id = body["run_id"]
    assert _run_state(env, run_id) == "recovering"
    states = _states(env, run_id)
    assert states["a"] == "blocked" and states["b"] == "failed" and states["c"] == "waiting_dependencies"
    assert _spawned(body, "c") == []
    kinds = {r[0] for r in env.db.execute("SELECT a.state FROM task_attempts a JOIN tasks t ON t.id=a.task_id WHERE t.run_id=?", (run_id,))}
    assert "unknown" in kinds


def test_named_profile_is_recorded_in_the_spec_and_default_is_not(env):
    from types import SimpleNamespace
    from synapse.daemon.profile_registry import ProfileSpec
    reader = ProfileSpec("reader", "claude", str(env.exe), ("Edit", "Read"), 1, True, 60.0, None, "acceptEdits")
    env.service.registry = SimpleNamespace(get=lambda pid: reader if pid == "reader" else None)

    async def go(c):
        named = (await c.post("/api/v1/agent-runs", headers=headers("p1"), json=env.body(profile="reader"))).json()
        await wait_state(env, named["run_id"], {"succeeded", "failed"})
        bad = await c.post("/api/v1/agent-runs", headers=headers("p2"), json=env.body(profile="nope"))
        plain = (await c.post("/api/v1/agent-runs", headers=headers("p3"), json=env.body())).json()
        await wait_state(env, plain["run_id"], {"succeeded", "failed"})
        shown = [(await c.get("/api/v1/runs/%s/agent" % r["run_id"], headers=headers())).json() for r in (named, plain)]
        return bad, shown
    bad, (named, plain) = drive(env, go)
    assert bad.status_code == 422 and "nope" not in bad.text
    assert named["tasks"][0]["spec"]["profile"] == "reader"
    assert "profile" not in plain["tasks"][0]["spec"]


def test_stored_profile_id_shape_is_checked_on_load(env):
    from types import SimpleNamespace
    from synapse.daemon.profile_registry import ProfileSpec
    from synapse.persistence import agent_tasks as at
    reader = ProfileSpec("reader", "claude", str(env.exe), ("Edit", "Read"), 1, True, 60.0, None, "acceptEdits")
    env.service.registry = SimpleNamespace(get=lambda pid: reader if pid == "reader" else None)

    async def go(c):
        r = (await c.post("/api/v1/agent-runs", headers=headers("t1"), json=env.body(profile="reader"))).json()
        await wait_state(env, r["run_id"], {"succeeded", "failed"})
        return r["run_id"]
    run_id = drive(env, go)
    specs = at.load_task_specs(env.db, run_id)
    node, spec = next(iter(specs.items()))
    assert spec["profile_id"] == "reader"
    at._check_shape(node, spec)
    for bad in ("default", "a b", "", 3, None):
        with pytest.raises(ValueError):
            at._check_shape(node, {**spec, "profile_id": bad})


def test_per_task_profiles_run_each_task_under_its_own_profile(env):
    from synapse.daemon.profile_registry import ProfileRegistry, ProfileSpec
    reader = ProfileSpec("reader", "claude", str(env.exe), ("Edit", "Read"), 1, True, 60.0, None, "acceptEdits")
    registry = ProfileRegistry((reader,), primary="reader")
    env.service.registry = env.runtime.registry = registry
    env.db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,enabled,max_concurrency,created_at) "
                   "VALUES('reader','claude','p','registry:abc',1,1,'now')")
    env.db.commit()
    tasks = [{"id": "a", "prompt": "ok", "allowed_tools": ["Read"], "profile": "reader"},
             {"id": "b", "prompt": "ok", "allowed_tools": ["Read"], "depends_on": ["a"]}]

    async def go(c):
        made = (await c.post("/api/v1/agent-runs", headers=headers("t1"),
                             json={"repo": str(env.repo), "base_ref": "main", "timeout_seconds": 30, "tasks": tasks})).json()
        assert "run_id" in made, made
        await wait_state(env, made["run_id"], {"succeeded", "failed"})
        bad = await c.post("/api/v1/agent-runs", headers=headers("t2"), json={
            "repo": str(env.repo), "base_ref": "main", "timeout_seconds": 30,
            "tasks": [{"id": "a", "prompt": "ok", "allowed_tools": ["Read"], "profile": "nope"}]})
        shown = (await c.get("/api/v1/runs/%s/agent" % made["run_id"], headers=headers())).json()
        return made, bad, shown
    made, bad, shown = drive(env, go)
    assert bad.status_code == 422 and "nope" not in bad.text
    by_node = {t["node_id"]: t for t in shown["tasks"]}
    assert by_node["a"]["spec"]["profile"] == "reader" and "profile" not in by_node["b"]["spec"]
    assert by_node["a"]["state"] == by_node["b"]["state"] == "completed"
    # the effective profile is reported, from the claim and from the attempt (the spec only holds what was requested)
    assert by_node["a"]["profile_id"] == by_node["a"]["attempt"]["profile_id"] == "reader"
    assert by_node["b"]["profile_id"] == by_node["b"]["attempt"]["profile_id"] == at.AGENT_PROFILE
    picked = {json.loads(r[0])["task_id"]: json.loads(r[0]) for r in env.db.execute(
        "SELECT payload_json FROM events WHERE type='agent.profile_selected'")}
    assert {p["profile_id"] for p in picked.values()} == {"reader", at.AGENT_PROFILE}
    assert env.db.execute("SELECT COUNT(*) FROM events WHERE type='agent.profile_selected'").fetchone() == (2,)
