"""Stale sockets are reclaimed under daemon ownership; anything else is preserved."""
import errno
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest

from synapse.daemon.__main__ import _socket
from synapse.infrastructure.socket_reclaim import reclaim_stale_socket

ROOT = Path(__file__).resolve().parents[1]


def test_bound_but_not_listening_socket_is_reclaimed(tmp_path):
    # Under daemon ownership a refused, user-owned socket is a crash remnant.
    # Known limit: an unrelated process that bound but has not yet called
    # listen() on this private path would be indistinguishable.
    path = tmp_path / 'agentd.sock'
    owner = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        owner.bind(str(path))
        original = path.lstat().st_ino
        with _socket(path) as (sock, _identity):
            assert sock.getsockname() == str(path)
            assert path.lstat().st_ino != original or sock is not owner
        assert not path.exists()
    finally:
        owner.close()


def test_live_listener_is_never_replaced(tmp_path):
    path = tmp_path / 'agentd.sock'
    owner = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        owner.bind(str(path))
        owner.listen(1)
        inode = path.lstat().st_ino
        with pytest.raises(OSError) as error:
            with _socket(path):
                pytest.fail('live socket was replaced')
        assert error.value.errno == errno.EADDRINUSE
        assert path.lstat().st_ino == inode
    finally:
        owner.close()


def test_non_socket_paths_are_never_removed(tmp_path):
    regular = tmp_path / 'file.sock'
    regular.write_text('unrelated')
    link = tmp_path / 'link.sock'
    link.symlink_to(regular)
    for path in (regular, link):
        with pytest.raises(OSError) as error:
            reclaim_stale_socket(path)
        assert error.value.errno == errno.EADDRINUSE
    assert regular.read_text() == 'unrelated' and link.is_symlink()
    assert reclaim_stale_socket(tmp_path / 'missing.sock') is False


def test_sigkill_restart_reclaims_stale_socket(tmp_path):
    args = [sys.executable, '-m', 'synapse.daemon', '--state-dir', str(tmp_path),
            '--migrations', str(ROOT / 'migrations')]
    path = tmp_path / 'agentd.sock'

    def wait_for_socket(process):
        deadline = time.monotonic() + 8
        while not path.exists():
            if process.poll() is not None:
                raise AssertionError(process.stderr.read().decode())
            if time.monotonic() >= deadline:
                raise AssertionError('daemon did not bind')
            time.sleep(0.02)

    daemon = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    restarted = None
    try:
        wait_for_socket(daemon)
        inode = path.lstat().st_ino
        daemon.send_signal(signal.SIGKILL)
        daemon.communicate(timeout=8)
        assert daemon.returncode == -signal.SIGKILL
        assert path.exists()  # the stale remnant
        restarted = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 8
        while True:
            if restarted.poll() is not None:
                raise AssertionError(restarted.stderr.read().decode())
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.connect(str(path))
                probe.close()
                break
            except OSError:
                probe.close()
                if time.monotonic() >= deadline:
                    raise AssertionError('restarted daemon never listened')
                time.sleep(0.02)
        restarted.send_signal(signal.SIGTERM)
        restarted.communicate(timeout=8)
        # The socket accepts connections slightly before uvicorn installs its
        # handlers, so an early SIGTERM may terminate the process directly.
        assert restarted.returncode in (0, -signal.SIGTERM)
    finally:
        for proc in (daemon, restarted):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=8)
