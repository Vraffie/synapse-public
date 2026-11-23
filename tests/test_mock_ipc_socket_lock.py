"""Mock IPC socket: lock around reclaim/bind, restrictive umask, cleanup on failure."""
import asyncio
from contextlib import closing
import os
import socket
import stat
import types

import pytest

from synapse.infrastructure.daemon_lock import DaemonAlreadyRunning
from synapse.mcp_gateway import ipc


@pytest.fixture
def private(tmp_path):
    tmp_path.chmod(0o700)
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def test_second_instance_refused_and_live_socket_untouched(private):
    path = private / "mock.sock"
    async def scenario():
        async with ipc.private_ipc_server(path, None):
            inode = path.lstat().st_ino
            with pytest.raises(DaemonAlreadyRunning):
                async with ipc.private_ipc_server(path, None):
                    pytest.fail("second instance must not bind")
            assert path.lstat().st_ino == inode
        assert not path.exists()
        async with ipc.private_ipc_server(path, None):  # lock released
            pass
    run(scenario())


def test_never_created_wider_than_0600(private, monkeypatch):
    path = private / "mock.sock"
    modes = []
    real_chmod = os.chmod
    def spy(p, mode, **kw):
        modes.append(stat.S_IMODE(os.lstat(p).st_mode))
        return real_chmod(p, mode, **kw)
    monkeypatch.setattr(ipc.os, "chmod", spy)
    async def scenario():
        async with ipc.private_ipc_server(path, None):
            assert modes and all(m & 0o077 == 0 for m in modes), modes
            assert stat.S_IMODE(path.lstat().st_mode) == 0o600
    previous = os.umask(0)
    try:
        run(scenario())
        assert os.umask(0) == 0
    finally:
        os.umask(previous)


class _FailingBind(socket.socket):
    def bind(self, address):
        raise OSError("bind failed")


def test_bind_failure_releases_lock_unlinks_and_restores_umask(private, monkeypatch):
    path = private / "mock.sock"
    fake = types.SimpleNamespace(socket=_FailingBind, AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM)
    monkeypatch.setattr(ipc, "socket", fake)
    async def failing():
        async with ipc.private_ipc_server(path, None):
            pytest.fail("bind failed")
    previous = os.umask(0o022)
    try:
        with pytest.raises(OSError, match="bind failed"):
            run(failing())
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(previous)
    assert not path.exists()
    monkeypatch.undo()
    async def again():
        async with ipc.private_ipc_server(path, None):
            pass
    run(again())  # lock was released


def test_failure_after_bind_unlinks_socket_and_releases_lock(private, monkeypatch):
    path = private / "mock.sock"
    def boom(*a, **kw):
        raise OSError("chmod failed")
    monkeypatch.setattr(ipc.os, "chmod", boom)
    async def failing():
        async with ipc.private_ipc_server(path, None):
            pytest.fail("chmod failed")
    with pytest.raises(OSError, match="chmod failed"):
        run(failing())
    assert not path.exists()
    monkeypatch.undo()
    async def again():
        async with ipc.private_ipc_server(path, None):
            pass
    run(again())


def test_stale_socket_still_reclaimed(private):
    path = private / "mock.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # leaves a refused-connection socket file behind
    assert path.exists()
    async def scenario():
        async with ipc.private_ipc_server(path, None):
            with closing(socket.socket(socket.AF_UNIX)) as client:
                client.connect(str(path))
    run(scenario())
    assert not path.exists()
