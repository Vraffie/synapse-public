"""Opt-in bubblewrap sandbox for the Claude CLI child (ADR 019 option C2, first slice).

Argv-construction, config and fail-closed tests run everywhere. The namespace tests run a
real bwrap with a FAKE agent script (no real claude, no network) and skip with a reason when
bwrap or unprivileged user namespaces are unavailable.
"""
import asyncio
import json
import os
from pathlib import Path
import stat
import tempfile
import time

import pytest

from synapse.daemon import agent_config
from synapse.daemon.agent_config import AgentConfigError, load_agent_config
from synapse.providers import claude_cli, sandbox_launcher
from synapse.providers.claude_cli import ClaudeCliProfile, ClaudeCliProvider
from synapse.providers.contracts import ExecutionRequest
from synapse.providers.sandbox_launcher import (
    SandboxUnavailable, build_bwrap_argv, find_bwrap, git_readonly_dirs, require_sandbox,
)

PY = "/usr/bin/python3"
FAKE = '''#!/usr/bin/python3
import json, os, subprocess, sys, time
prompt = sys.stdin.read().split("\\n")
mode, arg = prompt[0], json.loads(prompt[1]) if len(prompt) > 1 and prompt[1] else {}
def emit(o): sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
def text(t): emit({"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}})
def ok(): emit({"type": "result", "subtype": "success", "is_error": False})
def attempt(fn):
    try:
        return "ok:" + str(fn())[:80]
    except OSError as e:
        return "err:" + type(e).__name__
if mode == "probe":
    pids = [int(n) for n in os.listdir("/proc") if n.isdigit()]
    wt = os.getcwd()
    out = {
        "token": attempt(lambda: open(arg["state"] + "/approver.token").read()),
        "sqlite": attempt(lambda: open(arg["state"] + "/synapse.sqlite", "rb").read()),
        "state_listdir": attempt(lambda: os.listdir(arg["state"])),
        "home_listdir": attempt(lambda: os.listdir(arg["real_home"])),
        "home_env": os.environ.get("HOME"), "cfg_env": os.environ.get("CLAUDE_CONFIG_DIR"),
        "home_write": attempt(lambda: open(os.environ["HOME"] + "/x", "w").write("1")),
        "wt_write": attempt(lambda: open(wt + "/made-by-agent", "w").write("1")),
        "etc_write": attempt(lambda: open("/etc/synapse-x", "w").write("1")),
        "usr_write": attempt(lambda: open("/usr/synapse-x", "w").write("1")),
        "repo_write": attempt(lambda: open(arg["repo"] + "/synapse-x", "w").write("1")) if arg.get("repo") else None,
        "pids": pids, "self_pid": os.getpid(), "cwd": wt,
        "api_env": os.environ.get("ANTHROPIC_API_KEY"),
    }
    text(json.dumps(out)); ok()
elif mode == "git":
    def sh(*a):
        r = subprocess.run(a, capture_output=True, text=True)
        return [r.returncode, r.stdout.strip()[:200], r.stderr.strip()[:200]]
    out = {"log": sh("git", "log", "--oneline"), "diff": sh("git", "diff", "--stat"),
           "status": sh("git", "status", "--short"),
           "config": attempt(lambda: open(arg["config"]).read()),
           "config_write": attempt(lambda: open(arg["config"], "a").write("[x]")),
           "config_unlink": attempt(lambda: os.unlink(arg["config"]))}
    text(json.dumps(out)); ok()
elif mode == "grandchild":
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", arg["token"]], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    text("spawned"); time.sleep(300)
else:
    text("hi"); ok()
'''


def _userns_reason():
    bwrap = find_bwrap()
    if bwrap is None:
        return "bwrap is not installed"
    if not os.path.exists(PY):
        return PY + " is not available for the fake agent"
    try:
        require_sandbox(bwrap)
    except SandboxUnavailable as exc:
        return "unprivileged user namespaces unavailable: %s" % exc
    return None


_REASON = _userns_reason()
needs_bwrap = pytest.mark.skipif(_REASON is not None, reason=_REASON or "")


@pytest.fixture
def box(tmp_path, monkeypatch):
    scratch_tmp = tmp_path / "scratchtmp"
    scratch_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_tmp))
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(FAKE)
    exe.chmod(0o755)
    wt = tmp_path / "wt"
    (wt / ".git").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    (state / "approver.token").write_text("FAKE-APPROVER-TOKEN")
    (state / "approver.token").chmod(0o600)
    (state / "synapse.sqlite").write_bytes(b"SQLite format 3\x00fake")
    return tmp_path, exe, wt, state


