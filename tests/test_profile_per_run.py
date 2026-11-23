"""Profile per run: run-level selection, per-task override, refusals, no fallback, audit, and per-profile
Claude config directories (path only: the daemon never opens a credentials file). Fake CLIs and fake credential
files under tmp_path only; no real claude, no network, never ~/.claude*.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

import pytest

from synapse.daemon.agent_profiles import load_agent_profiles, spec_sha256
from synapse.daemon.agent_runtime import AgentRuntime
from synapse.daemon.profile_registry import ProfileRegistry, ProfileSpec, RegistryError
from synapse.persistence import agent_tasks as at
from synapse.providers.claude_cli import (
    ClaudeCliProfile, ClaudeCliProvider, check_claude_config_dir,
)
from synapse.providers.contracts import ExecutionRequest
from synapse.providers.sandbox_launcher import SandboxUnavailable, build_bwrap_argv

from test_agent_profiles import entry, env as profiles_env, migrated, write  # noqa: F401  (fixture reuse)
from test_agent_runs_api import Env, drive, headers, wait_state
from test_claude_cli_sandbox import needs_bwrap

# The fake CLI also records which credentials it can see through $CLAUDE_CONFIG_DIR (contents are fake markers).
FAKE = '''#!{py}
import json, os, sys
mode = sys.stdin.read()
open("spawns.log", "a").write(mode.split()[0] + "\\n")
def attempt(fn):
    try:
        return "ok:" + str(fn())
    except OSError as e:
        return "err:" + type(e).__name__
cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
creds = attempt(lambda: open(os.path.join(cfg, ".credentials.json")).read())
open("creds.log", "a").write(creds + "\\n")
def emit(o): sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
emit({{"type": "system", "subtype": "init"}})
if mode.startswith("ok"):
    emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": creds}}]}}}})
    emit({{"type": "result", "subtype": "success", "is_error": False}})
else:
    sys.exit(3)
'''


def config_dir(root: Path, name: str, marker: str | None = None, mode=0o700) -> Path:
    d = root / name
    d.mkdir(mode=0o700)
    if marker is not None:
        (d / ".credentials.json").write_text(marker)
        (d / ".credentials.json").chmod(0o600)
    d.chmod(mode)
    return d.resolve()


# ------------------------------------------------------------------ config dir vetting (lstat only)
def test_config_dir_checks(tmp_path):
    good = config_dir(tmp_path, "good", "FAKE-CRED")
    assert check_claude_config_dir(str(good)) == str(good / ".credentials.json")
    assert check_claude_config_dir(str(config_dir(tmp_path, "empty"))).endswith(".credentials.json")  # may be absent
    loose = config_dir(tmp_path, "loose", "x", mode=0o750)
    with pytest.raises(ValueError, match="0700"):
        check_claude_config_dir(str(loose))
    (tmp_path / "file").write_text("x")
    with pytest.raises(ValueError, match="directory"):
        check_claude_config_dir(str(tmp_path / "file"))
    (tmp_path / "link").symlink_to(good)
    with pytest.raises(ValueError, match="symlinks"):
        check_claude_config_dir(str(tmp_path / "link"))
    for bad in ("relative", str(good) + "/", str(good) + "/../good", "/"):
        with pytest.raises(ValueError):
            check_claude_config_dir(bad)
    with pytest.raises(ValueError, match="does not exist"):
        check_claude_config_dir(str(tmp_path / "missing"))
    for avoid in (str(tmp_path), str(good), str(good / "sub")):  # contains, equals, inside
        with pytest.raises(ValueError, match="overlap"):
            check_claude_config_dir(str(good), avoid=(avoid,))
    world = config_dir(tmp_path, "world", "x")
    (world / ".credentials.json").chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        check_claude_config_dir(str(world))
    linked = config_dir(tmp_path, "linked")
    (linked / ".credentials.json").symlink_to(good / ".credentials.json")
    with pytest.raises(ValueError, match="regular file"):
        check_claude_config_dir(str(linked))


def test_agent_config_accepts_three_profiles_with_distinct_config_dirs(profiles_env):
    tmp, state, base = profiles_env
    dirs = {n: config_dir(tmp, n, "FAKE-" + n) for n in ("claude-work", "claude-personal", "claude-personal-ios")}
    reg = load_agent_profiles(write(tmp, [entry(n, claude_config_dir=str(d)) for n, d in dirs.items()]), base)
    assert {s.id: s.claude_config_dir for s in reg.profiles} == {"default": None, **{n: str(d) for n, d in dirs.items()}}
    # the account is part of the profile's identity hash; a profile without a dir keeps its earlier hash
    work = reg.get("claude-work")
    plain = ProfileSpec(*[getattr(work, f) for f in ("id", "provider", "executable", "allowed_tools", "max_concurrency",
                                                     "enabled", "max_timeout_seconds", "model", "permission_mode")])
    assert spec_sha256(work) != spec_sha256(plain)
    (tmp / "blobs").mkdir(mode=0o700)
    runtime = AgentRuntime(migrated(tmp), tmp / "blobs", profile=base.profile, registry=reg)
    assert set(runtime.profile.protected_config_dirs) == {str(d) for d in dirs.values()}
    assert runtime._resolve_profile("claude-personal").claude_config_dir == str(dirs["claude-personal"])
    assert runtime.profile.claude_config_dir is None  # the default never borrows a profile's account


def test_agent_config_refuses_unsafe_or_shared_config_dirs(profiles_env):
    tmp, state, base = profiles_env
    a = config_dir(tmp, "a", "x")
    nested = a / "inner"
    nested.mkdir(mode=0o700)
    cases = [
        ([entry("w", claude_config_dir=str(a)), entry("p", claude_config_dir=str(a))], "distinct"),
        ([entry("w", claude_config_dir=str(a)), entry("p", claude_config_dir=str(nested))], "distinct"),
        ([entry("w", claude_config_dir=str(config_dir(tmp, "open", mode=0o755)))], "0700"),
        ([entry("w", claude_config_dir=str(state))], "overlap"),
        ([entry("w", claude_config_dir=str(tmp / "nope"))], "does not exist"),
        ([entry("w", claude_config_dir="relative/dir")], "absolute"),
        ([entry("w", provider="codex", executable="/bin/sh", claude_config_dir=str(a))], "only supported for a claude"),
    ]
    for profiles, text in cases:
        with pytest.raises(RegistryError, match=text):
            load_agent_profiles(write(tmp, profiles), base)


# ------------------------------------------------------------------ adapter: credentials by path only
@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "scratchtmp"))
    (tmp_path / "scratchtmp").mkdir()
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(FAKE.format(py=sys.executable))
    exe.chmod(0o755)
    wt = tmp_path / "wt"
    (wt / ".git").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return tmp_path, exe, wt, state


def provider(box, environ=None, **kw):
    _, exe, _, state = box
    profile = ClaudeCliProfile(executable=str(exe), allowed_tools=("Read",), state_dir=str(state), enabled=True,
                               term_grace_seconds=0.3, permission_mode="plan", **kw)
    return ClaudeCliProvider(profile, environ=environ if environ is not None else {"PATH": os.environ["PATH"]})


async def _run(prov, wt, prompt="ok"):
    handle = await prov.start(ExecutionRequest("p", "t", {"prompt": prompt, "worktree": str(wt)}))
    return [e async for e in prov.stream(handle)]


def test_profile_credentials_are_linked_never_read_and_never_fall_back(box, monkeypatch):
    tmp, _, wt, _ = box
    work = config_dir(tmp, "work", "FAKE-CRED-WORK")
    other = config_dir(tmp, "env-default", "FAKE-CRED-ENV")
    opened = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: (opened.append(os.fspath(p)), real_open(p, *a, **k))[1])
    prov = provider(box, environ={"PATH": os.environ["PATH"], "CLAUDE_CONFIG_DIR": str(other)},
                    claude_config_dir=str(work), protected_config_dirs=(str(work), str(other)))
    events = asyncio.run(_run(prov, wt))
    assert events[-1].kind == "succeeded", events
    assert (wt / "creds.log").read_text() == "ok:FAKE-CRED-WORK\n"  # the profile's account, not $CLAUDE_CONFIG_DIR's
    assert not any(p.startswith(str(work)) or p.startswith(str(other)) for p in opened)  # daemon never opened them
    # the deny rules cover every profile's config dir
    deny = prov._settings(str(tmp / "scratchtmp"))["permissions"]["deny"]
    assert "Read(/%s/**)" % work in deny and "Read(/%s/**)" % other in deny


def test_daemon_oauth_token_never_overrides_a_profile_account(box):
    tmp, _, wt, _ = box
    environ = {"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": "FAKE-DAEMON-TOKEN"}
    req = ExecutionRequest("p", "t", {"prompt": "ok", "worktree": str(wt)})
    with_dir = provider(box, environ=environ, claude_config_dir=str(config_dir(tmp, "work", "x")))._plan(req)
    without = provider(box, environ=environ)._plan(req)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in with_dir.env and without.env["CLAUDE_CODE_OAUTH_TOKEN"] == "FAKE-DAEMON-TOKEN"


def test_profile_credentials_unreadable_by_daemon_still_reach_child_by_path(box):
    """A 0000 credentials file: copying would fail; linking by path does not need to read it."""
    tmp, _, wt, _ = box
    work = config_dir(tmp, "work", "FAKE")
    (work / ".credentials.json").chmod(0o000)
    if os.access(work / ".credentials.json", os.R_OK):
        pytest.skip("running with CAP_DAC_OVERRIDE")
    events = asyncio.run(_run(provider(box, claude_config_dir=str(work)), wt))
    assert events[-1].kind == "succeeded"
    assert (wt / "creds.log").read_text() == "err:PermissionError\n"  # the child sees the path, the daemon read nothing


def test_missing_or_loosened_profile_credentials_refuse_the_spawn(box):
    tmp, _, wt, _ = box
    empty = config_dir(tmp, "empty")
    prov = provider(box, environ={"PATH": os.environ["PATH"], "CLAUDE_CONFIG_DIR": os.environ["CLAUDE_CONFIG_DIR"]},
                    claude_config_dir=str(empty))
    with pytest.raises(ValueError, match="no credentials file"):  # never the environment's login instead
        asyncio.run(_run(prov, wt))
    work = config_dir(tmp, "work", "x")
    work.chmod(0o755)  # loosened after startup: re-checked before every spawn
    with pytest.raises(ValueError, match="0700"):
        asyncio.run(_run(provider(box, claude_config_dir=str(work)), wt))
    assert not (wt / "spawns.log").exists()


def test_bwrap_file_bind_must_target_scratch(tmp_path):
    common = dict(worktree=str(tmp_path / "wt"), scratch=str(tmp_path / "s"), executable="/usr/bin/true",
                  inner=["/usr/bin/true"], forbidden=[str(tmp_path / "cfg")])
    argv = build_bwrap_argv("/usr/bin/bwrap", **common, ro_files=[(str(tmp_path / "cfg" / "c"), str(tmp_path / "s" / "c"))])
    i = argv.index(str(tmp_path / "cfg" / "c"))
    assert argv[i - 1] == "--ro-bind" and argv[i + 1] == str(tmp_path / "s" / "c")
    assert argv.index("--ro-bind", argv.index(str(tmp_path / "s")) - 1) < i  # after (on top of) the scratch bind
    for dest in (str(tmp_path / "elsewhere"), str(tmp_path / "wt" / "c"), str(tmp_path / "s")):
        with pytest.raises(SandboxUnavailable):
            build_bwrap_argv("/usr/bin/bwrap", **common, ro_files=[(str(tmp_path / "cfg" / "c"), dest)])


@needs_bwrap
def test_sandboxed_child_reads_only_its_own_credentials_read_only(box):
    tmp, exe, wt, _ = box
    work = config_dir(tmp, "work", "FAKE-CRED-WORK")
    personal = config_dir(tmp, "personal", "FAKE-CRED-PERSONAL")
    (work / "settings.json").write_text("{}")
    exe.write_text(FAKE_SANDBOX)
    prov = provider(box, environ={"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")}, sandbox=True,
                    claude_config_dir=str(work), protected_config_dirs=(str(work), str(personal)))
    prompt = "ok\n" + json.dumps({"work": str(work), "personal": str(personal)})
    events = asyncio.run(_run(prov, wt, prompt))
    assert events[-1].kind == "succeeded", events
    out = json.loads(next(e.text for e in events if e.kind == "output"))
    assert out["own"] == "ok:FAKE-CRED-WORK"
    assert out["own_write"].startswith("err:")  # read-only bind: the agent cannot alter the operator's file
    assert out["work_listdir"].startswith("err:") and out["work_settings"].startswith("err:")  # rest of its dir hidden
    assert out["personal_creds"].startswith("err:") and out["personal_listdir"].startswith("err:")
    assert (work / ".credentials.json").read_text() == "FAKE-CRED-WORK"


FAKE_SANDBOX = '''#!/usr/bin/python3
import json, os, sys
lines = sys.stdin.read().split("\\n")
arg = json.loads(lines[1])
def attempt(fn):
    try:
        return "ok:" + str(fn())[:80]
    except OSError as e:
        return "err:" + type(e).__name__
own = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".credentials.json")
out = {"own": attempt(lambda: open(own).read()),
       "own_write": attempt(lambda: open(own, "a").write("x")),
       "work_listdir": attempt(lambda: os.listdir(arg["work"])),
       "work_settings": attempt(lambda: open(os.path.join(arg["work"], "settings.json")).read()),
       "personal_creds": attempt(lambda: open(os.path.join(arg["personal"], ".credentials.json")).read()),
       "personal_listdir": attempt(lambda: os.listdir(arg["personal"]))}
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": json.dumps(out)}]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False}), flush=True)
'''


# ------------------------------------------------------------------ run-level selection through the API
@pytest.fixture
def penv(tmp_path):
    e = Env(tmp_path)
    e.exe.write_text(FAKE.format(py=sys.executable))
    e.dirs = {"claude-work": config_dir(tmp_path, "cfg-work", "FAKE-CRED-WORK"),
              "claude-personal": config_dir(tmp_path, "cfg-personal", "FAKE-CRED-PERSONAL"),
              "claude-personal-ios": config_dir(tmp_path, "cfg-ios", "FAKE-CRED-IOS")}
    specs = tuple(ProfileSpec(pid, "claude", str(e.exe), ("Edit", "Read"), 1, pid != "claude-personal-ios", 60.0, None,
                              "acceptEdits", str(d)) for pid, d in e.dirs.items())
    registry = ProfileRegistry(specs, primary="claude-work")
    e.service.registry = e.runtime.registry = registry
    for spec in specs:
        e.db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,enabled,max_concurrency,created_at) "
                     "VALUES(?,'claude','p',?,?,1,'now')", (spec.id, "registry:" + spec_sha256(spec), int(spec.enabled)))
    e.db.commit()
    yield e
    e.close()


def _multi(env, tasks, **over):
    return {"repo": str(env.repo), "base_ref": "main", "timeout_seconds": 30, "tasks": tasks, **over}


def _task(node, prompt="ok", **kw):
    return {"id": node, "prompt": prompt, "allowed_tools": ["Read"], **kw}


def _events(env, kind, run_id=None):
    sql = "SELECT payload_json FROM events WHERE type=?" + (" AND run_id=?" if run_id else "") + " ORDER BY sequence"
    return [json.loads(r[0]) for r in env.db.execute(sql, (kind, run_id) if run_id else (kind,))]


def test_run_level_profile_applies_to_every_task_with_per_task_override(penv):
    tasks = [_task("a"), _task("b", profile="claude-personal", depends_on=["a"]),
             _task("c", profile="default", depends_on=["a"])]

    async def go(c):
        made = (await c.post("/api/v1/agent-runs", headers=headers("r1"),
                             json=_multi(penv, tasks, profile="claude-work"))).json()
        assert "run_id" in made, made
        assert await wait_state(penv, made["run_id"], {"succeeded", "failed"}) == "succeeded"
        plain = (await c.post("/api/v1/agent-runs", headers=headers("r2"), json=penv.body())).json()
        await wait_state(penv, plain["run_id"], {"succeeded", "failed"})
        shown = [(await c.get("/api/v1/runs/%s/agent" % r["run_id"], headers=headers())).json() for r in (made, plain)]
        return made, plain, shown
    made, plain, (shown, shown_plain) = drive(penv, go)
    assert shown["profile"] == "claude-work" and shown_plain["profile"] == "default"  # agent-show prints this view
    by_node = {t["node_id"]: t for t in shown["tasks"]}
    assert by_node["a"]["spec"]["profile"] == by_node["a"]["attempt"]["profile_id"] == "claude-work"
    assert by_node["b"]["spec"]["profile"] == by_node["b"]["attempt"]["profile_id"] == "claude-personal"
    assert "profile" not in by_node["c"]["spec"] and by_node["c"]["attempt"]["profile_id"] == at.AGENT_PROFILE
    # each task ran as its profile's account (fake markers), the default task with the base login
    paths = {t["id"]: Path(t["worktree_path"]) for t in made["tasks"]}
    assert (paths["a"] / "creds.log").read_text() == "ok:FAKE-CRED-WORK\n"
    assert (paths["b"] / "creds.log").read_text() == "ok:FAKE-CRED-PERSONAL\n"
    assert "FAKE-CRED" not in (paths["c"] / "creds.log").read_text()
    # audit: ids only
    (event,) = _events(penv, "agent.run_profile_selected", made["run_id"])
    assert event == {"run_id": made["run_id"], "profile_id": "claude-work", "requested": "claude-work",
                     "task_profiles": {"a": "claude-work", "b": "claude-personal", "c": "default"},
                     "fallback": None, "principal": "local-operator"}
    (plain_event,) = _events(penv, "agent.run_profile_selected", plain["run_id"])
    assert plain_event["profile_id"] == "default" and plain_event["requested"] is None
    selected = {e["task_id"]: e for e in _events(penv, "agent.profile_selected")}
    assert all(e["fell_back_from"] is None for e in selected.values())
    for table in [r[0] for r in penv.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
        for row in penv.db.execute('SELECT * FROM "%s"' % table):
            assert not any(str(d) in repr(row) for d in penv.dirs.values())  # no config dir path stored anywhere
            assert "FAKE-CRED" not in repr(row)


def test_unknown_disabled_and_cooling_down_profiles_are_refused_before_any_run(penv):
    penv.runtime.cooldowns.set("claude-personal", 600)
    cases = [dict(profile="nope"), dict(profile="claude-personal-ios"), dict(profile="claude-personal")]

    async def go(c):
        out = []
        for i, over in enumerate(cases):
            out.append(await c.post("/api/v1/agent-runs", headers=headers("x%d" % i), json=penv.body(**over)))
        # the same refusals for a task-level profile under a valid run-level one
        for i, over in enumerate(cases):
            out.append(await c.post("/api/v1/agent-runs", headers=headers("y%d" % i),
                                    json=_multi(penv, [_task("a", **over)], profile="claude-work")))
        return out
    responses = drive(penv, go)
    assert [r.status_code for r in responses] == [422] * 6
    details = [r.json()["detail"] for r in responses]
    assert details[0] == details[3] == "unknown profile"
    assert details[1] == details[4] == "profile is disabled"
    assert details[2] == details[5] == "profile is cooling down"
    assert penv.counts() == {"runs": 0, "worktrees": []}  # nothing created, nothing spawned
    assert _events(penv, "agent.run_profile_selected") == []


def test_failed_task_never_falls_back_to_another_profile(penv):
    async def go(c):
        made = (await c.post("/api/v1/agent-runs", headers=headers("f1"),
                             json=_multi(penv, [_task("a", prompt="fail now")], profile="claude-work"))).json()
        await wait_state(penv, made["run_id"], {"succeeded", "failed", "recovering"})
        return made
    made = drive(penv, go)
    attempts = penv.db.execute("SELECT a.profile_id,a.state FROM task_attempts a JOIN tasks t ON t.id=a.task_id "
                               "WHERE t.run_id=?", (made["run_id"],)).fetchall()
    assert attempts and {p for p, _ in attempts} == {"claude-work"}  # every attempt under the chosen profile
    assert {e["profile_id"] for e in _events(penv, "agent.profile_selected")} == {"claude-work"}
    assert penv.db.execute("SELECT state FROM tasks WHERE run_id=?", (made["run_id"],)).fetchone()[0] != "completed"
