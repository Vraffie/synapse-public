"""scripts/synapse_up.py: config merge, sandbox stays on, executable moved out of protected paths."""
import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("synapse_up", Path(__file__).resolve().parents[1] / "scripts" / "synapse_up.py")
up = importlib.util.module_from_spec(spec)
spec.loader.exec_module(up)


def make_exe(path: Path, body=b"#!/bin/sh\nexit 0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o755)
    return path


def test_protected_detection(tmp_path):
    assert up.is_protected(tmp_path / ".local/share/claude/versions/1", tmp_path)
    assert up.is_protected(tmp_path / ".config/x", tmp_path)
    assert not up.is_protected(tmp_path / ".local/bin/claude", tmp_path)
    assert not up.is_protected(tmp_path / "opt/claude", tmp_path)


def test_executable_under_protected_path_is_copied_out_and_refreshed(tmp_path):
    exe = make_exe(tmp_path / ".local/share/claude/versions/1")
    dest = up.usable_executable(exe, tmp_path)
    assert dest == tmp_path / ".local/lib/synapse/claude" and dest.read_bytes() == exe.read_bytes()
    assert dest.stat().st_mode & 0o777 == 0o755 and not up.is_protected(dest, tmp_path)
    make_exe(exe, b"#!/bin/sh\necho newer-and-longer\n")
    os.utime(exe, (dest.stat().st_mtime + 10,) * 2)
    assert up.usable_executable(exe, tmp_path).read_bytes() == exe.read_bytes()


def test_unprotected_executable_is_used_in_place(tmp_path):
    exe = make_exe(tmp_path / "opt/claude")
    assert up.usable_executable(exe, tmp_path) == exe
    assert not (tmp_path / ".local/lib").exists()


def test_config_is_private_sandboxed_and_merges_repos_and_tools(tmp_path):
    cfg = tmp_path / "agent.json"
    exe, wt = tmp_path / "claude", tmp_path / "wt"
    out = up.write_config(cfg, exe, Path("/r/one"), ("Read",), wt)
    assert out["sandbox"] is True and stat.S_IMODE(cfg.stat().st_mode) == 0o600
    out = up.write_config(cfg, exe, Path("/r/two"), ("Read", "Edit"), wt)
    assert out["repos"] == ["/r/one", "/r/two"] and out["allowed_tools"] == ["Edit", "Read"]
    assert json.loads(cfg.read_text())["sandbox"] is True
    up.write_config(cfg, exe, Path("/r/two"), ("Read",), wt)
    assert json.loads(cfg.read_text())["repos"] == ["/r/one", "/r/two"]  # no duplicates


def test_config_with_sandbox_off_is_refused_not_overwritten(tmp_path):
    cfg = tmp_path / "agent.json"
    cfg.write_text(json.dumps({"sandbox": False, "repos": []}))
    with pytest.raises(up.UpError, match="sandbox disabled"):
        up.write_config(cfg, tmp_path / "claude", Path("/r"), ("Read",), tmp_path / "wt")
    assert json.loads(cfg.read_text())["sandbox"] is False


def test_repo_must_be_a_git_checkout_and_claude_must_exist(tmp_path, monkeypatch):
    with pytest.raises(up.UpError, match="not a git repository"):
        up.check_repo(tmp_path)
    (tmp_path / ".git").mkdir()
    assert up.check_repo(tmp_path) == tmp_path.resolve()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(up.UpError, match="no claude binary"):
        up.find_claude({"PATH": str(tmp_path / "empty")})


def test_alive_rejects_a_pid_that_is_not_synapse(tmp_path):
    pidfile = tmp_path / "p.pid"
    pidfile.write_text(str(os.getpid()))  # a live process, but pytest, not synapse.*
    assert up.alive(pidfile) is False
    pidfile.write_text("not-a-pid")
    assert up.alive(pidfile) is False


def test_worktree_root_is_a_sibling_of_the_state_dir(tmp_path):
    state = tmp_path / "state"
    root = up.worktree_root(state)
    assert root.parent == state.parent and state not in root.parents and root != state


# --- workspace discovery + multi-repo registration ------------------------------------------------
GIT_ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}


def make_repo(path: Path, commits=1) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, env=GIT_ENV, check=True)
    for i in range(commits):
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", str(i)], cwd=path, env=GIT_ENV, check=True)
    return path


@pytest.fixture
def workspace_dir(tmp_path):
    root = tmp_path / "BSS"
    make_repo(root / "api")
    make_repo(root / "web app")
    (root / "web app" / "wip.txt").write_text("uncommitted")
    make_repo(root / "empty", commits=0)
    (root / "documentation").mkdir()
    make_repo(tmp_path / "outside")
    return root.resolve()


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """`up` without starting anything: the daemon reports healthy, no desktop, claude is a dummy file."""
    state = tmp_path / "state"
    monkeypatch.setenv("SYNAPSE_STATE", str(state))
    exe = make_exe(tmp_path / "opt" / "claude")
    monkeypatch.setattr(up, "find_claude", lambda env=None: exe)
    monkeypatch.setattr(up, "alive", lambda pidfile: True)
    monkeypatch.setattr(up, "healthy", lambda state: True)

    def no_spawn(*a, **k):
        raise AssertionError("must not start a process in tests")
    monkeypatch.setattr(up, "spawn", no_spawn)
    return state