def make_profile(box, **kw):
    _, exe, _, state = box
    base = dict(executable=str(exe), allowed_tools=("Read",), state_dir=str(state), enabled=True,
                term_grace_seconds=0.3, sandbox=True)
    base.update(kw)
    return ClaudeCliProfile(**base)


def make_request(box, mode, arg=None):
    return ExecutionRequest("p1", "t1", {"prompt": mode + "\n" + json.dumps(arg or {}), "worktree": str(box[2])})


async def run(provider, req, cancel_after=None):
    handle = await provider.start(req)
    events = []

    async def consume():
        async for e in provider.stream(handle):
            events.append(e)

    task = asyncio.create_task(consume())
    if cancel_after is not None:
        await asyncio.sleep(cancel_after)
        await provider.cancel(handle)
    await asyncio.wait_for(task, 30)
    return events


def host_pids_with(token):
    found = []
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                if token.encode() in Path(f"/proc/{name}/cmdline").read_bytes():
                    found.append(int(name))
            except OSError:
                pass
    return found


# --- run everywhere: profile, config, argv, fail closed -----------------------------------------
def test_sandbox_defaults_off_and_is_validated():
    base = dict(executable="/usr/bin/claude", allowed_tools=("Read",), state_dir="/s")
    assert ClaudeCliProfile(**base).sandbox is False
    assert ClaudeCliProfile(**base, sandbox=True).sandbox is True
    with pytest.raises(ValueError):
        ClaudeCliProfile(**base, sandbox="yes")
    with pytest.raises(ValueError):
        ClaudeCliProfile(**base, sandbox_bwrap="relative/bwrap")


def _config(tmp_path, **extra):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"executable": "/usr/bin/true", "allowed_tools": ["Read"], "enabled": True, **extra}))
    path.chmod(0o600)
    return path


