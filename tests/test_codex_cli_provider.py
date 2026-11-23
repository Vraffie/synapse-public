"""Codex CLI provider tests using a fake executable: no real codex, no network.

The fake replays event lines recorded from codex-cli 0.170.0 (tests/fixtures/codex_cli_0_170_0)
for the schema-bearing modes and synthesises the process-level behaviours (hang, flood, ...).
"""
import asyncio
import json
import os
from pathlib import Path
import stat
import sys
import time

import pytest

from synapse.providers.codex_cli import (
    VERIFIED_CLI_VERSIONS, CodexCliProfile, CodexCliProvider, parse_cli_version,
)
from synapse.providers.contracts import ExecutionRequest

FIXTURES = Path(__file__).parent / "fixtures" / "codex_cli_0_170_0"

FAKE = '''#!{py}
import json, os, signal, sys, time
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.170.0"); sys.exit(0)
prompt = sys.stdin.read()
mode = prompt.split("\\n", 1)[0]
FIX = {fixtures!r}
def emit(o): sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
def replay(name):
    for line in open(os.path.join(FIX, name + ".jsonl")):
        sys.stdout.write(line); sys.stdout.flush()
if mode == "ok":
    replay("read_only_command")
elif mode == "writes":
    replay("workspace_write_command")
elif mode == "refused":
    replay("read_only_write_refused")
elif mode == "turnfailed":
    replay("turn_failed_bad_model"); sys.exit(1)
elif mode == "hang":
    emit({{"type": "thread.started", "thread_id": "t"}}); time.sleep(60)
elif mode == "flood":
    while True: emit({{"type": "turn.started", "pad": "x" * 100}})
elif mode == "longline":
    sys.stdout.write("y" * 50000 + "\\n"); sys.stdout.flush(); time.sleep(60)
elif mode == "garbage":
    sys.stdout.write("not json\\n"); sys.stdout.flush(); time.sleep(60)
elif mode == "fail":
    sys.exit(3)
elif mode == "noterminal":
    emit({{"type": "turn.started"}})
elif mode == "dupterminal":
    emit({{"type": "turn.completed", "usage": {{}}}}); emit({{"type": "turn.completed", "usage": {{}}}}); time.sleep(60)
elif mode == "unknownevents":
    emit({{"type": "future.event", "x": 1}}); emit({{"type": "item.started", "item": {{"type": "future_item", "command": "SECRET-CMD"}}}})
    emit({{"type": "item.completed", "item": {{"type": "agent_message", "text": "hi"}}}})
    emit({{"type": "turn.completed", "usage": {{"input_tokens": 1, "output_tokens": 2}}}})
elif mode == "badusage":
    emit({{"type": "turn.completed", "usage": {{"input_tokens": -5, "output_tokens": 2**63}}}})
elif mode == "snoop":
    # Hostile agent: try to find the operator's real credentials via HOME/CODEX_HOME/env.
    seen = {{}}
    cands = [os.path.expanduser("~/.codex/auth.json"), os.path.expanduser("~/other.txt")]
    for k, v in os.environ.items():
        cands += [v + "/auth.json", v + "/.codex/auth.json", v + "/other.txt"]
    for c in cands:
        try: seen[c] = open(c).read()
        except OSError: pass
    leaked_env = {{k: v for k, v in os.environ.items() if "REALSECRET" in v or "REALSECRET" in k}}
    emit({{"type": "item.completed", "item": {{"type": "agent_message", "text": json.dumps({{
        "seen": seen, "leaked_env": leaked_env, "home_listing": sorted(os.listdir(os.environ["HOME"])),
        "codex_listing": sorted(os.listdir(os.environ["CODEX_HOME"])),
        "tmp_listing": sorted(os.listdir(os.environ["TMPDIR"]))}})}}}})
    emit({{"type": "turn.completed", "usage": {{"input_tokens": 1, "output_tokens": 1}}}})
elif mode == "env":
    import resource
    cdir = os.environ.get("CODEX_HOME", "")
    auth = cdir + "/auth.json"
    root = os.path.dirname(cdir)
    def mode_of(p): return oct(os.stat(p).st_mode & 0o777)
    emit({{"type": "item.completed", "item": {{"type": "agent_message", "text": json.dumps({{
        "env": dict(os.environ), "argv": sys.argv, "cwd": os.getcwd(), "prompt": prompt,
        "auth": open(auth).read() if os.path.exists(auth) else None,
        "auth_mode": mode_of(auth) if os.path.exists(auth) else None,
        "root_mode": mode_of(root), "root": root,
        "rlimits": {{n: resource.getrlimit(getattr(resource, n)) for n in
            ("RLIMIT_CPU", "RLIMIT_NOFILE", "RLIMIT_FSIZE", "RLIMIT_CORE", "RLIMIT_NPROC", "RLIMIT_AS")}}}})}}}})
    emit({{"type": "turn.completed", "usage": {{"input_tokens": 1, "output_tokens": 1}}}})
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
    emit({{"type": "turn.completed", "usage": {{}}}})
else:
    emit({{"type": "item.completed", "item": {{"type": "agent_message", "text": "prompt:" + mode}}}})
    emit({{"type": "turn.completed", "usage": {{"input_tokens": 1, "output_tokens": 1}}}})
'''


