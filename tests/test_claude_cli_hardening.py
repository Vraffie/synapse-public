"""Hardening tests for the Claude CLI adapter (F3/F7/F12) using the fake executable."""
import asyncio
import json
import os

import pytest

from synapse.providers.claude_cli import ClaudeCliProfile, ClaudeCliProvider
from test_claude_cli_provider import env, final, profile, request, run  # noqa: F401  (env is a fixture)

GOOD = dict(executable="/usr/bin/claude", allowed_tools=("Read",), state_dir="/s")


@pytest.mark.parametrize("bad", [
    "--permission-prompt-tool=mcp__x", "--permission-prompt-tool", "--tools=Bash", "--disallowedTools=X",
    "--append-system-prompt=x", "--system-prompt=x", "--ide", "-pc", "-p", "--setting-sources=project",
    "--settings=/x", "--model", "--model x", "--model=x y", "--model=--evil", "--effort=ultra",
    "--max-budget-usd=-1", "--max-budget-usd=1e9", "--allowedTools=Bash", "--add-dir=/", "--agents={}",
    "--plugin-dir=/x", "--mcp-config=/x", "--dangerously-skip-permissions", "--fallback-model=a,b,c,d,e,f",
    "--model=a\n", "--model=a\x00", "--max-budget-usd=\u0663",
])
def test_extra_args_is_an_allowlist(bad):
    with pytest.raises(ValueError):
        ClaudeCliProfile(**GOOD, extra_args=(bad,))


def test_extra_args_safe_flags_pass_through_and_dedupe():
    ok = ("--model=claude-sonnet-5-5", "--effort=low", "--max-budget-usd=0.5",
          "--fallback-model=sonnet,opus", "--exclude-dynamic-system-prompt-sections")
    assert ClaudeCliProfile(**GOOD, extra_args=ok).extra_args == ok
    with pytest.raises(ValueError):
        ClaudeCliProfile(**GOOD, extra_args=("--model=a", "--model=b"))


def scratch_base(env):
    return (env[0] / "scratchtmp").resolve() / f"synapse-agent-{os.getuid()}"


def run_env(env, prompt="env", ambient=None, **kw):
    prov = ClaudeCliProvider(profile(env, **kw), environ=ambient or {"PATH": "/usr/bin", "HOME": "/home/x"})
    _, ev = asyncio.run(run(prov, request(env, prompt)))
    return json.loads(ev[1].text), prov, ev


def test_scratch_dir_settings_credentials_and_cleanup(env):
    real_cfg = env[0] / "realcfg"
    real_cfg.mkdir()
    (real_cfg / ".credentials.json").write_text('{"tok":"abc"}')
    (real_cfg / ".credentials.json").chmod(0o600)
    ambient = {"PATH": "/usr/bin", "HOME": "/home/x", "CLAUDE_CONFIG_DIR": str(real_cfg)}
    out, prov, ev = run_env(env, ambient=ambient)
    assert ev[-1].kind == "succeeded"
    assert out["creds"] == '{"tok":"abc"}' and out["creds_mode"] == "0o600" and out["root_mode"] == "0o700"
    assert out["root"].startswith(str(scratch_base(env)))
    assert not out["root"].startswith(str(env[3]))  # scratch lives outside the state dir
    assert out["argv"][out["argv"].index("--setting-sources") + 1] == "user"
    assert out["argv"].index("--settings") < out["argv"].index("--allowedTools")
    # The available tool set is restricted with --tools (--allowedTools alone only pre-approves tools).
    assert out["argv"][out["argv"].index("--tools") + 1] == "Read,Edit,Bash"  # exact; test profile patterns stripped to base names
    assert out["settings"]["disableAllHooks"] is True
    deny = out["settings"]["permissions"]["deny"]
    assert f"Read(/{env[3]}/**)" in deny and "Edit(//home/x/.ssh/**)" in deny and f"Read(/{real_cfg}/**)" in deny
    assert not os.path.exists(out["root"])  # scratch (incl. credential copy) removed after the run
    assert (real_cfg / ".credentials.json").read_text() == '{"tok":"abc"}'


def test_credentials_file_profile_option(env):
    cred = env[0] / "elsewhere.json"
    cred.write_text("CRED")
    cred.chmod(0o600)
    out, _, _ = run_env(env, credentials_file=str(cred))
    assert out["creds"] == "CRED"