def test_config_sandbox_option(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    assert load_agent_config(_config(tmp_path), state).profile.sandbox is False
    monkeypatch.setattr(agent_config, "find_bwrap", lambda: "/usr/bin/bwrap")
    assert load_agent_config(_config(tmp_path, sandbox=True), state).profile.sandbox is True
    with pytest.raises(AgentConfigError, match="sandbox must be a boolean"):
        load_agent_config(_config(tmp_path, sandbox="yes"), state)
    monkeypatch.setattr(agent_config, "find_bwrap", lambda: None)
    with pytest.raises(AgentConfigError, match="bwrap is not installed"):
        load_agent_config(_config(tmp_path, sandbox=True), state)


def test_argv_construction(tmp_path):
    wt, scratch, state = tmp_path / "wt", tmp_path / "scratch", tmp_path / "state"
    exe = tmp_path / "claude"
    for d in (wt, scratch, state):
        d.mkdir()
    exe.write_text("x")
    inner = [str(exe), "-p", "--output-format", "stream-json"]
    argv = build_bwrap_argv("/usr/bin/bwrap", worktree=str(wt), scratch=str(scratch), executable=str(exe),
                            inner=inner, forbidden=[str(state), "/home/someone"])
    assert argv[0] == "/usr/bin/bwrap" and argv[argv.index("--") + 1:] == inner
    for flag in ("--die-with-parent", "--unshare-pid", "--unshare-ipc", "--unshare-uts"):
        assert flag in argv
    assert "--unshare-net" not in argv and "--unshare-all" not in argv  # owner decision: network allowed
    assert "--clearenv" not in argv  # env flows via the process environment, never this argv
    assert argv[argv.index("--proc") + 1] == "/proc" and argv[argv.index("--chdir") + 1] == str(wt)
    writable = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
    assert writable == [str(scratch), str(wt)]  # the worktree and scratch are the only writable mounts
    assert not any(str(state) in a for a in argv)
    assert not any("/home/" in a for a in argv)
    assert argv.index("--tmpfs") < argv.index(str(scratch))  # /tmp tmpfs first, then the binds on top
    for bad in (str(state), str(state / "sub"), str(tmp_path)):
        with pytest.raises(SandboxUnavailable):
            build_bwrap_argv("/usr/bin/bwrap", worktree=bad, scratch=str(scratch), executable=str(exe),
                             inner=inner, forbidden=[str(state)])


def test_git_readonly_dirs_for_linked_worktree(tmp_path):
    repo, wt = tmp_path / "repo", tmp_path / "wt"
    (repo / ".git" / "worktrees" / "w1").mkdir(parents=True)
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/w1\n")
    assert git_readonly_dirs(str(wt), [str(repo)]) == [str(repo / ".git")]
    with pytest.raises(SandboxUnavailable):  # not an allowlisted repo
        git_readonly_dirs(str(wt), [])
    (wt / ".git").unlink()
    (wt / ".git").mkdir()
    assert git_readonly_dirs(str(wt), []) == []  # a self-contained clone needs no extra mount


def test_provider_wraps_child_argv_only_when_enabled_and_keeps_secrets_out_of_argv(box):
    for sandbox in (False, True):
        prov = ClaudeCliProvider(make_profile(box, sandbox=sandbox, api_key="SECRET-KEY-123",
                                              sandbox_bwrap="/usr/bin/bwrap"), environ={"PATH": "/usr/bin", "HOME": "/home/x"})
        plan = prov._plan(make_request(box, "ok"))
        inner = [*plan.argv, "--settings", "/s/settings.json", *plan.tail]
        if not sandbox:
            assert inner[0] == str(box[1])
            continue
        root = box[0] / "scratchtmp" / "r"
        root.mkdir()
        argv = prov._sandboxed(inner, plan, str(root))
        assert argv[0] == "/usr/bin/bwrap" and "--die-with-parent" in argv
        assert argv[argv.index("--") + 1:][0] == os.path.realpath(box[1])
        assert not any("SECRET-KEY-123" in a or str(box[3]) in a for a in argv)
        assert plan.env["ANTHROPIC_API_KEY"] == "SECRET-KEY-123"  # via env only


def test_fail_closed_when_bwrap_missing(box, monkeypatch):
    for kw in (dict(sandbox_bwrap=str(box[0] / "no-such-bwrap")),):
        prov = ClaudeCliProvider(make_profile(box, **kw))
        with pytest.raises(SandboxUnavailable, match="bwrap is not installed"):
            asyncio.run(prov.start(make_request(box, "ok")))
        assert asyncio.run(prov.probe("p1")).status == "unavailable"
    monkeypatch.setattr(sandbox_launcher, "find_bwrap", lambda: None)
    with pytest.raises(SandboxUnavailable):
        asyncio.run(ClaudeCliProvider(make_profile(box)).start(make_request(box, "ok")))
    assert not (box[0] / "scratchtmp" / f"synapse-agent-{os.getuid()}").exists()  # nothing was started


def test_fail_closed_when_user_namespaces_unavailable(box):
    fake = box[0] / "bwrap"
    fake.write_text("#!/bin/sh\necho 'bwrap: No permissions to create new namespace' >&2\nexit 1\n")
    fake.chmod(0o755)
    marker = box[0] / "agent-ran"
    prov = ClaudeCliProvider(make_profile(box, sandbox_bwrap=str(fake)))
    with pytest.raises(SandboxUnavailable, match="user namespaces"):
        asyncio.run(prov.start(make_request(box, "probe", {"state": str(marker)})))
    assert not marker.exists() and prov._executions == {}  # refused: never ran unsandboxed either


def test_unsandboxed_default_does_not_need_bwrap(box):
    prov = ClaudeCliProvider(make_profile(box, sandbox=False, sandbox_bwrap=str(box[0] / "nope")))
    handle = asyncio.run(prov.start(make_request(box, "ok")))
    assert handle


# --- real bwrap + fake agent -----------------------------------------------------------------------
def real_home():
    return os.path.expanduser("~")


@needs_bwrap
def test_sandboxed_agent_cannot_see_state_dir_or_real_home_and_has_private_pid_ns(box):
    _, _, wt, state = box
    ambient = {"PATH": "/usr/bin:/bin", "HOME": real_home()}
    prov = ClaudeCliProvider(make_profile(box, api_key="K-1"), environ=ambient)
    events = asyncio.run(run(prov, make_request(box, "probe", {"state": str(state), "real_home": real_home()})))
    assert events[-1].kind == "succeeded", events
    out = json.loads(next(e.text for e in events if e.kind == "output"))
    assert out["token"] == "err:FileNotFoundError" and out["sqlite"] == "err:FileNotFoundError"
    assert out["state_listdir"].startswith("err:") and out["home_listdir"].startswith("err:")
    assert out["home_env"] != real_home() and out["home_env"].startswith(tempfile.gettempdir())
    assert out["home_write"].startswith("ok:")  # the scratch HOME is writable
    assert out["wt_write"].startswith("ok:") and (wt / "made-by-agent").read_text() == "1"
    assert out["etc_write"].startswith("err:") and out["usr_write"].startswith("err:")
    assert out["cwd"] == str(wt)
    assert os.getpid() not in out["pids"] and len(out["pids"]) <= 6 and out["self_pid"] in out["pids"]
    assert out["api_env"] == "K-1"
    # the planted files are untouched
    assert (state / "approver.token").read_text() == "FAKE-APPROVER-TOKEN"


@needs_bwrap
def test_control_unsandboxed_agent_can_read_state_dir(box):
    """Control: without the sandbox the same fake DOES read the planted secrets (same uid)."""
    _, _, _, state = box
    prov = ClaudeCliProvider(make_profile(box, sandbox=False), environ={"PATH": "/usr/bin:/bin", "HOME": real_home()})
    events = asyncio.run(run(prov, make_request(box, "probe", {"state": str(state), "real_home": real_home()})))
    out = json.loads(next(e.text for e in events if e.kind == "output"))
    assert out["token"].startswith("ok:FAKE-APPROVER") and out["sqlite"].startswith("ok:")
    assert os.getpid() in out["pids"]


@needs_bwrap
def test_linked_worktree_repo_is_read_only_inside_sandbox(box):
    tmp, _, wt, state = box
    repo = tmp / "repo"
    (repo / ".git" / "worktrees" / "w1").mkdir(parents=True)
    (wt / ".git").rmdir()
    (wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/w1\n")
    prov = ClaudeCliProvider(make_profile(box, git_repos=(str(repo),)),
                             environ={"PATH": "/usr/bin:/bin", "HOME": real_home()})
    events = asyncio.run(run(prov, make_request(box, "probe", {"state": str(state), "real_home": real_home(),
                                                               "repo": str(repo / ".git")})))
    assert events[-1].kind == "succeeded", events
    out = json.loads(next(e.text for e in events if e.kind == "output"))
    assert out["repo_write"].startswith("err:") and out["wt_write"].startswith("ok:")
    assert not (repo / ".git" / "synapse-x").exists()


@needs_bwrap
def test_setsid_grandchild_dies_on_cancel(box):
    token = "synapse-grandchild-%d-%d" % (os.getpid(), time.monotonic_ns())
    prov = ClaudeCliProvider(make_profile(box), environ={"PATH": "/usr/bin:/bin", "HOME": real_home()})

    async def go():
        handle = await prov.start(make_request(box, "grandchild", {"token": token}))
        events = []

        async def consume():
            async for e in prov.stream(handle):
                events.append(e)

        task = asyncio.create_task(consume())
        for _ in range(100):  # wait until the setsid grandchild exists on the host
            if host_pids_with(token):
                break
            await asyncio.sleep(0.1)
        assert host_pids_with(token), "grandchild never started"
        await prov.cancel(handle)
        await asyncio.wait_for(task, 30)
        return events

    events = asyncio.run(go())
    assert events[-1].kind == "cancelled"
    for _ in range(50):
        if not host_pids_with(token):
            break
        time.sleep(0.1)
    assert host_pids_with(token) == []


@needs_bwrap
def test_scratch_removed_after_sandboxed_run(box):
    prov = ClaudeCliProvider(make_profile(box), environ={"PATH": "/usr/bin:/bin", "HOME": real_home()})
    events = asyncio.run(run(prov, make_request(box, "ok")))
    assert events[-1].kind == "succeeded", events
    base = box[0] / "scratchtmp" / f"synapse-agent-{os.getuid()}"
    assert list(base.iterdir()) == [] and stat.S_IMODE(base.stat().st_mode) == 0o700


# --- N1: a real-looking layout with everything under the (monkeypatched) real HOME -------------

@pytest.fixture
def home_layout(tmp_path, monkeypatch):
    home = tmp_path / "home" / "dev"
    for d in ("Projects/wt", "Projects/sibling", ".local/bin", ".claude", ".ssh", "state"):
        (home / d).mkdir(parents=True)
    (home / "Projects/wt/.git").mkdir()
    (home / "Projects/sibling/secret.txt").write_text("sibling")
    (home / "state/operator.token").write_text("tok")
    (home / ".claude/.credentials.json").write_text("cred")
    (home / ".claude/.credentials.json").chmod(0o600)
    exe = home / ".local/bin/claude"
    exe.write_text("x")
    monkeypatch.setenv("HOME", str(home))
    return home, home / "Projects/wt", home / "state", exe


def _forbidden(home, state):
    return [str(state)] + [str(home / d) for d in claude_cli._DENY_HOME_SUBDIRS]


def _argv(home, wt, state, exe, scratch, **kw):
    kw.setdefault("worktree", str(wt))
    return build_bwrap_argv(
        "/usr/bin/bwrap", scratch=str(scratch), executable=str(exe), inner=[str(exe)],
        forbidden=_forbidden(home, state), no_ancestor=[str(home)], **kw)


def test_worktree_under_home_is_accepted_but_home_itself_is_not_mounted(home_layout, tmp_path):
    home, wt, state, exe = home_layout
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    argv = _argv(home, wt, state, exe, scratch)
    assert str(wt) in argv and str(exe) in argv
    assert str(home) not in argv and str(state) not in argv  # only exact allowlisted paths are bound


def test_refuses_worktree_that_is_or_contains_protected_paths(home_layout, tmp_path):
    home, wt, state, exe = home_layout
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for bad in (home, home.parent, state, state / "x", home / ".claude", home / ".ssh",
                home / ".claude" / "sub"):
        with pytest.raises(SandboxUnavailable):
            _argv(home, wt, state, exe, scratch, worktree=str(bad))
    (wt / "inner-state").mkdir()  # a worktree that contains the state dir
    with pytest.raises(SandboxUnavailable):
        build_bwrap_argv("/usr/bin/bwrap", worktree=str(wt), scratch=str(scratch), executable=str(exe),
                         inner=[str(exe)], forbidden=[str(wt / "inner-state")])
    with pytest.raises(SandboxUnavailable):  # the repo .git extra dir may not be HOME either
        _argv(home, wt, state, exe, scratch, extra_ro_dirs=[str(home)])
    # an executable under a credential dir is refused (F3)
    cred_exe = home / ".claude" / "local-claude"
    cred_exe.write_text("x")
    with pytest.raises(SandboxUnavailable):
        _argv(home, wt, state, cred_exe, scratch)


def test_provider_sandboxed_accepts_layout_under_home(home_layout, tmp_path, monkeypatch):
    home, wt, state, exe = home_layout
    scratchtmp = tmp_path / "stmp"
    scratchtmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratchtmp))
    prof = ClaudeCliProfile(executable=str(exe), allowed_tools=("Read",), state_dir=str(state), enabled=True,
                            sandbox=True, sandbox_bwrap="/usr/bin/bwrap")
    prov = ClaudeCliProvider(prof, environ={"PATH": "/usr/bin", "HOME": str(home)})
    root = str(scratchtmp / "run")
    os.makedirs(root)
    plan = type("P", (), {"cwd": str(wt)})()
    out = prov._sandboxed([str(exe), "-p"], plan, root)
    assert str(wt) in out and str(home) not in out
    plan.cwd = str(home)
    with pytest.raises(SandboxUnavailable):
        prov._sandboxed([str(exe), "-p"], plan, root)


