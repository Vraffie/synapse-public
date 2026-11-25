"""Regression tests for security review 2026-11-20 findings F3, F4, F5, F12.

Complements test_claude_cli_hardening.py, test_worktrees.py and test_agent_runs_api.py. Each
test here fails if the corresponding fix is reverted. Hermetic: tmp_path repos, fake git on PATH,
the fake claude executable from test_claude_cli_provider (never the real claude).
"""
import asyncio
import json
import os
import time

import pytest

import synapse.infrastructure.worktrees as wtmod
from synapse.infrastructure.worktrees import WorktreeError
from synapse.providers import claude_cli
from synapse.providers.claude_cli import ClaudeCliProfile, ClaudeCliProvider
from test_claude_cli_provider import env, profile, request, run  # noqa: F401  (env is a fixture)
from test_worktrees import mgr, repo, run_git  # noqa: F401  (fixtures)
from test_agent_runs_api import Env

@pytest.fixture
def env_api(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


GOOD = dict(executable="/usr/bin/claude", allowed_tools=("Read",), state_dir="/s")

# ---------------------------------------------------------------- F12 extra_args allowlist

ALLOWED = {
    "--model": "--model=claude-sonnet-5-5",
    "--fallback-model": "--fallback-model=sonnet",
    "--effort": "--effort=low",
    "--max-budget-usd": "--max-budget-usd=1",
    "--exclude-dynamic-system-prompt-sections": "--exclude-dynamic-system-prompt-sections",
}


def test_f12_allowlist_contents_are_pinned():
    # Adding a permission/tool/dir/settings/MCP/system-prompt flag here must be a deliberate review.
    assert set(claude_cli._ALLOWED_EXTRA) == set(ALLOWED)


@pytest.mark.parametrize("flag", sorted(ALLOWED))
def test_f12_each_allowed_flag_is_accepted_once_and_rejected_when_duplicated(flag):
    one = ALLOWED[flag]
    assert ClaudeCliProfile(**GOOD, extra_args=(one,)).extra_args == (one,)
    with pytest.raises(ValueError):
        ClaudeCliProfile(**GOOD, extra_args=(one, one))


@pytest.mark.parametrize("flag", [f for f in sorted(ALLOWED) if f != "--exclude-dynamic-system-prompt-sections"])
def test_f12_space_separated_flag_value_form_is_rejected(flag):
    value = ALLOWED[flag].split("=", 1)[1]
    for bad in (f"{flag} {value}", flag):
        with pytest.raises(ValueError):
            ClaudeCliProfile(**GOOD, extra_args=(bad,))
    with pytest.raises(ValueError):  # two argv elements: "--flag", "value"
        ClaudeCliProfile(**GOOD, extra_args=(flag, value))


@pytest.mark.parametrize("bad", [
    "--permission-mode=bypassPermissions", "--allow-dangerously-skip-permissions", "--resume=abc",
    "--continue", "--session-id=x", "--input-format=stream-json", "--output-format=text",
    "--model=x;touch pwn", "--model=$(id)", "--model=a b", "--effort=low ", "--effort=LOW",
    "--max-budget-usd=1.00001", "--max-budget-usd=", "--model=" + "a" * 65, "",
    "--exclude-dynamic-system-prompt-sections=1", " --effort=low"])
def test_f12_more_unlisted_or_malformed_values_are_rejected(bad):
    with pytest.raises(ValueError):
        ClaudeCliProfile(**GOOD, extra_args=(bad,))


def test_f12_allowed_extra_arg_reaches_argv_after_setting_sources(env):
    plan = ClaudeCliProvider(profile(env, extra_args=("--effort=low",)))._plan(request(env, "ok"))
    assert "--effort=low" in plan.argv
    assert plan.argv[plan.argv.index("--setting-sources") + 1] == "user"


# ---------------------------------------------------------------- F3 repo-controlled Claude config

def _hostile_settings(root):
    d = root / ".claude"
    d.mkdir()
    (d / "settings.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "touch pwn"}]}]},
        "apiKeyHelper": "touch pwn", "env": {"X": "1"}}))
    (d / "settings.local.json").write_text("{}")