@pytest.fixture
def env(tmp_path):
    exe = tmp_path / "bin" / "codex"
    exe.parent.mkdir()
    exe.write_text(FAKE.format(py=sys.executable, fixtures=str(FIXTURES)))
    exe.chmod(0o755)
    wt = tmp_path / "wt"
    (wt / ".git").mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    return tmp_path, exe, wt, state


def _w(path, text, mode=0o600):
    path.write_text(text)
    path.chmod(mode)


def profile(env, **kw):
    _, exe, _, state = env
    base = dict(executable=str(exe), state_dir=str(state), enabled=True, term_grace_seconds=0.3)
    base.update(kw)
    return CodexCliProfile(**base)


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


# --- recorded-schema behaviour ---------------------------------------------
def test_recorded_success_maps_to_output_tool_use_and_usage(env):
    p = CodexCliProvider(profile(env))

    async def go():
        h, ev = await run(p, request(env, "ok"))
        return ev, await p.usage(h)

    ev, usage = asyncio.run(go())
    assert [e.kind for e in ev] == ["started", "output", "tool_use", "output", "succeeded"]
    assert ev[2].text == "command_execution"  # the item type only, never the command line
    assert "cat note.txt" not in repr([(e.kind, e.text) for e in ev])
    assert usage.input_tokens == 32034 and usage.output_tokens == 56 and usage.source == "codex-cli"


def test_recorded_workspace_write_and_refused_runs_succeed(env):
    p = CodexCliProvider(profile(env))
    for mode in ("writes", "refused"):
        _, ev = asyncio.run(run(p, request(env, mode)))
        assert final(ev) == "succeeded", mode


def test_recorded_turn_failed_is_failed_with_exit_nonzero(env):
    p = CodexCliProvider(profile(env))
    _, ev = asyncio.run(run(p, request(env, "turnfailed")))
    assert ev[-1].kind == "failed" and ev[-1].text == "exit_nonzero"


def test_turn_failed_with_zero_exit_is_failed(env, tmp_path):
    p = CodexCliProvider(profile(env))
    # the fixture mode exits 1; emulate exit 0 with a failed turn through the parser directly
    ex = type("E", (), {})()
    ex.result_ok, ex.usage = None, None
    events = p._parse(ex, b'{"type":"turn.failed","error":{"message":"x"}}\n')
    assert events == [] and ex.result_ok is False


def test_unknown_event_types_are_ignored_and_item_type_only_surfaces(env):
    p = CodexCliProvider(profile(env))
    _, ev = asyncio.run(run(p, request(env, "unknownevents")))
    assert [e.kind for e in ev] == ["started", "tool_use", "output", "succeeded"]
    assert ev[1].text == "future_item" and "SECRET-CMD" not in str([(e.kind, e.text) for e in ev])


def test_no_terminal_turn_event_is_unknown_not_success(env):
    p = CodexCliProvider(profile(env))
    _, ev = asyncio.run(run(p, request(env, "noterminal")))
    assert ev[-1].kind == "unknown" and ev[-1].text == "no_turn_completed"


def test_duplicate_terminal_event_is_a_protocol_error(env):
    p = CodexCliProvider(profile(env))
    _, ev = asyncio.run(run(p, request(env, "dupterminal")))
    assert ev[-1].kind == "failed" and ev[-1].text == "protocol_error"


