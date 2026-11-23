"""F6: full-spec approval binding, approver-visible subject, spawn-time worktree recheck, approval policy."""
import asyncio
import copy
import json
import os
import sqlite3

import httpx
import pytest
from jsonschema import Draft202012Validator

from synapse.api.app import create_app
from synapse.daemon.agent_config import AgentConfigError, ApprovalPolicy, load_agent_config
from synapse.daemon.agent_runtime import AgentRuntime
from synapse.persistence import agent_tasks as at
from synapse.persistence.approval_query import read_approvals
from synapse.infrastructure.worktrees import WorktreeError
from synapse.persistence.approval_subject import SUBJECT_PROMPT_BYTES, agent_subject, sanitize_preview
from synapse.persistence.migrations import open_database

import test_agent_runs_api as api
import test_agent_runtime as rt
from test_agent_runtime import GATE, Env, finish

HEAD = "a" * 40
EXTRA = {"repo": "/srv/repo", "base_ref": "main", "worktree_head": HEAD}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def checker(state):
    runtime_calls = state.setdefault("calls", [])
    def check(run_id, task_id, path):
        runtime_calls.append((run_id, task_id))
        if state.get("raise"):
            raise RuntimeError("git exploded")
        return state["head"], state["clean"]
    return check


def runtime_with(env, check, **kw):
    runtime = AgentRuntime(env.db, env.blobs, profile=env.profile, worktree_check=check, **kw)
    runtime._park_poll = 0.01
    runtime._cancel_timeout = 10
    env.runtime = runtime
    return runtime


# ------------------------------------------------------- (1) full-spec binding
BASE = {"prompt_sha256": "b" * 64, "repo": "/r", "base_ref": "main", "worktree": "/w", "worktree_head": HEAD,
        "allowed_tools": ["Edit", "Read"], "permission_mode": "plan", "timeout_seconds": 60}
CHANGES = {"prompt_sha256": "c" * 64, "repo": "/other", "base_ref": "dev", "worktree": "/w2", "worktree_head": "d" * 40,
           "allowed_tools": ["Read"], "permission_mode": "acceptEdits", "timeout_seconds": 61}


@pytest.mark.parametrize("field", sorted(CHANGES))
def test_spec_hash_covers_every_field(field):
    assert at.spec_hash({**BASE, field: CHANGES[field]}) != at.spec_hash(BASE)


def test_spec_hash_is_order_and_number_type_insensitive():
    assert at.spec_hash({**BASE, "allowed_tools": ["Read", "Edit"], "timeout_seconds": 60.0}) == at.spec_hash(BASE)


def test_approval_binds_full_spec_hash_not_prompt_hash(env):
    async def go():
        env.runtime.start(env.run_id, "s", {"work": env.spec(approval_step=GATE, **EXTRA)})
        await env.until(lambda: env.states()[1]["work"] == "awaiting_approval")
        env.runtime._active.cancel()
        await asyncio.gather(env.runtime._active, return_exceptions=True)
    asyncio.run(go())
    spec = at.load_task_specs(env.db, env.run_id)["work"]
    bound = env.db.execute("SELECT candidate_manifest_sha256 FROM approval_requests").fetchone()[0]
    assert bound == at.spec_hash(spec) and bound != spec["prompt_sha256"]
    assert (spec["repo"], spec["base_ref"], spec["worktree_head"]) == ("/srv/repo", "main", HEAD)


def _tamper(env, mutate, *, consistent):
    env.db.execute("DROP TRIGGER events_prevent_update")
    row = env.db.execute("SELECT id,payload_json FROM events WHERE type=?", (at.EVENT_TYPE,)).fetchone()
    payload = json.loads(row[1])
    task = payload["tasks"]["work"]
    mutate(task)
    if consistent:  # the attacker also recomputes the step digest and the event checksum
        task["approval_step"]["candidate_sha256"] = at.spec_hash(task)
    payload["sha256"] = at._sha(payload["tasks"])
    env.db.execute("UPDATE events SET payload_json=? WHERE id=?", (json.dumps(payload), row[0]))