def test_f3_repo_with_settings_json_hooks_is_refused_before_any_spawn(env):
    _hostile_settings(env[2])
    with pytest.raises(ValueError, match="repo-controlled"):
        asyncio.run(ClaudeCliProvider(profile(env)).start(request(env, "env")))
    assert not (env[2] / "pwn").exists()


def test_f3_settings_json_planted_after_start_fails_spawn_and_runs_nothing(env):
    prov = ClaudeCliProvider(profile(env))

    async def go():
        h = await prov.start(request(env, "env"))
        _hostile_settings(env[2])
        return [e async for e in prov.stream(h)]

    ev = asyncio.run(go())
    assert ev[-1].kind == "failed" and ev[-1].text == "spawn_failed"
    assert [e.kind for e in ev] == ["started", "failed"]  # fake CLI never produced output
    assert not (env[2] / "pwn").exists()


def test_f3_repo_config_symlink_is_also_refused(env):
    target = env[0] / "elsewhere.md"
    target.write_text("x")
    os.symlink(target, env[2] / "CLAUDE.md")
    with pytest.raises(ValueError, match="repo-controlled"):
        asyncio.run(ClaudeCliProvider(profile(env)).start(request(env, "ok")))


def test_f3_isolated_run_passes_no_repo_or_user_settings_and_scrubs_env(env):
    ambient = {"PATH": "/usr/bin", "HOME": "/home/victim", "CLAUDE_CONFIG_DIR": "/home/victim/.claude",
               "ANTHROPIC_API_KEY": "leak", "ANTHROPIC_BASE_URL": "http://evil", "GIT_SSH_COMMAND": "evil",
               "LD_PRELOAD": "/evil.so", "NODE_OPTIONS": "--require /evil.js", "AWS_SECRET_ACCESS_KEY": "s"}
    prov = ClaudeCliProvider(profile(env), environ=ambient)
    _, ev = asyncio.run(run(prov, request(env, "env")))
    out = json.loads(ev[1].text)
    argv = out["argv"]
    assert argv[argv.index("--setting-sources") + 1] == "user"  # never "project"/"local"
    assert "--settings" in argv and out["settings"]["disableAllHooks"] is True
    e = out["env"]
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "GIT_SSH_COMMAND", "LD_PRELOAD", "NODE_OPTIONS",
              "AWS_SECRET_ACCESS_KEY"):
        assert k not in e
    assert e["HOME"] != "/home/victim" and e["CLAUDE_CONFIG_DIR"] != "/home/victim/.claude"
    assert out["cwd"] == str(env[2])


# ---------------------------------------------------------------- F4 planted git config / attributes

def _dirty(mgr, repo):
    path = mgr.create("r", "t", repo, "main")
    with open(os.path.join(path, "a.txt"), "a") as fh:
        fh.write("more\n")
    return path


@pytest.mark.parametrize("key,value", [
    ("merge.x.driver", "touch pwned"), ("core.attributesFile", "/tmp/attrs"), ("core.askPass", "x"),
    ("core.editor", "x"), ("core.gitProxy", "x"), ("credential.helper", "!x"),
    ("credential.https://h.helper", "!x"), ("gpg.program", "x"), ("gpg.ssh.program", "x"),
    ("includeIf.gitdir:/.path", "/nonexistent-synapse-test/x"), ("diff.x.command", "x"), ("filter.x.smudge", "x"),
    ("alias.x", "  !touch pwned"), ("core.fsmonitor", "true"), ("core.fsmonitor", "/bin/true")])
def test_f4_more_dangerous_keys_refused_on_every_tree_call(mgr, repo, key, value):
    _dirty(mgr, repo)
    run_git(repo, "config", key, value)
    for op in (mgr.status, mgr.diff):
        with pytest.raises(WorktreeError, match="not allowed"):
            op("r", "t")
    with pytest.raises(WorktreeError):
        mgr.remove("r", "t")


def test_f4_config_key_check_is_case_insensitive(mgr, repo):
    _dirty(mgr, repo)
    with open(os.path.join(repo, ".git", "config"), "a") as fh:
        fh.write('[Filter "x"]\n\tCLEAN = touch pwned\n[CORE]\n\tHooksPath = /tmp\n')
    with pytest.raises(WorktreeError, match="not allowed"):
        mgr.status("r", "t")