def test_garbage_and_oversized_lines_fail_closed(env):
    p = CodexCliProvider(profile(env, max_line_bytes=1024))
    _, ev = asyncio.run(run(p, request(env, "garbage")))
    assert ev[-1].kind == "failed" and ev[-1].text == "protocol_error"
    _, ev = asyncio.run(run(p, request(env, "longline")))
    assert ev[-1].kind == "failed" and ev[-1].text == "output_limit"


def test_flood_hits_the_output_cap(env):
    p = CodexCliProvider(profile(env))
    _, ev = asyncio.run(run(p, request(env, "flood", max_output_bytes=4096)))
    assert ev[-1].kind == "failed" and ev[-1].text == "output_limit"


def test_nonzero_exit_without_events_is_failed(env):
    p = CodexCliProvider(profile(env))
    _, ev = asyncio.run(run(p, request(env, "fail")))
    assert ev[-1].kind == "failed" and ev[-1].text == "exit_nonzero"


# --- argv, env, scratch, credentials ---------------------------------------
def _dump(env, prof, ambient, prompt="env"):
    async def go():
        _, ev = await run(CodexCliProvider(prof, environ=ambient), request(env, prompt))
        return ev
    ev = asyncio.run(go())
    assert ev[-1].kind == "succeeded", ev
    return json.loads(ev[1].text)


def test_argv_prompt_on_stdin_and_no_dangerous_flags(env):
    out = _dump(env, profile(env), {"PATH": "/usr/bin"}, prompt="env\nthe secret prompt body")
    argv = out["argv"][1:]  # drop the script path
    assert argv[:2] == ["exec", "--json"] and argv[-1] == "-"
    assert argv[argv.index("-s") + 1] == "read-only" and argv[argv.index("-C") + 1] == str(env[2])
    assert {"--ephemeral", "--ignore-user-config", "--ignore-rules"} <= set(argv)
    assert "the secret prompt body" not in " ".join(out["argv"]) and out["prompt"].endswith("the secret prompt body")
    joined = " ".join(argv)
    for forbidden in ("danger", "bypass", "approve-for-me", "--config", "-c ", "--oss", "--add-dir"):
        assert forbidden not in joined
    assert out["cwd"] == str(env[2])


def test_workspace_write_sandbox_is_selectable_and_full_access_is_not(env):
    out = _dump(env, profile(env, sandbox="workspace-write"), {"PATH": "/usr/bin"})
    assert out["argv"][out["argv"].index("-s") + 1] == "workspace-write"
    # workspace-write is not confined to the worktree by default (it also allows /tmp and $TMPDIR);
    # the adapter adds fixed overrides, and only for that mode
    argv = out["argv"]
    assert "sandbox_workspace_write.exclude_slash_tmp=true" in argv
    assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" in argv
    assert "sandbox_workspace_write.network_access=false" in argv
    assert argv.count("-c") == 3
    assert "-c" not in _dump(env, profile(env), {"PATH": "/usr/bin"})["argv"]
    for bad in ("danger-full-access", "full", "", "Read-Only"):
        with pytest.raises(ValueError):
            profile(env, sandbox=bad)


def test_env_scrubbing_and_scratch_home(env):
    ambient = {"PATH": "/usr/bin", "HOME": "/home/x", "LANG": "C", "AGENT_OPERATOR_TOKEN": "op-secret",
               "OPENAI_API_KEY": "ambient-key", "CODEX_API_KEY": "ambient-key2", "CODEX_HOME": "/home/x/.codex",
               "SECRET": "s"}
    out = _dump(env, profile(env), ambient)
    assert {"PATH", "HOME", "LANG", "CODEX_HOME", "TMPDIR"} <= set(out["env"])
    assert set(out["env"]) <= {"PATH", "HOME", "LANG", "PWD", "LC_CTYPE", "CODEX_HOME", "TMPDIR"}
    assert out["env"]["HOME"].startswith(str(env[3])) and out["env"]["CODEX_HOME"].startswith(str(env[3]))
    dumped = json.dumps(out["env"])
    assert not any(s in dumped for s in ("op-secret", "ambient-key", "ambient-key2", "/home/x"))
    assert out["root_mode"] == "0o700"