def _set(key, value):
    return lambda task: task.__setitem__(key, value)


TAMPERS = {"repo": "/evil", "base_ref": "evil", "worktree": "/tmp/evil", "worktree_head": "e" * 40,
           "allowed_tools": ["Read"], "permission_mode": "plan", "timeout_seconds": 5}


@pytest.mark.parametrize("field", sorted(TAMPERS))
@pytest.mark.parametrize("consistent", [True, False])
def test_tamper_after_approval_fails_closed_for_every_field(env, field, consistent):
    async def first():
        env.runtime.start(env.run_id, "s", {"work": env.spec(approval_step=GATE, **EXTRA)})
        await env.until(lambda: env.states()[1]["work"] == "awaiting_approval")
        env.runtime._active.cancel()
        await asyncio.gather(env.runtime._active, return_exceptions=True)
    asyncio.run(first())
    env.decide()  # a human approved the ORIGINAL spec
    _tamper(env, _set(field, TAMPERS[field]), consistent=consistent)
    env.db.close()
    env.db = open_database(env.path)
    env.runtime = env.new_runtime()
    env.runtime.recover_interrupted()

    async def second():
        if env.runtime.resume_parked():
            await finish(env)
    asyncio.run(second())
    assert env.spawns() == []
    run, tasks = env.states()
    assert run != "succeeded" and tasks["work"] != "completed"
    if consistent:  # binding recheck, not just the checksum, refuses it
        assert (run, tasks["work"]) == ("failed", "failed")
        assert env.db.execute("SELECT COUNT(*) FROM task_attempts WHERE state='running'").fetchone() == (0,)


def test_tampered_prompt_digest_is_untrustworthy(env):
    async def first():
        env.runtime.start(env.run_id, "s", {"work": env.spec(approval_step=GATE)})
        await env.until(lambda: env.states()[1]["work"] == "awaiting_approval")
        env.runtime._active.cancel()
        await asyncio.gather(env.runtime._active, return_exceptions=True)
    asyncio.run(first())
    _tamper(env, _set("prompt_sha256", "f" * 64), consistent=True)
    assert at.load_task_specs(env.db, env.run_id) is None


# ----------------------------------------------- (3) spawn-time worktree recheck
def _gated_run(env, state, then):
    runtime_with(env, checker(state))

    async def go():
        env.runtime.start(env.run_id, "s", {"work": env.spec(approval_step=GATE, **EXTRA)})
        await env.until(lambda: env.states()[1]["work"] == "awaiting_approval")
        then()
        env.decide()
        await finish(env)
    asyncio.run(go())


def test_clean_unchanged_worktree_spawns(env):
    state = {"head": HEAD, "clean": True}
    _gated_run(env, state, lambda: None)
    assert env.states() == ("succeeded", {"work": "completed"}) and env.spawns() == ["ok"]


@pytest.mark.parametrize("change,reason", [
    ({"head": "b" * 40}, "worktree_head_changed_since_approval"),
    ({"clean": False}, "worktree_dirty_since_approval"),
    ({"raise": True}, "worktree_check_failed"),
])
def test_worktree_change_after_approval_refuses_spawn(env, change, reason):
    state = {"head": HEAD, "clean": True}
    _gated_run(env, state, lambda: state.update(change))
    assert env.spawns() == []
    assert env.states() == ("failed", {"work": "failed"})
    assert [s["detail"] for s in env.summaries()] == [reason]


def test_missing_checker_fails_closed(env):
    runtime_with(env, None)

    async def go():
        env.runtime.start(env.run_id, "s", {"work": env.spec(approval_step=GATE, **EXTRA)})
        await env.until(lambda: env.states()[1]["work"] == "awaiting_approval")
        env.decide()
        await finish(env)
    asyncio.run(go())
    assert env.spawns() == [] and [s["detail"] for s in env.summaries()] == ["worktree_check_unavailable"]