def test_f4_worktree_git_dir_config_worktree_refused_on_diff(mgr, repo):
    path = _dirty(mgr, repo)
    gitdir = open(os.path.join(path, ".git")).read().split("gitdir: ", 1)[1].strip()
    open(os.path.join(gitdir, "config.worktree"), "w").write("[core]\n\thooksPath = /tmp\n")
    for op in (mgr.status, mgr.diff):
        with pytest.raises(WorktreeError, match="config.worktree"):
            op("r", "t")


def test_f4_info_attributes_symlink_refused(mgr, repo, tmp_path):
    _dirty(mgr, repo)
    (tmp_path / "attrs").write_text("# nothing\n")
    os.symlink(tmp_path / "attrs", os.path.join(repo, ".git", "info", "attributes"))
    with pytest.raises(WorktreeError, match="info/attributes"):
        mgr.status("r", "t")


def test_f4_creating_a_worktree_in_a_poisoned_repo_runs_nothing(mgr, repo, tmp_path):
    marker = tmp_path / "ran"
    run_git(repo, "config", "core.fsmonitor", f"touch {marker}")
    hooks = os.path.join(repo, ".git", "hooks")
    for name in ("post-checkout", "reference-transaction", "post-commit", "pre-commit"):
        p = os.path.join(hooks, name)
        open(p, "w").write(f"#!/bin/sh\ntouch {marker}\n")
        os.chmod(p, 0o755)
    try:
        mgr.create("r2", "t2", repo, "main")
    except WorktreeError:
        pass  # refusing is fine; executing is not
    assert not marker.exists()