def test_scratch_removed_on_cancel_timeout_and_failed_spawn(env):
    for kw, cancel in (({}, 0.7), ({"timeout_seconds": 1}, None)):
        prov = ClaudeCliProvider(profile(env))
        h, ev = asyncio.run(run(prov, request(env, "hang", **kw), cancel_after=cancel))
        assert final(ev) in ("cancelled", "timed_out") and prov._execution(h).scratch is None
    base = scratch_base(env)
    assert list(base.iterdir()) == []
    prov = ClaudeCliProvider(profile(env, executable=str(env[0] / "nope")))
    _, ev = asyncio.run(run(prov, request(env, "ok")))
    assert ev[-1].text == "spawn_failed" and list(base.iterdir()) == []


def test_api_key_profile_copies_no_credentials(env):
    out, _, _ = run_env(env, api_key="k", ambient={"PATH": "/usr/bin", "HOME": str(env[0])})
    assert out["creds"] is None


def test_rlimits_applied_and_bounded(env):
    out, _, _ = run_env(env)
    lim = out["rlimits"]
    assert lim["RLIMIT_NOFILE"][0] == 4096 and lim["RLIMIT_CORE"][0] == 0
    assert lim["RLIMIT_FSIZE"][0] == 1024 ** 3
    assert 0 < lim["RLIMIT_CPU"][0] <= 3600 + 5 + 60 + 1
    assert lim["RLIMIT_NPROC"][0] > 1024
    assert lim["RLIMIT_AS"][0] == lim["RLIMIT_AS"][1]  # untouched by default
    out, _, _ = run_env(env, memory_limit_bytes=64 << 30)
    assert out["rlimits"]["RLIMIT_AS"][0] == 64 << 30


@pytest.mark.parametrize("name", [".claude", "CLAUDE.md", "CLAUDE.local.md", ".mcp.json"])
def test_worktree_with_repo_claude_config_is_refused_unless_opted_in(env, name):
    path = env[2] / name
    path.mkdir() if name == ".claude" else path.write_text("x")
    with pytest.raises(ValueError, match="repo-controlled"):
        asyncio.run(ClaudeCliProvider(profile(env)).start(request(env, "ok")))
    ev = asyncio.run(run(ClaudeCliProvider(profile(env, allow_repo_claude_config=True)), request(env, "ok")))[1]
    assert final(ev) == "succeeded"


def test_repo_config_planted_after_start_fails_spawn(env):
    prov = ClaudeCliProvider(profile(env))

    async def go():
        h = await prov.start(request(env, "ok"))
        (env[2] / ".claude").mkdir()
        return [e async for e in prov.stream(h)]

    ev = asyncio.run(go())
    assert ev[-1].kind == "failed" and ev[-1].text == "spawn_failed"


def test_hostile_prompt_is_stdin_not_argv(env):
    hostile = "--dangerously-skip-permissions; $(touch pwn) `id` &\n'\""
    plan = ClaudeCliProvider(profile(env))._plan(request(env, hostile))
    argv = (*plan.argv, "--settings", "/x", *plan.tail)
    assert plan.prompt == hostile and not any(hostile in a for a in argv) and "--" not in argv
    assert "--dangerously-skip-permissions" not in argv
    _, ev = asyncio.run(run(ClaudeCliProvider(profile(env)), request(env, "env\n" + hostile)))
    out = json.loads(ev[1].text)
    assert out["prompt"] == "env\n" + hostile and not any(hostile in a for a in out["argv"])
    assert not (env[2] / "pwn").exists()


# --- credential source vetting (same rules as the Codex adapter) ---

def _cred(env, name, data="CRED", mode=0o600):
    path = env[0] / name
    path.write_text(data)
    path.chmod(mode)
    return path


def _start_error(env, **kw):
    prov = ClaudeCliProvider(profile(env, **kw), environ={"PATH": "/usr/bin", "HOME": "/home/x"})
    with pytest.raises(ValueError, match="credentials"):
        asyncio.run(run(prov, request(env, "env")))


def test_credentials_symlink_refused(env):
    link = env[0] / "link.json"
    link.symlink_to(_cred(env, "real.json"))
    _start_error(env, credentials_file=str(link))


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o602, 0o666])
def test_credentials_loose_mode_refused(env, mode):
    _start_error(env, credentials_file=str(_cred(env, "loose.json", mode=mode)))


def test_credentials_stricter_mode_ok(env):
    out, _, _ = run_env(env, credentials_file=str(_cred(env, "strict.json", mode=0o400)))
    assert out["creds"] == "CRED"


