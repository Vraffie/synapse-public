"""Validation and fail-closed paths of the mock sandbox that need no bubblewrap."""
import os
import subprocess
import sys

import pytest

from synapse.infrastructure import mock_sandbox as ms


@pytest.fixture
def managed(tmp_path):
    root = tmp_path / "managed"
    root.mkdir(mode=0o700)
    work = root / "work"
    work.mkdir(mode=0o700)
    return root, work


@pytest.mark.parametrize("bad", [0, -1, 300.01, 1000, True, False, "5", None, float("nan")])
def test_timeout_bounds_rejected_before_anything_runs(managed, monkeypatch, bad):
    monkeypatch.setattr(ms, "_start", lambda *a: pytest.fail("must not start"))
    with pytest.raises(ValueError, match="timeout"):
        ms.run_fixed_mock(*managed, timeout=bad)


def test_worktree_must_be_below_root_not_equal_to_it(managed):
    root, _ = managed
    with pytest.raises(ValueError, match="below the managed root"):
        ms._worktree(root, root)


def test_worktree_outside_root_rejected(managed, tmp_path):
    root, _ = managed
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="below the managed root"):
        ms._worktree(root, outside)


def test_symlinked_worktree_is_rejected(managed, tmp_path):
    root, _ = managed
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    link = root / "link"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="user-owned directories"):
        ms._worktree(root, link)


def test_regular_file_is_not_a_worktree(managed):
    root, _ = managed
    f = root / "file"
    f.write_text("x")
    with pytest.raises(ValueError, match="user-owned directories"):
        ms._worktree(root, f)


def test_missing_worktree_raises_instead_of_being_created(managed):
    root, _ = managed
    with pytest.raises(OSError):
        ms._worktree(root, root / "nope")
    assert not (root / "nope").exists()


def test_dotdot_escape_is_rejected(managed, tmp_path):
    root, work = managed
    sibling = tmp_path / "sibling"
    sibling.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="below the managed root"):
        ms._worktree(root, work / ".." / ".." / "sibling")


def test_valid_worktree_resolves(managed):
    root, work = managed
    assert ms._worktree(root, work) == work.resolve()


def test_command_binds_only_the_worktree_and_drops_environment(managed):
    _, work = managed
    cmd = ms._command(work, "print(1)")
    assert cmd[0] == "/usr/bin/bwrap"
    assert "--unshare-all" in cmd and "--die-with-parent" in cmd and "--clearenv" in cmd
    binds = [cmd[i + 1:i + 3] for i, c in enumerate(cmd) if c == "--bind"]
    assert binds == [[str(work), "/work"]]
    assert cmd[-3:] == ["-I", "-c", "print(1)"]


def test_unlaunchable_bwrap_becomes_sandbox_unavailable(managed, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("bwrap")
    monkeypatch.setattr(ms.subprocess, "Popen", boom)
    with pytest.raises(ms.SandboxUnavailable):
        ms.run_fixed_mock(*managed)


def _fake_process(code: str):
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)


def test_nonzero_exit_fails_closed(managed, monkeypatch):
    monkeypatch.setattr(ms, "_start", lambda *a: _fake_process(
        "import sys; sys.stdout.write('synapse mock candidate v1\\n'); sys.exit(3)"))
    with pytest.raises(ms.SandboxUnavailable, match="failed or returned"):
        ms.run_fixed_mock(*managed)


def test_unexpected_output_fails_closed(managed, monkeypatch):
    monkeypatch.setattr(ms, "_start", lambda *a: _fake_process("print('evil')"))
    with pytest.raises(ms.SandboxUnavailable):
        ms.run_fixed_mock(*managed)


def test_exact_expected_output_is_returned(managed, monkeypatch):
    monkeypatch.setattr(ms, "_start", lambda *a: _fake_process(
        "import sys; sys.stdout.buffer.write(b'synapse mock candidate v1\\n')"))
    assert ms.run_fixed_mock(*managed) == b"synapse mock candidate v1\n"


def test_timeout_kills_the_whole_process_group(managed, monkeypatch):
    holder = {}

    def start(*a):
        holder["p"] = _fake_process("import time; time.sleep(60)")
        return holder["p"]
    monkeypatch.setattr(ms, "_start", start)
    with pytest.raises(subprocess.TimeoutExpired):
        ms.run_fixed_mock(*managed, timeout=0.3)
    proc = holder["p"]
    assert proc.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(proc.pid, 0)


def test_stop_escalates_to_sigkill_when_sigterm_is_ignored():
    proc = _fake_process(
        "import signal,time,sys; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "sys.stdout.write('ready\\n'); sys.stdout.flush(); time.sleep(60)")
    assert proc.stdout.readline() == b"ready\n"
    ms._stop(proc, grace=0.3)
    assert proc.returncode == -9


def test_stop_on_finished_process_is_a_noop():
    proc = _fake_process("pass")
    proc.communicate()
    ms._stop(proc)
    assert proc.returncode == 0