def test_credentials_are_copied_at_0600_into_scratch_and_removed_after(env):
    real = env[0] / "realcodex"
    real.mkdir()
    _w(real / "auth.json", '{"tok":"abc"}')
    ambient = {"PATH": "/usr/bin", "CODEX_HOME": str(real)}
    out = _dump(env, profile(env), ambient)
    assert out["auth"] == '{"tok":"abc"}' and out["auth_mode"] == "0o600"
    assert out["root"].startswith(str(env[3] / "codex-run"))
    assert not os.path.exists(out["root"])  # scratch (incl. the credential copy) removed after the run
    assert (real / "auth.json").read_text() == '{"tok":"abc"}'  # the source is never modified


def test_credentials_default_to_home_dot_codex_and_file_option(env):
    home = env[0] / "h"
    (home / ".codex").mkdir(parents=True)
    _w(home / ".codex" / "auth.json", "HOMEAUTH")
    assert _dump(env, profile(env), {"PATH": "/usr/bin", "HOME": str(home)})["auth"] == "HOMEAUTH"
    cred = env[0] / "elsewhere.json"
    _w(cred, "FILEAUTH")
    assert _dump(env, profile(env, credentials_file=str(cred)), {"PATH": "/usr/bin", "HOME": str(home)})["auth"] == "FILEAUTH"
    assert _dump(env, profile(env), {"PATH": "/usr/bin"})["auth"] is None  # nothing to copy: codex reports auth failure


def test_credentials_never_appear_in_events_or_usage(env):
    real = env[0] / "realcodex"
    real.mkdir()
    _w(real / "auth.json", "TOKEN-SENTINEL-123")
    p = CodexCliProvider(profile(env), environ={"PATH": "/usr/bin", "CODEX_HOME": str(real)})

    async def go():
        h, ev = await run(p, request(env, "ok"))
        return ev, await p.usage(h)
    ev, usage = asyncio.run(go())
    assert "TOKEN-SENTINEL-123" not in repr(ev) and "TOKEN-SENTINEL-123" not in repr(usage)


def test_rlimits_are_applied(env):
    out = _dump(env, profile(env), {"PATH": "/usr/bin"})
    rl = out["rlimits"]
    assert rl["RLIMIT_CORE"][0] == 0 and rl["RLIMIT_NOFILE"][0] == 4096 and rl["RLIMIT_CPU"][0] > 0


def test_scratch_removed_after_failure_too(env):
    p = CodexCliProvider(profile(env))
    asyncio.run(run(p, request(env, "fail")))
    base = env[3] / "codex-run"
    assert not base.exists() or not list(base.iterdir())


# --- worktree and repo config ------------------------------------------------
@pytest.mark.parametrize("name", [".codex", "AGENTS.md", "AGENTS.override.md", ".mcp.json"])
def test_repo_controlled_config_is_refused_unless_opted_in(env, name):
    (env[2] / name).write_text("x")
    with pytest.raises(ValueError, match="repo-controlled"):
        asyncio.run(CodexCliProvider(profile(env)).start(request(env, "ok")))
    p = CodexCliProvider(profile(env, allow_repo_codex_config=True))
    _, ev = asyncio.run(run(p, request(env, "ok")))
    assert final(ev) == "succeeded"


def test_repo_config_planted_after_start_is_refused_at_spawn(env):
    p = CodexCliProvider(profile(env))

    async def go():
        h = await p.start(request(env, "ok"))
        (env[2] / "AGENTS.md").write_text("planted")
        return [e async for e in p.stream(h)]
    ev = asyncio.run(go())
    assert ev[-1].kind == "failed" and "AGENTS.md" in ev[-1].text and "refused" in ev[-1].text and not any(e.kind == "output" for e in ev)


def test_refusal_text_with_control_characters_is_escaped_and_capped(env):
    p = CodexCliProvider(profile(env))

    async def go():
        h = await p.start(request(env, "ok"))
        (env[2] / ("a\x1b[31m\nb" + "x" * 200)).mkdir()  # repo-controlled name, hostile characters
        (env[2] / ("a\x1b[31m\nb" + "x" * 200) / "AGENTS.md").write_text("planted")
        return [e async for e in p.stream(h)]
    ev = asyncio.run(go())
    assert ev[-1].kind == "failed"
    assert "\x1b" not in ev[-1].text and "\n" not in ev[-1].text and len(ev[-1].text) <= 200