# ----------------------------------------------------------- (4) approval policy
def test_runtime_policy_requires_approval_step(env):
    runtime_with(env, None, approval_policy=ApprovalPolicy(modes=frozenset({"acceptEdits"})))

    async def go():
        with pytest.raises(at.AgentSpecError, match="policy"):
            env.runtime.start(env.run_id, "s", env.specs())
    asyncio.run(go())
    assert env.states()[0] == "queued"


@pytest.mark.parametrize("policy,mode,tools,needed", [
    (ApprovalPolicy(), "acceptEdits", ["Edit"], False),
    (ApprovalPolicy(require_all=True), "plan", ["Read"], True),
    (ApprovalPolicy(modes=frozenset({"acceptEdits"})), "plan", ["Read"], False),
    (ApprovalPolicy(modes=frozenset({"acceptEdits"})), "acceptEdits", ["Read"], True),
    (ApprovalPolicy(tools=frozenset({"Edit"})), "plan", ["Read"], False),
    (ApprovalPolicy(tools=frozenset({"Edit"})), "plan", ["Read", "Edit"], True),
])
def test_policy_matrix(policy, mode, tools, needed):
    assert policy.required_for(mode, tools) is needed


def _config(tmp_path, **extra):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"executable": str(exe), "allowed_tools": ["Read", "Edit"], "enabled": True, **extra}))
    path.chmod(0o600)
    state = tmp_path / "st"
    state.mkdir(exist_ok=True)
    return path, state


def test_config_policy_defaults_and_validation(tmp_path):
    path, state = _config(tmp_path)
    assert load_agent_config(path, state).approval_policy == ApprovalPolicy()
    path, state = _config(tmp_path, require_approval=True, require_approval_for_modes=["acceptEdits"],
                          require_approval_for_tools=["Edit"])
    policy = load_agent_config(path, state).approval_policy
    assert policy == ApprovalPolicy(True, frozenset({"acceptEdits"}), frozenset({"Edit"}))
    for bad in ({"require_approval": "yes"}, {"require_approval_for_modes": ["bypassPermissions"]},
                {"require_approval_for_modes": "plan"}, {"require_approval_for_tools": ["Write"]},
                {"require_approval_for_tools": ["Read", "Read"]}):
        path, state = _config(tmp_path, **bad)
        with pytest.raises(AgentConfigError):
            load_agent_config(path, state)


@pytest.fixture
def apienv(tmp_path):
    e = api.Env(tmp_path, policy=ApprovalPolicy(modes=frozenset({"acceptEdits"})))
    yield e
    e.close()


STEP = {"operation": "agent.run", "target": "repo/main", "ttl_seconds": 600}


def test_api_refuses_run_without_approval_when_policy_requires_it(apienv):
    async def go(c):
        refused = await c.post("/api/v1/agent-runs", headers=api.headers(), json=apienv.body())
        plan = await c.post("/api/v1/agent-runs", headers=api.headers("k2"), json=apienv.body(permission_mode="plan"))
        return refused, plan
    refused, plan = api.drive(apienv, go)
    assert refused.status_code == 422 and "approval_step" in refused.json()["message"]
    assert apienv.counts() == {"runs": 1 if plan.status_code == 201 else 0, "worktrees": apienv.counts()["worktrees"]}
    assert plan.status_code == 201  # plan mode is not covered by this policy
    assert len(apienv.counts()["worktrees"]) == 1  # none was created for the refused request


def test_api_accepts_run_with_approval_under_policy(apienv):
    async def go(c):
        r = await c.post("/api/v1/agent-runs", headers=api.headers(), json=apienv.body(approval_step=STEP))
        await c.post("/api/v1/runs/%s/agent-cancel" % r.json()["run_id"], headers=api.headers("c"))
        return r
    assert api.drive(apienv, go).status_code == 201