def test_credentials_non_regular_and_oversized_refused(env):
    fifo = env[0] / "fifo.json"
    os.mkfifo(fifo, 0o600)
    _start_error(env, credentials_file=str(fifo))
    big = env[0] / "big.json"
    big.write_bytes(b"x" * ((1 << 20) + 1))
    big.chmod(0o600)
    _start_error(env, credentials_file=str(big))


def test_credentials_inside_worktree_refused(env):
    cred = env[2] / "creds.json"
    cred.write_text("CRED")
    cred.chmod(0o600)
    _start_error(env, credentials_file=str(cred))


def test_credentials_owned_by_another_user_refused(env, monkeypatch):
    cred = _cred(env, "owner.json")
    assert ClaudeCliProvider._read_credentials(str(cred)) == b"CRED"
    monkeypatch.setattr(os, "getuid", lambda: os.stat(cred).st_uid + 1)
    with pytest.raises(ValueError, match="owned by the current user"):
        ClaudeCliProvider._read_credentials(str(cred))


def test_ambient_credentials_symlink_refused(env):
    cfg = env[0] / "cfgdir"
    cfg.mkdir()
    (cfg / ".credentials.json").symlink_to(_cred(env, "r.json"))
    prov = ClaudeCliProvider(profile(env), environ={"PATH": "/usr/bin", "HOME": "/home/x", "CLAUDE_CONFIG_DIR": str(cfg)})
    with pytest.raises(ValueError, match="credentials"):
        asyncio.run(run(prov, request(env, "env")))


def argv_for(env, **kw):
    out, _, _ = run_env(env, **kw)
    return out["argv"]


def test_tools_exact_for_read_only_and_bash_pattern_profiles(env):
    a = argv_for(env, allowed_tools=("Read", "Glob", "Grep"))
    assert a[a.index("--tools") + 1] == "Read,Glob,Grep"
    assert a[a.index("--disallowedTools") + 1] == "Write,Edit,MultiEdit,Bash,WebFetch,NotebookEdit"
    b = argv_for(env, allowed_tools=("Read", "Bash(git status)", "Bash(git diff *)"))
    assert b[b.index("--tools") + 1] == "Read,Bash"
    assert "--disallowedTools" not in b  # profiles that legitimately allow mutating tools are unchanged
    assert b[b.index("--allowedTools") + 1:] == ["Read", "Bash(git status)", "Bash(git diff *)"]


def test_disallowed_tools_only_when_no_mutating_tool_allowed(env):
    for tool in ("Write", "Edit", "Bash", "WebFetch", "NotebookEdit", "Bash(ls)"):
        assert "--disallowedTools" not in argv_for(env, allowed_tools=("Read", tool))


def test_tools_precede_extra_args_and_extra_args_cannot_alter_it(env):
    extra = ("--model=claude-sonnet-5-5", "--fallback-model=sonnet,opus", "--effort=low",
             "--max-budget-usd=0.5", "--exclude-dynamic-system-prompt-sections")
    a = argv_for(env, allowed_tools=("Read",), extra_args=extra)
    i = a.index("--tools")
    assert a[i:i + 2] == ["--tools", "Read"] and a.count("--tools") == 1 and a.count("--disallowedTools") == 1
    assert all(i < a.index(x) for x in extra)
    assert a[a.index("--disallowedTools") + 1] == "Write,Edit,MultiEdit,Bash,WebFetch,NotebookEdit"


@pytest.mark.parametrize("bad", ["default", "Default", "DEFAULT", "default(x)", "", " ", "default ", "()", "(Bash)"])
def test_reserved_or_empty_tool_names_are_rejected(bad):
    with pytest.raises(ValueError):
        ClaudeCliProfile(**{**GOOD, "allowed_tools": ("Read", bad)})


@pytest.mark.parametrize("tool", ["Bash", "bash", "BASH", "bash(ls)"])
def test_accept_edits_with_bash_is_refused_whatever_the_case(tool):
    with pytest.raises(ValueError, match="acceptEdits"):
        ClaudeCliProfile(**{**GOOD, "allowed_tools": ("Read", tool)}, permission_mode="acceptEdits")


def test_multiedit_and_lowercase_writers_are_not_labelled_read_only():
    for tools in (("Read", "MultiEdit"), ("Read", "write"), ("Read", "EDIT")):
        prov = ClaudeCliProvider(ClaudeCliProfile(**{**GOOD, "allowed_tools": tools}))
        assert prov._disallowed_args() == ()
    read_only = ClaudeCliProvider(ClaudeCliProfile(**GOOD))
    assert "MultiEdit" in read_only._disallowed_args()[1].split(",")
