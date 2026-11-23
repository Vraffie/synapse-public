import os
from pathlib import Path
import subprocess
import sys

import pytest

from synapse.infrastructure.daemon_lock import DaemonAlreadyRunning, daemon_lock


def test_lock_rejects_concurrent_owner_and_releases_on_close(tmp_path):
    path = tmp_path / "daemon.lock"
    with daemon_lock(path):
        with pytest.raises(DaemonAlreadyRunning):
            with daemon_lock(path):
                pytest.fail("second lock acquired")
    with daemon_lock(path):
        assert path.is_file()


def test_lock_releases_when_owner_process_exits(tmp_path):
    path = tmp_path / "daemon.lock"
    code = (
        "import sys\n"
        "from synapse.infrastructure.daemon_lock import daemon_lock\n"
        "with daemon_lock(sys.argv[1]):\n"
        " print('locked', flush=True)\n"
        " sys.stdin.readline()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(DaemonAlreadyRunning):
            with daemon_lock(path):
                pytest.fail("second process acquired lock")
    finally:
        process.stdin.close()
        process.wait(timeout=5)
        process.stdout.close()
    assert process.returncode == 0
    with daemon_lock(path):
        pass


def test_rejects_symlink_and_public_lock_file(tmp_path):
    target = tmp_path / "target"
    target.write_text("")
    symlink = tmp_path / "link"
    symlink.symlink_to(target)
    with pytest.raises(OSError):
        with daemon_lock(symlink):
            pytest.fail("symlink accepted")
    target.chmod(0o644)
    with pytest.raises(PermissionError, match="private"):
        with daemon_lock(target):
            pytest.fail("public lock accepted")


def test_lock_releases_after_owner_is_terminated(tmp_path):
    path = tmp_path / "daemon.lock"
    code = (
        "import sys\n"
        "from synapse.infrastructure.daemon_lock import daemon_lock\n"
        "with daemon_lock(sys.argv[1]):\n"
        " print('locked', flush=True)\n"
        " sys.stdin.readline()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        process.terminate()
        process.wait(timeout=5)
        with daemon_lock(path):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()



def test_rejects_non_private_directory(tmp_path):
    directory = tmp_path / "public"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(PermissionError, match="directory"):
        with daemon_lock(directory / "daemon.lock"):
            pytest.fail("public directory accepted")



def test_rejects_hardlinked_lock_file(tmp_path):
    path = tmp_path / "daemon.lock"
    path.touch(mode=0o600)
    os.link(path, tmp_path / "alias.lock")
    with pytest.raises(PermissionError, match="one link"):
        with daemon_lock(path):
            pytest.fail("hardlinked lock accepted")