def test_worktree_validation_reused(env):
    p = CodexCliProvider(profile(env))
    for bad in ("relative", "/nonexistent-xyz", str(env[3]), "/"):
        with pytest.raises(ValueError):
            asyncio.run(p.start(ExecutionRequest("p1", "t1", {"prompt": "ok", "worktree": bad})))


def test_prompt_and_limits_validation(env):
    prov = CodexCliProvider(profile(env))
    for bad in ("", "  ", "a\x00b", "x" * 100_001):
        with pytest.raises(ValueError):
            prov._plan(request(env, bad))
    for extra in ({"timeout_seconds": 0}, {"timeout_seconds": True}, {"timeout_seconds": 1e9},
                  {"max_output_bytes": 0}, {"max_output_bytes": 10**12}):
        with pytest.raises(ValueError):
            prov._plan(request(env, "ok", **extra))


# --- profile / extra_args -----------------------------------------------------
def test_disabled_by_default(env):
    p = CodexCliProvider(profile(env, enabled=False))
    with pytest.raises(RuntimeError):
        asyncio.run(p.start(request(env, "ok")))
    assert CodexCliProfile(executable="/bin/x", state_dir="/s").enabled is False
    assert asyncio.run(p.probe("p1")).status == "unavailable"


def test_profile_validation():
    good = dict(executable="/usr/bin/codex", state_dir="/s")
    CodexCliProfile(**good)
    for override in (
        {"executable": "codex"}, {"executable": "/x\x00y"}, {"state_dir": "s"}, {"enabled": 1},
        {"credentials_file": "rel"}, {"allow_repo_codex_config": 1}, {"memory_limit_bytes": 5},
        {"max_timeout_seconds": 0}, {"max_timeout_seconds": 1e9}, {"term_grace_seconds": -1},
        {"max_output_bytes_limit": 10}, {"max_line_bytes": 10}, {"max_line_bytes": 2 * 1024 * 1024, "max_output_bytes_limit": 1024 * 1024},
    ):
        with pytest.raises(ValueError):
            CodexCliProfile(**{**good, **override})


@pytest.mark.parametrize("bad", [
    "--config=sandbox_mode=danger-full-access", "-c", "-c=x", "--sandbox=danger-full-access", "-s", "-s=read-only",
    "--cd=/", "-C", "--add-dir=/", "--dangerously-bypass-approvals-and-sandbox", "--approve-for-me", "--oss",
    "--profile=x", "-p", "--enable=x", "--disable=x", "--image=x", "-i", "--output-last-message=/x", "-o",
    "--output-schema=/x", "--model", "--model x", "--model=x y", "--model=--evil", "--model=a\n", "--model=a\x00",
    "--skip-git-repo-check", "--ignore-user-config", "--json", "-m",
])
def test_extra_args_is_an_allowlist(bad):
    with pytest.raises(ValueError):
        CodexCliProfile(executable="/usr/bin/codex", state_dir="/s", extra_args=(bad,))


def test_extra_args_model_is_accepted_once_and_passed_as_one_argv_element(env):
    out = _dump(env, profile(env, extra_args=("--model=gpt-5.1",)), {"PATH": "/usr/bin"})
    assert "--model=gpt-5.1" in out["argv"]
    with pytest.raises(ValueError):
        profile(env, extra_args=("--model=a", "--model=b"))


# --- lifecycle ----------------------------------------------------------------
def test_timeout(env):
    p = CodexCliProvider(profile(env))
    t = time.monotonic()
    _, ev = asyncio.run(run(p, request(env, "hang", timeout_seconds=1)))
    assert final(ev) == "timed_out" and time.monotonic() - t < 10


def test_cancel_running(env):
    p = CodexCliProvider(profile(env))
    _, ev = asyncio.run(run(p, request(env, "hang"), cancel_after=0.7))
    assert final(ev) == "cancelled"