# ---------------------------------------------------- (2) subject on /approvals
@pytest.fixture
def aenv(tmp_path):
    e = api.Env(tmp_path)
    e.app = create_app(token=api.TOKEN, read_events=lambda *_: [], start_agent_run=e.service.start,
                       read_agent_run=e.service.show, cancel_agent_run=e.service.cancel,
                       read_approvals=lambda after, limit: read_approvals(e.db, after=after, limit=limit,
                                                                          read_blob=e.runtime.blobs.read))
    yield e
    e.close()


def _start_gated(aenv, **over):
    async def go(c):
        r = await c.post("/api/v1/agent-runs", headers=api.headers(), json=aenv.body(approval_step=STEP, **over))
        run_id = r.json()["run_id"]
        for _ in range(500):
            if aenv.db.execute("SELECT 1 FROM approval_requests").fetchone():
                break
            await asyncio.sleep(0.02)
        listed = await c.get("/api/v1/approvals", headers=api.headers())
        anon = await c.get("/api/v1/approvals")
        schema = (await c.get("/openapi.json", headers=api.headers())).json()
        await c.post("/api/v1/runs/%s/agent-cancel" % run_id, headers=api.headers("c"))
        await api.wait_state(aenv, run_id, {"cancelled", "recovering"})
        return r, listed, anon, schema
    return api.drive(aenv, go)