def test_f4_every_git_call_carries_the_hardening_flags(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    log = tmp_path / "argv.log"
    (fake / "git").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\nexit 0\n')
    (fake / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    wtmod._git(str(tmp_path), ["status"])
    line = log.read_text()
    for needle in ("core.hooksPath=/dev/null", "core.fsmonitor=false", "core.attributesFile=/dev/null",
                   "diff.external=", "protocol.ext.allow=never"):
        assert needle in line


def test_f4_tree_git_pins_attributes_to_empty_tree(mgr, repo, monkeypatch):
    _dirty(mgr, repo)
    seen = []
    real = wtmod._git
    monkeypatch.setattr(wtmod, "_git", lambda cwd, args, **k: (seen.append(list(args)), real(cwd, args, **k))[1])
    mgr.status("r", "t")
    mgr.diff("r", "t")
    tree_calls = [a for a in seen if a and a[0].startswith("--attr-source=")]
    assert tree_calls and all(a[0] == f"--attr-source={wtmod._EMPTY_TREE}" for a in tree_calls)


# ---------------------------------------------------------------- F5 git deadline / event loop

def _fake_git(tmp_path, monkeypatch, body):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "git").write_text("#!/bin/sh\n" + body + "\n")
    (fake / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")


def test_f5_per_call_timeout_is_honoured_when_git_is_silent(tmp_path, monkeypatch):
    _fake_git(tmp_path, monkeypatch, "sleep 600")
    t0 = time.monotonic()
    with pytest.raises(WorktreeError, match="timed out"):
        wtmod._git(str(tmp_path), ["status"], timeout=0.5)
    assert time.monotonic() - t0 < 4  # default is 60s


def test_f5_deadline_is_wall_clock_even_if_git_trickles_output(tmp_path, monkeypatch):
    _fake_git(tmp_path, monkeypatch, "while true; do echo x; sleep 0.1; done")
    monkeypatch.setattr(wtmod, "_GIT_TIMEOUT", 1.0)
    t0 = time.monotonic()
    with pytest.raises(WorktreeError):
        wtmod._git(str(tmp_path), ["status"], max_bytes=1 << 30)
    assert time.monotonic() - t0 < 5


def test_f5_hanging_git_in_a_worker_thread_does_not_stall_the_loop(tmp_path, monkeypatch):
    _fake_git(tmp_path, monkeypatch, "sleep 3")
    monkeypatch.setattr(wtmod, "_GIT_TIMEOUT", 2.0)

    async def go():
        ticks = []

        async def ticker():
            for _ in range(10):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        t = asyncio.ensure_future(ticker())
        with pytest.raises(WorktreeError):
            await asyncio.to_thread(wtmod._git, str(tmp_path), ["status"])
        await t
        return ticks

    ticks = asyncio.run(go())
    assert max(b - a for a, b in zip(ticks, ticks[1:])) < 0.5


def test_f5_hung_git_through_agent_service_start_does_not_stall_the_loop(env_api, monkeypatch):
    # Real service path: start() -> check_base() -> hung fake git. The loop must keep ticking
    # (approvals/cancel/expiry sweep share it) and the start must fail cleanly at the git deadline.
    from synapse.api.agent_commands import AgentRunError
    (env_api.tmp / "fakegit").mkdir()
    _fake_git(env_api.tmp / "fakegit", monkeypatch, "sleep 30")
    monkeypatch.setattr(wtmod, "_GIT_TIMEOUT", 1.5)

    async def go():
        ticks = []
        stop = asyncio.Event()

        async def ticker():
            while not stop.is_set():
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        t = asyncio.ensure_future(ticker())
        try:
            with pytest.raises(AgentRunError):
                await env_api.service.start("k-hung", env_api.body(permission_mode="acceptEdits", approval_step=None))
        finally:
            stop.set()
            await t
        return ticks

    ticks = asyncio.run(go())
    assert len(ticks) >= 10  # ~1.5s of 50ms ticks; a blocked loop yields 1-2
    assert max(b - a for a, b in zip(ticks, ticks[1:])) < 0.5
    assert env_api.counts()["runs"] == 0


def _ticking(coro_factory):
    """Run ``coro_factory()`` on a loop with a 50ms ticker; return (result_or_exc, ticks)."""
    async def go():
        ticks, stop = [], asyncio.Event()

        async def ticker():
            while not stop.is_set():
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        t = asyncio.ensure_future(ticker())
        try:
            try:
                out = await coro_factory()
            except Exception as exc:  # noqa: BLE001
                out = exc
        finally:
            stop.set()
            await t
        return out, ticks
    return asyncio.run(go())


def _selective_git(tmp_path, monkeypatch, hang_on):
    """Fake git on PATH: delegates to the real git except when argv mentions ``hang_on`` (then hangs)."""
    body = f'case " $* " in *" {hang_on} "*) sleep 30;; esac\nexec /usr/bin/git "$@"'
    _fake_git(tmp_path, monkeypatch, body)


@pytest.mark.parametrize("phase", ["create", "cleanup_remove"])
def test_f5_hung_git_in_worktree_create_and_cleanup_does_not_stall_the_loop(env_api, monkeypatch, phase):
    from synapse.api.agent_commands import AgentRunError
    from synapse.daemon.agent_runtime import AgentDisabled
    (env_api.tmp / "fakegit").mkdir()
    if phase == "create":
        _selective_git(env_api.tmp / "fakegit", monkeypatch, "add")  # `git worktree add`
    else:
        # create succeeds, runtime refuses to start -> _cleanup() removes the worktree; make that hang
        def refuse(*a, **k):
            raise AgentDisabled()
        monkeypatch.setattr(env_api.runtime, "start", refuse)
        _selective_git(env_api.tmp / "fakegit", monkeypatch, "remove")
    monkeypatch.setattr(wtmod, "_GIT_TIMEOUT", 1.5)
    out, ticks = _ticking(lambda: env_api.service.start("k-" + phase, env_api.body(permission_mode="acceptEdits", approval_step=None)))
    assert isinstance(out, AgentRunError)
    assert len(ticks) >= 10
    assert max(b - a for a, b in zip(ticks, ticks[1:])) < 0.5


def test_f5_hung_git_in_show_worktree_collect_does_not_stall_the_loop(env_api, monkeypatch):
    async def start():
        return await env_api.service.start("k-show", env_api.body(permission_mode="acceptEdits", approval_step=None))

    res, _ = _ticking(start)
    run_id = res.response.get("run_id") if not isinstance(res, BaseException) else None
    assert run_id, res
    (env_api.tmp / "fakegit").mkdir()
    _selective_git(env_api.tmp / "fakegit", monkeypatch, "status")
    monkeypatch.setattr(wtmod, "_GIT_TIMEOUT", 1.5)
    out, ticks = _ticking(lambda: env_api.service.show(run_id))
    assert not isinstance(out, BaseException) or isinstance(out, WorktreeError), out
    assert len(ticks) >= 10
    assert max(b - a for a, b in zip(ticks, ticks[1:])) < 0.5