@needs_bwrap
def test_real_bwrap_worktree_under_home_hides_siblings_and_state(home_layout, tmp_path):
    import subprocess
    home, wt, state, exe = home_layout
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (wt / "mine.txt").write_text("ok")
    ls = "/usr/bin/ls"
    argv = build_bwrap_argv(find_bwrap(), worktree=str(wt), scratch=str(scratch), executable=ls,
                            inner=[ls, "-A", str(home)], no_ancestor=[str(home)],
                            forbidden=_forbidden(home, state))

    def run_ls(*args):
        argv[argv.index("--") + 1:] = [ls, *args]
        return subprocess.run(argv, capture_output=True, timeout=30, stdin=subprocess.DEVNULL)

    done = run_ls("-A", str(home))
    assert done.returncode == 0, done.stderr
    assert done.stdout.decode().split() == ["Projects"]  # only the path leading to the worktree
    assert run_ls("-A", str(home / "Projects")).stdout.decode().split() == ["wt"]
    for probe in (str(home / "Projects/sibling"), str(state), str(home / ".claude"), str(home / ".ssh")):
        assert run_ls(probe).returncode != 0, probe
    r = run_ls(str(wt))
    assert r.returncode == 0 and b"mine.txt" in r.stdout


# --- PR #149 review fixes (F1-F3) ------------------------------------------------------