def test_workspace_command_prints_table_and_register_hint(workspace_dir, capsys):
    assert up.main(["workspace", str(workspace_dir)]) == 0
    out = capsys.readouterr().out
    assert "REPO" in out and "api" in out and "web app" in out and "yes (1)" in out
    assert "NO: no commits yet" in out and "documentation" not in out and "outside" not in out
    assert "uncommitted changes are NOT visible to agents" in out
    last = out.strip().splitlines()[-1]
    assert "up --workspace" in last and "--repo api" in last and "--repo 'web app'" in last and "empty" not in last


def test_workspace_command_rejects_control_characters(capsys):
    assert up.main(["workspace", "/tmp/x\x1b"]) == 1
    assert "control characters" in capsys.readouterr().err


def test_dry_run_writes_nothing(workspace_dir, fake_env, capsys):
    rc = up.main(["up", "--workspace", str(workspace_dir), "--repo", "api", "--repo", "web app", "--dry-run",
                  "--no-desktop"])
    out = capsys.readouterr().out
    assert rc == 0 and "dry run: nothing written" in out
    assert str(workspace_dir / "api") in out and str(workspace_dir / "web app") in out
    assert not fake_env.exists()


def test_register_several_merges_private_sandboxed_config(workspace_dir, fake_env, capsys):
    fake_env.mkdir()
    cfg = fake_env / "agent.json"
    cfg.write_text(json.dumps({"sandbox": True, "repos": ["/srv/existing"], "allowed_tools": ["Edit"]}))
    cfg.chmod(0o644)
    rc = up.main(["up", "--workspace", str(workspace_dir), "--repo", "api", "--repo", "web app", "--no-desktop"])
    assert rc == 0
    data = json.loads(cfg.read_text())
    assert data["repos"] == ["/srv/existing", str(workspace_dir / "api"), str(workspace_dir / "web app")]
    assert data["sandbox"] is True and stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert data["allowed_tools"] == ["Edit", "Glob", "Grep", "Read"]
    out = capsys.readouterr().out
    assert "will add to repos" in out and "uncommitted" in out
    up.main(["up", "--workspace", str(workspace_dir), "--repo", "api", "--no-desktop"])
    assert json.loads(cfg.read_text())["repos"] == data["repos"]  # idempotent


@pytest.mark.parametrize("name,match", [("empty", "no commits"), ("../outside", "outside the scanned folder"),
                                        ("documentation", "not a repository found"), ("nope", "not a repository")])
def test_register_refuses_unusable_and_outside(workspace_dir, fake_env, capsys, name, match):
    assert up.main(["up", "--workspace", str(workspace_dir), "--repo", name, "--no-desktop"]) == 1
    assert match in capsys.readouterr().err
    assert not fake_env.exists()


def test_register_refuses_absolute_path_outside_folder(workspace_dir, fake_env, capsys):
    outside = str(workspace_dir.parent / "outside")
    assert up.main(["up", "--workspace", str(workspace_dir), "--repo", outside, "--no-desktop"]) == 1
    assert "not a repository found" in capsys.readouterr().err and not fake_env.exists()


def test_register_refuses_sandbox_off_and_nesting(workspace_dir, fake_env, capsys):
    fake_env.mkdir()
    cfg = fake_env / "agent.json"
    cfg.write_text(json.dumps({"sandbox": False}))
    assert up.main(["up", "--workspace", str(workspace_dir), "--repo", "api", "--dry-run"]) == 1
    assert "sandbox disabled" in capsys.readouterr().err
    cfg.write_text(json.dumps({"sandbox": True, "repos": [str(workspace_dir)]}))
    assert up.main(["up", "--workspace", str(workspace_dir), "--repo", "api", "--no-desktop"]) == 1
    assert "nested" in capsys.readouterr().err
    assert json.loads(cfg.read_text())["repos"] == [str(workspace_dir)]


def test_up_argument_combinations(workspace_dir, fake_env, capsys):
    assert up.main(["up", "--no-desktop"]) == 1
    assert up.main(["up", str(workspace_dir / "api"), "--workspace", str(workspace_dir), "--repo", "api"]) == 1
    assert up.main(["up", "--workspace", str(workspace_dir)]) == 1
    assert up.main(["up", str(workspace_dir / "api"), "--repo", "api"]) == 1
    assert not fake_env.exists()


def test_legacy_up_repo_still_registers_one_repo(workspace_dir, fake_env):
    assert up.main(["up", str(workspace_dir / "api"), "--no-desktop"]) == 0
    data = json.loads((fake_env / "agent.json").read_text())
    assert data["repos"] == [str(workspace_dir / "api")] and data["sandbox"] is True