def test_cancel_before_stream_never_spawns(env):
    p = CodexCliProvider(profile(env))

    async def go():
        h = await p.start(request(env, "hang"))
        await p.cancel(h)
        return [e async for e in p.stream(h)]
    ev = asyncio.run(go())
    assert [e.kind for e in ev] == ["started", "cancelled"]
    assert not (env[3] / "codex-run").exists() or not list((env[3] / "codex-run").iterdir())


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.parametrize("mode", ["grandchild_hang", "grandchild_exit"])
def test_no_descendant_survives(env, mode):
    p = CodexCliProvider(profile(env))
    _, ev = asyncio.run(run(p, request(env, mode, timeout_seconds=5), cancel_after=1.0 if mode == "grandchild_hang" else None))
    pid = int((env[2] / "gc.pid").read_text())
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    assert not _alive(pid)


def test_stream_can_only_be_consumed_once_and_unknown_handle_rejected(env):
    p = CodexCliProvider(profile(env))

    async def go():
        h, _ = await run(p, request(env, "ok"))
        with pytest.raises(RuntimeError):
            [e async for e in p.stream(h)]
        with pytest.raises(ValueError):
            await p.usage("nope")
    asyncio.run(go())


def test_spawn_failure_is_failed_never_succeeded(env, tmp_path):
    missing = profile(env, executable=str(tmp_path / "bin" / "missing"))
    _, ev = asyncio.run(run(CodexCliProvider(missing), request(env, "ok")))
    assert ev[-1].kind == "failed" and ev[-1].text == "spawn_failed"


# --- probe ---------------------------------------------------------------------
def _version_exe(tmp_path, body):
    exe = tmp_path / "codex-version"
    exe.write_text(f"#!{sys.executable}\n{body}\n")
    exe.chmod(0o755)
    return str(exe)


def test_probe_reports_version_status(env, tmp_path):
    def status(exe):
        prof = CodexCliProfile(executable=exe, state_dir=str(tmp_path / "s"), enabled=True)
        return asyncio.run(CodexCliProvider(prof).probe("p1")).status
    assert VERIFIED_CLI_VERSIONS == ("0.170.0",)
    assert status(str(env[1])) == "healthy"
    assert status(_version_exe(tmp_path, 'print("codex-cli 0.171.0")')) == "unverified_version"
    assert status(_version_exe(tmp_path, 'print("hello")')) == "unavailable"
    assert status(_version_exe(tmp_path, 'import sys; sys.exit(2)')) == "unavailable"
    assert status(_version_exe(tmp_path, 'import time; time.sleep(60)')) == "unavailable"


def test_parse_cli_version():
    assert parse_cli_version("codex-cli 0.170.0\n") == "0.170.0"
    for bad in ("", "0.170.0", "codex 0.170.0", "codex-cli 0.170", "codex-cli 0.170.0x", "9" * 200):
        assert parse_cli_version(bad) is None


def test_capabilities_and_type_checks(env):
    p = CodexCliProvider(profile(env))
    caps = asyncio.run(p.capabilities("p1"))
    assert caps.streaming and caps.cancellation and caps.structured_events and not caps.resume and not caps.mcp
    with pytest.raises(TypeError):
        CodexCliProvider(object())
    with pytest.raises(TypeError):
        asyncio.run(p.start(object()))


# --- credential confinement (A2) -------------------------------------------
def _real_home(env):
    home = env[0] / "realhome"
    (home / ".codex").mkdir(parents=True)
    _w(home / ".codex" / "auth.json", "REALSECRET-TOKEN")
    _w(home / "other.txt", "REALSECRET-OTHER")
    return home


def test_agent_cannot_find_real_credentials_via_home_or_env(env):
    home = _real_home(env)
    ambient = {"PATH": "/usr/bin", "HOME": str(home), "CODEX_HOME": str(home / ".codex"), "LANG": "C",
               "XDG_CONFIG_HOME": str(home), "GH_TOKEN": "REALSECRET-GH", "TMPDIR": str(home)}
    out = _dump(env, profile(env), ambient, prompt="snoop")
    assert out["leaked_env"] == {}
    # only the scratch copy of the one required file is visible, never the real HOME contents
    assert all(path.startswith(str(env[3] / "codex-run")) for path in out["seen"])
    assert "REALSECRET-OTHER" not in json.dumps(out["seen"])
    assert out["codex_listing"] == ["auth.json"] and out["home_listing"] == [] and out["tmp_listing"] == []