def _prov_home(tmp_path, monkeypatch, home, wt, repos=(), exe=None, env_home="same"):
    """A sandbox provider whose passwd home and $HOME are monkeypatched; returns (prov, plan, root)."""
    state = tmp_path / "pstate"
    state.mkdir(exist_ok=True)
    stmp = tmp_path / "pstmp"
    stmp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(stmp))
    exe = exe or (tmp_path / "bin-claude")
    if not exe.exists():
        exe.write_text("x")
    prof = ClaudeCliProfile(executable=str(exe), allowed_tools=("Read",), state_dir=str(state), enabled=True,
                            sandbox=True, sandbox_bwrap="/usr/bin/bwrap", git_repos=tuple(repos))
    environ = {"PATH": "/usr/bin"}
    if env_home == "same":
        environ["HOME"] = str(home)
    elif env_home is not None:
        environ["HOME"] = env_home
    root = str(stmp / "run")
    os.makedirs(root, exist_ok=True)
    plan = type("P", (), {"cwd": str(wt)})()
    return ClaudeCliProvider(prof, environ=environ), plan, root, exe


def _fake_passwd(monkeypatch, home):
    import pwd
    real = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: type("E", (), {"pw_dir": home, "pw_name": real.pw_name})())


def test_f1_agent_rewritten_dotgit_cannot_mount_home_dirs(home_layout, tmp_path, monkeypatch):
    home, _, _, _ = home_layout
    _fake_passwd(monkeypatch, str(home))
    fake = home / ".codex" / "worktrees" / "x"
    fake.mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("secret")
    repo = tmp_path / "repo"
    (repo / ".git" / "worktrees" / "w1").mkdir(parents=True)
    wt = home / "Projects" / "wt"
    (wt / ".git").rmdir()
    (wt / ".git").write_text(f"gitdir: {fake}\n")  # the attack
    prov, plan, root, exe = _prov_home(tmp_path, monkeypatch, home, wt, repos=[str(repo)])
    with pytest.raises(SandboxUnavailable):
        prov._sandboxed([str(exe), "-p"], plan, root)
    for target in (home / ".codex", home / ".local" / "share", home / ".password-store"):
        (wt / ".git").write_text(f"gitdir: {target}/worktrees/x\n")
        with pytest.raises(SandboxUnavailable):
            prov._sandboxed([str(exe), "-p"], plan, root)
        with pytest.raises(SandboxUnavailable):
            git_readonly_dirs(str(wt), [str(repo)])
    # also refused when no repo is allowlisted at all, and for a gitdir outside worktrees/
    (wt / ".git").write_text(f"gitdir: {repo}/.git\n")
    with pytest.raises(SandboxUnavailable):
        git_readonly_dirs(str(wt), [str(repo)])


