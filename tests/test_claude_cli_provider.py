"""Claude CLI provider tests using a fake executable: no real claude, no network."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time

import pytest

from synapse.providers.claude_cli import ClaudeCliProfile, ClaudeCliProvider, validate_worktree
from synapse.providers.contracts import ExecutionRequest

FAKE = '''#!{py}
import json, os, signal, sys, time
if sys.argv[1:] == ["--version"]:
    print("2.1.329 (Claude Code)"); sys.exit(0)
prompt = sys.stdin.read()
mode = prompt.split("\\n", 1)[0]
def emit(o): sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
def text(t): emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": t}}]}}}})
def ok(): emit({{"type": "result", "subtype": "success", "is_error": False, "usage": {{"input_tokens": 3, "output_tokens": 4}}}})
emit({{"type": "system", "subtype": "init"}})
if mode == "ok":
    text("hello"); emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Edit", "input": {{"secret": "x"}}}}]}}}}); ok()
elif mode == "hang":
    time.sleep(60)
elif mode == "flood":
    while True: emit({{"type": "system", "pad": "x" * 100}})
elif mode == "longline":
    sys.stdout.write("y" * 50000 + "\\n"); sys.stdout.flush(); time.sleep(60)
elif mode == "garbage":
    sys.stdout.write("not json\\n"); sys.stdout.flush(); time.sleep(60)
elif mode == "fail":
    sys.exit(3)
elif mode == "resulterr":
    emit({{"type": "result", "subtype": "error_during_execution", "is_error": True}})
elif mode == "noresult":
    text("hi")
elif mode == "env":
    import resource
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    cred = cfg + "/.credentials.json"
    root = os.path.dirname(cfg)
    def mode_of(p): return oct(os.stat(p).st_mode & 0o777)
    text(json.dumps({{"env": dict(os.environ), "argv": sys.argv, "cwd": os.getcwd(), "prompt": prompt,
        "creds": open(cred).read() if os.path.exists(cred) else None,
        "creds_mode": mode_of(cred) if os.path.exists(cred) else None,
        "settings": json.load(open(sys.argv[sys.argv.index("--settings") + 1])),
        "root_mode": mode_of(root), "root": root,
        "rlimits": {{n: resource.getrlimit(getattr(resource, n)) for n in
            ("RLIMIT_CPU", "RLIMIT_NOFILE", "RLIMIT_FSIZE", "RLIMIT_CORE", "RLIMIT_NPROC", "RLIMIT_AS")}}}})); ok()
elif mode in ("grandchild_hang", "grandchild_exit"):
    pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        open("gc.pid", "w").write(str(os.getpid()))
        time.sleep(120)
        os._exit(0)
    while not os.path.exists("gc.pid") or not open("gc.pid").read(): time.sleep(0.01)
    if mode == "grandchild_hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(120)
    ok()
else:
    text("prompt:" + mode); ok()
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "scratchtmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "scratchtmp"))
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(FAKE.format(py=sys.executable))
    exe.chmod(0o755)
    wt = tmp_path / "wt"
    (wt / ".git").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    return tmp_path, exe, wt, state


def profile(env, **kw):
    _, exe, _, state = env
    base = dict(executable=str(exe), allowed_tools=("Read", "Edit", "Bash(git status)"),
                state_dir=str(state), enabled=True, term_grace_seconds=0.3, permission_mode="plan")
    base.update(kw)
    return ClaudeCliProfile(**base)


def request(env, prompt, **extra):
    return ExecutionRequest("p1", "t1", {"prompt": prompt, "worktree": str(env[2]), **extra})


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
    return handle, events


def final(events):
    return events[-1].kind


def test_success_events_and_usage(env):
    p = ClaudeCliProvider(profile(env))

    async def go():
        h, ev = await run(p, request(env, "ok"))
        return ev, await p.usage(h)

    ev, usage = asyncio.run(go())
    assert [e.kind for e in ev] == ["started", "output", "tool_use", "succeeded"]
    assert ev[2].text == "Edit"  # name only, never tool inputs
    assert usage.input_tokens == 3 and usage.output_tokens == 4


def test_disabled_by_default(env):
    p = ClaudeCliProvider(profile(env, enabled=False))
    with pytest.raises(RuntimeError):
        asyncio.run(p.start(request(env, "ok")))
    assert ClaudeCliProfile(executable="/bin/x", allowed_tools=("Read",), state_dir="/s").enabled is False


def test_env_scrubbing_and_closed_stdin(env):
    ambient = {"PATH": "/usr/bin", "HOME": "/home/x", "LANG": "C", "AGENT_OPERATOR_TOKEN": "op-secret",
               "SYNAPSE_STATE_DIR": "/s", "ANTHROPIC_API_KEY": "ambient-key", "SECRET": "s"}

    async def dump(prof):
        _, ev = await run(ClaudeCliProvider(prof, environ=ambient), request(env, "env"))
        return ev

    ev = asyncio.run(dump(profile(env)))
    out = json.loads(ev[1].text)
    assert {"PATH", "HOME", "LANG", "CLAUDE_CONFIG_DIR", "TMPDIR"} <= set(out["env"])
    assert set(out["env"]) <= {"PATH", "HOME", "LANG", "PWD", "LC_CTYPE", "CLAUDE_CONFIG_DIR", "TMPDIR"}
    assert out["env"]["HOME"].startswith(str(env[0] / "scratchtmp"))  # per-run scratch HOME, never the real one
    assert out["env"]["CLAUDE_CONFIG_DIR"].startswith(str(env[0] / "scratchtmp"))
    assert "ambient-key" not in ev[1].text and "op-secret" not in ev[1].text
    assert out["prompt"] == "env" and out["cwd"] == str(env[2])
    ev = asyncio.run(dump(profile(env, api_key="cfg-key")))
    assert json.loads(ev[1].text)["env"]["ANTHROPIC_API_KEY"] == "cfg-key"
    assert "ambient-key" not in ev[1].text

def test_prompt_and_limits_validation(env):
    prov = ClaudeCliProvider(profile(env))
    for bad in ("", "  ", "a\x00b", "x" * 100_001):
        with pytest.raises(ValueError):
            prov._plan(request(env, bad))
    for extra in ({"timeout_seconds": 0}, {"timeout_seconds": True}, {"timeout_seconds": 1e9},
                  {"max_output_bytes": 0}, {"max_output_bytes": 10**12}):
        with pytest.raises(ValueError):
            prov._plan(request(env, "ok", **extra))


def test_timeout(env):
    p = ClaudeCliProvider(profile(env))
    t = time.monotonic()
    _, ev = asyncio.run(run(p, request(env, "hang", timeout_seconds=1)))
    assert final(ev) == "timed_out" and time.monotonic() - t < 10


def test_cancel_running(env):
    p = ClaudeCliProvider(profile(env))
    _, ev = asyncio.run(run(p, request(env, "hang"), cancel_after=0.7))
    assert final(ev) == "cancelled"


def test_cancel_before_stream_spawns_nothing(env):
    p = ClaudeCliProvider(profile(env))

    async def go():
        h = await p.start(request(env, "hang"))
        await p.cancel(h)
        return [e async for e in p.stream(h)], p._execution(h)

    ev, ex = asyncio.run(go())
    assert [e.kind for e in ev] == ["started", "cancelled"] and ex.process is None


def _alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _wait_dead(pid):
    for _ in range(100):
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_cancel_kills_sigterm_ignoring_grandchild(env):
    p = ClaudeCliProvider(profile(env))
    _, ev = asyncio.run(run(p, request(env, "grandchild_hang"), cancel_after=1.0))
    assert final(ev) == "cancelled"
    assert _wait_dead(int((env[2] / "gc.pid").read_text()))


def test_natural_exit_kills_leftover_grandchild(env):
    p = ClaudeCliProvider(profile(env))
    _, ev = asyncio.run(run(p, request(env, "grandchild_exit")))
    assert final(ev) == "succeeded"
    assert _wait_dead(int((env[2] / "gc.pid").read_text()))


def test_timeout_kills_grandchild(env):
    p = ClaudeCliProvider(profile(env))
    _, ev = asyncio.run(run(p, request(env, "grandchild_hang", timeout_seconds=1.5)))
    assert final(ev) == "timed_out"
    assert _wait_dead(int((env[2] / "gc.pid").read_text()))


def test_output_cap(env):
    p = ClaudeCliProvider(profile(env))
    _, ev = asyncio.run(run(p, request(env, "flood", max_output_bytes=4096)))
    assert ev[-1].kind == "failed" and ev[-1].text == "output_limit"


def test_line_cap(env):
    p = ClaudeCliProvider(profile(env, max_line_bytes=2048))
    _, ev = asyncio.run(run(p, request(env, "longline")))
    assert ev[-1].kind == "failed" and ev[-1].text == "output_limit"


def test_malformed_json_fails_closed(env):
    p = ClaudeCliProvider(profile(env))
    t = time.monotonic()
    _, ev = asyncio.run(run(p, request(env, "garbage")))
    assert ev[-1].kind == "failed" and ev[-1].text == "protocol_error" and time.monotonic() - t < 10


def test_exit_codes_and_error_results(env):
    p = ClaudeCliProvider(profile(env))
    assert final(asyncio.run(run(p, request(env, "fail")))[1]) == "failed"
    assert final(asyncio.run(run(p, request(env, "resulterr")))[1]) == "failed"


def test_missing_result_is_unknown(env):
    p = ClaudeCliProvider(profile(env))
    assert final(asyncio.run(run(p, request(env, "noresult")))[1]) == "unknown"


def test_unestablished_process_is_unknown(env, monkeypatch):
    import synapse.providers.claude_cli as mod
    from synapse.infrastructure.mock_process_identity import ProcessStatus

    def refuse(process, identity, *, grace_seconds):
        process.kill()
        process.wait()
        return ProcessStatus.UNKNOWN

    monkeypatch.setattr(mod, "stop_mock_child_group", refuse)
    p = ClaudeCliProvider(profile(env))
    _, ev = asyncio.run(run(p, request(env, "hang"), cancel_after=0.5))
    assert final(ev) == "unknown"


def test_spawn_failure(env):
    p = ClaudeCliProvider(profile(env, executable=str(env[0] / "missing")))
    _, ev = asyncio.run(run(p, request(env, "ok")))
    assert ev[-1].kind == "failed" and ev[-1].text == "spawn_failed"


def test_stream_single_consumer(env):
    p = ClaudeCliProvider(profile(env))

    async def go():
        h, _ = await run(p, request(env, "ok"))
        with pytest.raises(RuntimeError):
            async for _ in p.stream(h):
                pass
        await p.cancel(h)  # no-op after finish

    asyncio.run(go())


def test_consumer_abandon_reaps_child(env):
    p = ClaudeCliProvider(profile(env))

    async def go():
        h = await p.start(request(env, "hang"))
        gen = p.stream(h)
        await gen.__anext__()
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await gen.aclose()
        return p._execution(h)

    ex = asyncio.run(go())
    assert ex.reaped and ex.process.returncode is not None


def test_worktree_rules(env, tmp_path):
    _, _, wt, state = env
    assert validate_worktree(str(wt), str(state)) == str(wt)
    link = tmp_path / "link"
    link.symlink_to(wt)
    for bad in (str(link), "relative", str(tmp_path / "nope"), str(state), str(tmp_path), "/", "",
                str(wt) + "/../wt"):
        with pytest.raises(ValueError):
            validate_worktree(bad, str(state))
    nogit = tmp_path / "nogit"
    nogit.mkdir()
    with pytest.raises(ValueError):
        validate_worktree(str(nogit), str(state))
    inside = state / "wt2"
    (inside / ".git").mkdir(parents=True)
    with pytest.raises(ValueError):
        validate_worktree(str(inside), str(state))
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / ".git").symlink_to(state)
    with pytest.raises(ValueError):
        validate_worktree(str(evil), str(state))


def test_profile_validation():
    good = dict(executable="/usr/bin/claude", allowed_tools=("Read",), state_dir="/s")
    ClaudeCliProfile(**good)
    for override in (
        {"executable": "claude"}, {"state_dir": "rel"}, {"allowed_tools": ()}, {"allowed_tools": ["Read"]},
        {"allowed_tools": ("Read; rm",)}, {"allowed_tools": ("",)}, {"permission_mode": "yolo"},
        {"permission_mode": "bypassPermissions"}, {"api_key": ""}, {"extra_args": ("--add-dir",)},
        {"extra_args": ("--settings=/x",)}, {"extra_args": ("--",)}, {"extra_args": ("notflag",)},
        {"credentials_file": "rel"}, {"allow_repo_claude_config": 1}, {"memory_limit_bytes": 5},
        {"term_grace_seconds": 99}, {"max_line_bytes": 10},
    ):
        with pytest.raises(ValueError):
            ClaudeCliProfile(**{**good, **override})
    loud = ClaudeCliProfile(**good, permission_mode="bypassPermissions", allow_bypass_permissions=True)
    assert loud.permission_mode == "bypassPermissions"
    assert "k-secret" not in repr(ClaudeCliProfile(**good, api_key="k-secret"))


def test_probe_and_capabilities(env):
    p = ClaudeCliProvider(profile(env))
    assert asyncio.run(p.probe("p1")).status == "healthy"
    assert asyncio.run(ClaudeCliProvider(profile(env, enabled=False)).probe("p1")).status == "unavailable"
    caps = asyncio.run(p.capabilities("p1"))
    assert caps.streaming and caps.cancellation and not caps.resume


def _version_exe(tmp_path, body):
    exe = tmp_path / "claude-version"
    exe.write_text(f"#!{sys.executable}\n{body}\n")
    exe.chmod(0o755)
    return str(exe)


def test_probe_reports_version_status(env, tmp_path):
    def status(exe):
        prof = ClaudeCliProfile(executable=exe, allowed_tools=("Read",), state_dir=str(tmp_path / "s"), enabled=True)
        return asyncio.run(ClaudeCliProvider(prof).probe("p1")).status
    assert status(_version_exe(tmp_path, 'print("2.1.329 (Claude Code)")')) == "healthy"
    assert status(_version_exe(tmp_path, 'print("2.9.0 (Claude Code)")')) == "unverified_version"
    assert status(_version_exe(tmp_path, 'print("hello")')) == "unavailable"
    assert status(_version_exe(tmp_path, 'import sys; sys.exit(2)')) == "unavailable"
    assert status(_version_exe(tmp_path, 'import time; time.sleep(60)')) == "unavailable"


def test_parse_cli_version():
    from synapse.providers.claude_cli import parse_cli_version
    assert parse_cli_version("2.1.329 (Claude Code)\n") == "2.1.329"
    assert parse_cli_version("2.1.329") == "2.1.329"
    for bad in ("", "v2.1.329", "2.1", "2.1.329x", "x 2.1.329", "9" * 200):
        assert parse_cli_version(bad) is None


def test_permission_modes_match_real_cli_choices():
    from synapse.providers.claude_cli import PERMISSION_MODES
    good = dict(executable="/usr/bin/claude", allowed_tools=("Read",), state_dir="/s")
    assert PERMISSION_MODES == {"acceptEdits", "plan", "dontAsk", "manual", "bypassPermissions"}
    for mode in ("manual", "plan", "dontAsk", "acceptEdits"):
        assert ClaudeCliProfile(**good, permission_mode=mode).permission_mode == mode
    for mode in ("default", "auto", "bogus"):
        with pytest.raises(ValueError):
            ClaudeCliProfile(**good, permission_mode=mode)


def test_tools_flag_is_the_deduplicated_base_names_of_the_allowlist(tmp_path):
    from synapse.providers.claude_cli import ClaudeCliProfile, ClaudeCliProvider
    from synapse.providers.contracts import ExecutionRequest
    wt = tmp_path / "wt"
    (wt / ".git").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    prof = ClaudeCliProfile(executable=sys.executable, allowed_tools=("Read", "Bash(git status)", "Bash(git diff)", "Edit"),
                            state_dir=str(state), enabled=True, permission_mode="plan")
    plan = ClaudeCliProvider(prof)._plan(ExecutionRequest("p1", "t1", {"prompt": "x", "worktree": str(wt.resolve())}))
    assert plan.argv[plan.argv.index("--tools") + 1] == "Read,Bash,Edit"
    assert plan.tail[0] == "--allowedTools" and "Bash(git status)" in plan.tail  # patterns still pre-approve exactly


# --- A5: acceptEdits must not be combined with Bash unless explicitly opted in -------------------------
_GOOD = dict(executable="/usr/bin/claude", state_dir="/s")


@pytest.mark.parametrize("tools", [("Read", "Bash"), ("Edit", "Bash(git status)"), ("Bash(touch x)",)])
def test_accept_edits_with_bash_is_refused_by_default(tools):
    with pytest.raises(ValueError, match="acceptEdits"):
        ClaudeCliProfile(**_GOOD, allowed_tools=tools, permission_mode="acceptEdits")
    with pytest.raises(ValueError, match="acceptEdits"):  # the default mode is acceptEdits
        ClaudeCliProfile(**_GOOD, allowed_tools=tools)


def test_accept_edits_without_bash_and_bash_in_other_modes_are_fine():
    ClaudeCliProfile(**_GOOD, allowed_tools=("Read", "Edit", "Write", "MultiEdit"), permission_mode="acceptEdits")
    for mode in ("plan", "dontAsk", "manual"):
        ClaudeCliProfile(**_GOOD, allowed_tools=("Read", "Bash(git status)"), permission_mode=mode)


def test_opt_in_flag_must_be_a_real_bool():
    with pytest.raises(ValueError):
        ClaudeCliProfile(**_GOOD, allowed_tools=("Bash",), permission_mode="plan", allow_bash_with_accept_edits=1)


def test_opt_in_allows_the_combination_passes_argv_and_is_audited(tmp_path):
    wt = tmp_path / "wt"
    (wt / ".git").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    prof = ClaudeCliProfile(executable=sys.executable, allowed_tools=("Read", "Bash(git status)"), state_dir=str(state),
                            enabled=True, permission_mode="acceptEdits", allow_bash_with_accept_edits=True)
    prov = ClaudeCliProvider(prof)
    req = ExecutionRequest("p1", "t1", {"prompt": "x", "worktree": str(wt.resolve())})
    plan = prov._plan(req)
    assert plan.argv[plan.argv.index("--permission-mode") + 1] == "acceptEdits"
    assert plan.argv[plan.argv.index("--setting-sources") + 1] == "user"  # repo settings never loaded
    assert "--settings" not in plan.argv  # appended at spawn from the Synapse-owned scratch file

    async def first_two():
        gen = prov.stream(await prov.start(req))
        evs = [await gen.__anext__(), await gen.__anext__()]
        await gen.aclose()
        return evs
    evs = asyncio.run(first_two())
    assert [e.kind for e in evs] == ["started", "audit"] and "allow_bash_with_accept_edits" in evs[1].text