@pytest.mark.parametrize("how", ["symlink", "group_readable", "world_readable", "directory", "oversize", "fifo"])
def test_unsafe_credential_source_is_refused(env, how):
    src = env[0] / "cred.json"
    if how == "symlink":
        _w(env[0] / "real.json", "x")
        src.symlink_to(env[0] / "real.json")
    elif how == "group_readable":
        _w(src, "x", 0o640)
    elif how == "world_readable":
        _w(src, "x", 0o644)
    elif how == "directory":
        src.mkdir()
    elif how == "oversize":
        _w(src, "x" * ((1 << 20) + 1))
    else:
        os.mkfifo(src, 0o600)
    p = CodexCliProvider(profile(env, credentials_file=str(src)), environ={"PATH": "/usr/bin"})
    with pytest.raises(ValueError):
        asyncio.run(p.start(request(env, "ok")))
    assert not (env[3] / "codex-run").exists() or not list((env[3] / "codex-run").iterdir())


def test_unsafe_default_credential_source_is_refused(env):
    home = env[0] / "h2"
    (home / ".codex").mkdir(parents=True)
    _w(home / ".codex" / "auth.json", "x", 0o644)
    p = CodexCliProvider(profile(env), environ={"PATH": "/usr/bin", "HOME": str(home)})
    with pytest.raises(ValueError):
        asyncio.run(p.start(request(env, "ok")))


def test_credential_source_inside_worktree_or_state_dir_is_refused(env):
    _w(env[2] / "auth.json", "x")
    p = CodexCliProvider(profile(env, credentials_file=str(env[2] / "auth.json")), environ={"PATH": "/usr/bin"})
    with pytest.raises(ValueError):
        asyncio.run(p.start(request(env, "ok")))
    _w(env[3] / "auth.json", "x")
    with pytest.raises(ValueError):
        profile(env, credentials_file=str(env[3] / "auth.json"))


def test_scratch_and_credential_copy_removed_when_spawn_fails(env):
    real = env[0] / "realcodex"
    real.mkdir()
    _w(real / "auth.json", "tok")
    p = CodexCliProvider(profile(env), environ={"PATH": "/usr/bin", "CODEX_HOME": str(real)})
    env[1].unlink()  # exec fails after the scratch dir and credential copy were created

    async def go():
        h = await p.start(request(env, "ok"))
        return [e async for e in p.stream(h)]
    ev = asyncio.run(go())
    assert ev[-1].kind == "failed"
    assert list((env[3] / "codex-run").iterdir()) == []


def test_out_of_range_usage_is_unknown(env):
    p = CodexCliProvider(profile(env))

    async def go():
        h, ev = await run(p, request(env, "badusage"))
        return await p.usage(h)
    usage = asyncio.run(go())
    assert usage.input_tokens is None and usage.output_tokens is None


def test_finished_executions_are_pruned(env, monkeypatch):
    import synapse.providers.codex_cli as mod
    monkeypatch.setattr(mod, "KEEP_FINISHED", 2)
    p = CodexCliProvider(profile(env))

    async def go():
        handles = [(await run(p, request(env, "ok")))[0] for _ in range(5)]
        return handles
    handles = asyncio.run(go())
    assert len(p._executions) == 2 and set(p._executions) == set(handles[-2:])


def test_long_running_first_execution_is_not_pruned_when_it_finishes(env, monkeypatch):
    import synapse.providers.codex_cli as mod
    monkeypatch.setattr(mod, "KEEP_FINISHED", 2)
    p = CodexCliProvider(profile(env))

    async def go():
        first = await p.start(request(env, "ok"))  # started first, finishes last
        later = [(await run(p, request(env, "ok")))[0] for _ in range(4)]
        events = [e async for e in p.stream(first)]
        return first, later, events, await p.usage(first)  # its own usage() must still resolve
    first, later, events, usage = asyncio.run(go())
    assert events and first in p._executions
    assert set(p._executions) == {first, later[-1]}  # newest two finished, by finish order


def test_deep_agents_md_is_refused_at_start(env):
    p = CodexCliProvider(profile(env))
    d = env[2]
    for i in range(14):
        d = d / "d"
    d.mkdir(parents=True)
    (d / "AGENTS.md").write_text("deep")
    with pytest.raises(ValueError, match="deeper"):
        asyncio.run(run(p, request(env, "ok")))