def test_f1_legitimate_linked_worktree_still_works(home_layout, tmp_path, monkeypatch):
    home, _, _, _ = home_layout
    _fake_passwd(monkeypatch, str(home))
    repo = home / "Projects" / "repo"
    (repo / ".git" / "worktrees" / "w1").mkdir(parents=True)
    wt = home / "Projects" / "wt"
    (wt / ".git").rmdir()
    (wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/w1\n")
    prov, plan, root, exe = _prov_home(tmp_path, monkeypatch, home, wt, repos=[str(repo)])
    argv = prov._sandboxed([str(exe), "-p"], plan, root)
    i = argv.index(str(repo / ".git"))
    assert argv[i - 1] == "--ro-bind" and argv[i + 1] == str(repo / ".git")
    assert str(home / ".codex") not in argv


@pytest.mark.parametrize("sub", [".codex", ".config", ".config/anything", ".netrc", ".local/share",
                                 ".local/share/x", ".password-store", ".npmrc", ".pypirc", ".git-credentials",
                                 ".gnupg", ".aws", ".kube", ".mozilla", ".bash_history", ".ssh", ".claude"])
def test_f2_deny_list_refuses_each_home_path(home_layout, tmp_path, monkeypatch, sub):
    home, wt, state, exe = home_layout
    _fake_passwd(monkeypatch, str(home))
    bad = home / sub
    bad.mkdir(parents=True, exist_ok=True)
    prov, plan, root, exe = _prov_home(tmp_path, monkeypatch, home, bad, exe=exe)
    with pytest.raises(SandboxUnavailable):
        prov._sandboxed([str(exe), "-p"], plan, root)


def test_f2_symlinked_deny_dir_is_resolved(home_layout, tmp_path, monkeypatch):
    home, wt, state, exe = home_layout
    _fake_passwd(monkeypatch, str(home))
    dotfiles = tmp_path / "dotfiles-repo" / "codex"
    dotfiles.mkdir(parents=True)
    (home / ".codex").symlink_to(dotfiles)
    # the worktree is the symlink's target: only the realpath entry catches it
    prov, plan, root, exe = _prov_home(tmp_path, monkeypatch, home, dotfiles, exe=exe)
    with pytest.raises(SandboxUnavailable):
        prov._sandboxed([str(exe), "-p"], plan, root)


def test_f2_passwd_home_is_checked_when_env_home_differs_or_is_unset(home_layout, tmp_path, monkeypatch):
    home, wt, state, exe = home_layout
    _fake_passwd(monkeypatch, str(home))
    other = tmp_path / "decoy"
    other.mkdir()
    bad = home / ".codex"
    bad.mkdir()
    for env_home in (str(other), "", None):
        prov, plan, root, exe = _prov_home(tmp_path, monkeypatch, home, bad, exe=exe, env_home=env_home)
        with pytest.raises(SandboxUnavailable):
            prov._sandboxed([str(exe), "-p"], plan, root)
    prov, plan, root, exe = _prov_home(tmp_path, monkeypatch, home, home, exe=exe, env_home=str(other))
    with pytest.raises(SandboxUnavailable):  # worktree == passwd home is an ancestor of denied dirs
        prov._sandboxed([str(exe), "-p"], plan, root)


def test_f2_refuses_when_no_home_is_determinable(home_layout, tmp_path, monkeypatch):
    home, wt, state, exe = home_layout
    import pwd

    def boom(uid):
        raise KeyError(uid)
    monkeypatch.setattr(pwd, "getpwuid", boom)
    prov, plan, root, exe = _prov_home(tmp_path, monkeypatch, home, wt, exe=exe, env_home=None)
    with pytest.raises(SandboxUnavailable, match="home directory"):
        prov._sandboxed([str(exe), "-p"], plan, root)
    with pytest.raises(SandboxUnavailable):
        sandbox_launcher.sandbox_homes("")


def test_f3_executable_must_be_regular_file_and_outside_forbidden(home_layout, tmp_path, monkeypatch):
    home, wt, state, exe = home_layout
    _fake_passwd(monkeypatch, str(home))
    prov, plan, root, exe = _prov_home(tmp_path, monkeypatch, home, wt, exe=exe)
    prov._sandboxed([str(exe), "-p"], plan, root)  # baseline ok
    adir = tmp_path / "adir"
    adir.mkdir()
    with pytest.raises(SandboxUnavailable, match="regular file"):
        prov._sandboxed([str(adir), "-p"], plan, root)
    with pytest.raises(SandboxUnavailable, match="regular file"):
        prov._sandboxed([str(tmp_path / "missing"), "-p"], plan, root)
    for sub in (".codex", ".local/share/claude", ".config"):
        d = home / sub
        d.mkdir(parents=True, exist_ok=True)
        bad_exe = d / "claude"
        bad_exe.write_text("x")
        with pytest.raises(SandboxUnavailable):
            prov._sandboxed([str(bad_exe), "-p"], plan, root)
    link = tmp_path / "link-claude"  # a symlink into a forbidden dir is resolved first
    link.symlink_to(home / ".codex" / "claude")
    with pytest.raises(SandboxUnavailable):
        prov._sandboxed([str(link), "-p"], plan, root)


def test_agent_config_passes_repos_as_git_repos(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    root = tmp_path / "wtroot"
    root.mkdir()
    cfg = load_agent_config(_config(tmp_path, repos=[str(repo)], worktree_root=str(root)), state)
    assert cfg.profile.git_repos == (str(repo),)


# --- git secret masking, --new-session, --unshare-cgroup-try ---

def test_argv_masks_git_config_and_adds_session_flags(tmp_path):
    wt, scratch, repo = tmp_path / "wt", tmp_path / "scratch", tmp_path / "repo"
    exe = tmp_path / "claude"
    for d in (wt / ".git", scratch, repo / ".git" / "worktrees" / "w1"):
        d.mkdir(parents=True)
    exe.write_text("x")
    for f in (wt / ".git" / "config", wt / ".git" / "credentials", repo / ".git" / "config",
              repo / ".git" / "config.worktree", repo / ".git" / "worktrees" / "w1" / "config.worktree"):
        f.write_text("secret")
    argv = build_bwrap_argv("/usr/bin/bwrap", worktree=str(wt), scratch=str(scratch), executable=str(exe),
                            inner=[str(exe)], extra_ro_dirs=[str(repo / ".git")])
    masks = [argv[i + 2] for i, a in enumerate(argv) if a == "--dev-bind" and argv[i + 1] == "/dev/null"]
    assert sorted(masks) == sorted(str(p) for p in (
        wt / ".git" / "config", wt / ".git" / "credentials", repo / ".git" / "config",
        repo / ".git" / "config.worktree", repo / ".git" / "worktrees" / "w1" / "config.worktree"))
    wt_bind = argv.index("--bind", argv.index(str(scratch)) + 1)
    assert all(argv.index(m) > wt_bind for m in masks)  # masks sit on top of the worktree bind
    for flag in ("--new-session", "--unshare-cgroup-try"):
        assert flag in argv
    assert str(wt / ".git" / "HEAD") not in argv  # nothing else in .git is masked


def test_no_mask_for_absent_files_or_symlinks(tmp_path):
    wt, scratch = tmp_path / "wt", tmp_path / "scratch"
    exe = tmp_path / "claude"
    for d in (wt / ".git", scratch):
        d.mkdir(parents=True)
    exe.write_text("x")
    (tmp_path / "elsewhere").write_text("s")
    (wt / ".git" / "config").symlink_to(tmp_path / "elsewhere")
    argv = build_bwrap_argv("/usr/bin/bwrap", worktree=str(wt), scratch=str(scratch), executable=str(exe), inner=[str(exe)])
    assert "/dev/null" not in argv


def _git(cwd, *args):
    import subprocess
    env = {"PATH": "/usr/bin:/bin", "HOME": str(cwd), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)


def _git_probe(box, config_path, **profile_kw):
    prov = ClaudeCliProvider(make_profile(box, **profile_kw), environ={"PATH": "/usr/bin:/bin", "HOME": real_home()})
    events = asyncio.run(run(prov, make_request(box, "git", {"config": str(config_path)})))
    assert events[-1].kind == "succeeded", events
    return json.loads(next(e.text for e in events if e.kind == "output"))


needs_git = pytest.mark.skipif(not os.path.exists("/usr/bin/git"), reason="/usr/bin/git is not available")


@needs_bwrap
@needs_git
def test_in_worktree_git_config_is_masked_but_git_reads_work(box):
    _, _, wt, _ = box
    (wt / ".git").rmdir()
    _git(wt, "init", "-q")
    (wt / "f.txt").write_text("one\n")
    _git(wt, "add", "f.txt")
    _git(wt, "commit", "-qm", "first")
    _git(wt, "remote", "add", "origin", "https://user:ghp_SECRETTOKEN@example.invalid/r.git")
    (wt / "f.txt").write_text("two\n")
    assert "ghp_SECRETTOKEN" in (wt / ".git" / "config").read_text()
    out = _git_probe(box, wt / ".git" / "config")
    assert out["log"][0] == 0 and "first" in out["log"][1], out
    assert out["diff"][0] == 0 and "f.txt" in out["diff"][1]
    assert out["status"][0] == 0
    assert out["config"] == "ok:"  # empty: the secret URL is not readable
    assert out["config_unlink"].startswith("err:")  # the mask cannot be removed or replaced
    assert "ghp_SECRETTOKEN" in (wt / ".git" / "config").read_text()  # host file untouched


@needs_bwrap
@needs_git
def test_linked_worktree_common_git_config_is_masked(box):
    tmp, _, wt, _ = box
    (wt / ".git").rmdir()
    wt.rmdir()
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "f.txt").write_text("one\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "first")
    _git(repo, "remote", "add", "origin", "https://user:ghp_SECRETTOKEN@example.invalid/r.git")
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    out = _git_probe(box, repo / ".git" / "config", git_repos=(str(repo),))
    assert out["log"][0] == 0 and "first" in out["log"][1], out
    assert out["config"] == "ok:" and out["config_unlink"].startswith("err:")
    assert "ghp_SECRETTOKEN" in (repo / ".git" / "config").read_text()


def test_sandbox_refusal_reason_is_logged_not_just_spawn_failed(box, caplog):
    """The run only says spawn_failed; the daemon log must say a protected path blocked the mount, and must
    never carry the prompt or paths. Reproduces the native Claude install under a protected directory."""
    import logging
    _, exe, _, _ = box
    prov = ClaudeCliProvider(make_profile(box, state_dir=str(exe.parent)))  # executable lies under a protected path
    with caplog.at_level(logging.WARNING, logger="synapse.providers.claude_cli"):
        ev = asyncio.run(run(prov, make_request(box, "ok")))
    assert ev[-1].kind == "failed" and ev[-1].text == "spawn_failed"
    messages = [r.getMessage() for r in caplog.records]
    assert any("a sandbox mount would expose a protected path" in m for m in messages)
    assert not any(str(exe.parent) in m for m in messages)
