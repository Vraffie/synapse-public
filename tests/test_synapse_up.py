"""scripts/synapse_up.py: config merge, sandbox stays on, executable moved out of protected paths."""
import importlib.util
import json
import os
import stat
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