def test_pending_agent_approval_carries_bounded_subject(aenv):
    prompt = "ok SENTINEL-PROMPT-TEXT " + "x" * 20000
    r, listed, anon, schema = _start_gated(aenv, prompt=prompt)
    assert anon.status_code == 401
    row, = listed.json()["approvals"]
    subject = row["subject"]
    assert subject["kind"] == "agent_run" and subject["repo"] == str(aenv.repo) and subject["base_ref"] == "main"
    assert subject["allowed_tools"] == ["Edit", "Read"] and subject["permission_mode"] == "acceptEdits"
    assert subject["timeout_seconds"] == 20 and len(subject["worktree_head"]) == 40
    assert subject["prompt_preview"].startswith("ok SENTINEL-PROMPT-TEXT")
    assert len(subject["prompt_preview"].encode()) == SUBJECT_PROMPT_BYTES and subject["prompt_preview_truncated"] is True
    assert subject["prompt_bytes"] == len(prompt) and subject["prompt_unavailable"] is False
    assert subject["spec_sha256"] == row["candidate_manifest_sha256"]
    assert api.TOKEN not in listed.text
    schema_ref = schema["paths"]["/api/v1/approvals"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    Draft202012Validator(schema_ref).validate(listed.json())


def test_subject_prompt_unavailable_is_reported_not_fatal(aenv):
    def broken(_sha):
        raise OSError("gone")
    aenv.app = create_app(token=api.TOKEN, read_events=lambda *_: [], start_agent_run=aenv.service.start,
                          read_agent_run=aenv.service.show, cancel_agent_run=aenv.service.cancel,
                          read_approvals=lambda after, limit: read_approvals(aenv.db, after=after, limit=limit, read_blob=broken))
    _, listed, _, _ = _start_gated(aenv)
    subject = listed.json()["approvals"][0]["subject"]
    assert subject["prompt_preview"] is None and subject["prompt_unavailable"] is True


def test_decided_row_has_no_preview_and_other_approvals_have_no_subject(aenv):
    _start_gated(aenv)
    aenv.db.execute("DROP TRIGGER IF EXISTS approval_binding_immutable")
    aenv.db.execute("UPDATE approval_requests SET state='approved'")
    row, = read_approvals(aenv.db, read_blob=aenv.runtime.blobs.read)
    assert row["subject"]["prompt_preview"] is None and row["subject"]["repo"] == str(aenv.repo)
    aenv.db.execute("UPDATE approval_requests SET candidate_manifest_sha256=?", ("0" * 64,))
    assert read_approvals(aenv.db, read_blob=aenv.runtime.blobs.read)[0]["subject"] is None


def test_agent_show_exposes_full_spec_hash(aenv):
    async def go(c):
        r = await c.post("/api/v1/agent-runs", headers=api.headers(), json=aenv.body(approval_step=STEP))
        shown = await c.get("/api/v1/runs/%s/agent" % r.json()["run_id"], headers=api.headers())
        await c.post("/api/v1/runs/%s/agent-cancel" % r.json()["run_id"], headers=api.headers("c"))
        await api.wait_state(aenv, r.json()["run_id"], {"cancelled", "recovering"})
        return shown
    spec = api.drive(aenv, go).json()["tasks"][0]["spec"]
    assert spec["spec_sha256"] and spec["repo"] == str(aenv.repo) and spec["base_ref"] == "main"


# ------------------------------------------- review follow-ups: fail-closed identity, caps, sanitising
@pytest.mark.parametrize("missing", ["repo", "base_ref", "worktree_head"])
def test_approval_step_without_worktree_identity_is_rejected(env, missing):
    spec = env.spec(approval_step=GATE, **EXTRA)
    del spec[missing]
    with pytest.raises(at.AgentSpecError, match="worktree_head"):
        env.runtime.start(env.run_id, "s", {"work": spec})
    assert env.states()[0] == "queued"


def test_spec_without_identity_is_fine_when_ungated(env):
    parsed = at.parse_task_specs({"work": env.spec()}, operator_tools=env.profile.allowed_tools,
                                 max_timeout=60, state_dir=env.profile.state_dir)
    assert parsed.specs["work"]["worktree_head"] is None and parsed.specs["work"]["approval_step"] is None


def test_recheck_with_mismatched_worktree_path_fails_closed(tmp_path):
    from synapse.daemon.agent_service import worktree_checker

    class Manager:
        def worktree_path(self, run_id, task_id):
            return "/state/%s/%s" % (run_id, task_id)

        def status(self, run_id, task_id, *, max_bytes):
            raise AssertionError("must not inspect a tree other than the one being spawned in")

    check = worktree_checker(Manager())
    with pytest.raises(WorktreeError, match="differs"):
        check("r", "t", "/somewhere/else")

    from synapse.orchestration.agent_engine import AgentWorkflowEngine
    engine = AgentWorkflowEngine.__new__(AgentWorkflowEngine)
    engine._worktree_check = check
    assert asyncio.run(engine._worktree_problem("r", "t", "/somewhere/else", HEAD)) == "worktree_check_failed"


def test_engine_passes_spec_path_to_recheck(env):
    seen = []
    state = {"head": HEAD, "clean": True}
    inner = checker(state)
    runtime_with(env, lambda r, t, p: (seen.append(p), inner(r, t, p))[1])

    async def go():
        env.runtime.start(env.run_id, "s", {"work": env.spec(approval_step=GATE, **EXTRA)})
        await env.until(lambda: env.states()[1]["work"] == "awaiting_approval")
        env.decide()
        await finish(env)
    asyncio.run(go())
    assert seen == [str(env.worktrees["work"])]


def test_more_than_50_tools_is_rejected_by_spec_parser(env):
    tools = ["Read"] * 51
    with pytest.raises(at.AgentSpecError, match="50"):
        at.parse_task_specs({"work": env.spec(allowed_tools=tools)}, operator_tools=env.profile.allowed_tools,
                            max_timeout=60, state_dir=env.profile.state_dir)


def _subject_for(env, prompt="ok"):
    async def go():
        env.runtime.start(env.run_id, "s", {"work": env.spec(prompt=prompt, approval_step=GATE, **EXTRA)})
        await env.until(lambda: env.states()[1]["work"] == "awaiting_approval")
        env.runtime._active.cancel()
        await asyncio.gather(env.runtime._active, return_exceptions=True)
    asyncio.run(go())
    spec = at.load_task_specs(env.db, env.run_id)["work"]
    return agent_subject(env.db, lambda sha: prompt.encode(), env.run_id, spec["approval_step"]["candidate_sha256"])


def test_subject_flags_tool_truncation_honestly(env, monkeypatch):
    import synapse.persistence.approval_subject as subj
    assert _subject_for(env)["allowed_tools_truncated"] is False
    monkeypatch.setattr(subj, "MAX_SUBJECT_TOOLS", 1)
    s = agent_subject(env.db, None, env.run_id, at.load_task_specs(env.db, env.run_id)["work"]["approval_step"]["candidate_sha256"])
    assert s["allowed_tools"] == ["Edit"] and s["allowed_tools_truncated"] is True


def test_preview_escapes_bidi_and_control_characters(env):
    prompt = "run\u202e evil\u2066x\u2069 \x1b[31mred\x08\x07 keep\ttab\nline \x85 \u2028"
    preview = _subject_for(env, prompt)["prompt_preview"]
    for ch in "\u202e\u2066\u2069\x1b\x08\x07\x85\u2028":
        assert ch not in preview
    assert "\\u202e" in preview and "\\u001b" in preview and "\\u0008" in preview
    assert "keep\ttab\nline" in preview


def test_sanitised_preview_expansion_is_rebounded_and_flagged(env):
    prompt = "\x07" * SUBJECT_PROMPT_BYTES  # each byte escapes to 6; stays within the stored prompt bound
    subject = _subject_for(env, prompt)
    assert len(subject["prompt_preview"].encode()) <= SUBJECT_PROMPT_BYTES
    assert subject["prompt_preview_truncated"] is True
    assert sanitize_preview("plain \u00e9 text") == "plain \u00e9 text"


def test_legacy_pre_binding_event_is_untrusted_not_resumable(env):
    """Pre-upgrade agent runs (no repo/base_ref/worktree_head keys) are not trusted: no spec, no subject."""
    async def go():
        env.runtime.start(env.run_id, "s", {"work": env.spec()})
        await env.until(lambda: env.states()[1]["work"] in {"completed", "running"})
    asyncio.run(go())
    env.db.execute("DROP TRIGGER events_prevent_update")
    row = env.db.execute("SELECT id,payload_json FROM events WHERE type=?", (at.EVENT_TYPE,)).fetchone()
    payload = json.loads(row[1])
    for key in ("repo", "base_ref", "worktree_head"):
        del payload["tasks"]["work"][key]
    payload["sha256"] = at._sha(payload["tasks"])
    env.db.execute("UPDATE events SET payload_json=? WHERE id=?", (json.dumps(payload), row[0]))
    assert at.load_task_specs(env.db, env.run_id) is None and not at.is_agent_run(env.db, env.run_id)
    assert agent_subject(env.db, None, env.run_id, "0" * 64) is None


# ------------------------------------------------------- profile id in the spec (A7b-2)
def test_profile_id_is_bound_only_when_set():
    default = at.spec_hash(BASE)
    assert at.spec_hash({**BASE, "profile_id": None}) == default  # default specs keep their v1 hash
    named = at.spec_hash({**BASE, "profile_id": "reader"})
    assert named != default and named != at.spec_hash({**BASE, "profile_id": "writer"})


def test_parse_task_specs_profile_id(env, tmp_path):
    from synapse.persistence.agent_tasks import AgentSpecError
    wt = tmp_path / "wt"
    (wt / ".git").mkdir(parents=True)
    state = tmp_path / "pstate"
    state.mkdir()
    spec = {"prompt": "p", "worktree": str(wt), "allowed_tools": ["Read"], "permission_mode": "plan",
            "timeout_seconds": 10, **EXTRA}
    kw = dict(operator_tools=("Read",), max_timeout=60, state_dir=str(state))
    ok = at.parse_task_specs({"n": {**spec, "profile_id": "reader"}}, **kw)
    assert ok.specs["n"]["profile_id"] == "reader"
    assert "profile_id" not in at.parse_task_specs({"n": spec}, **kw).specs["n"]
    for bad in ("", "a b", "x" * 65, 3, "../x", "default"):
        with pytest.raises(AgentSpecError, match="profile_id"):
            at.parse_task_specs({"n": {**spec, "profile_id": bad}}, **kw)
