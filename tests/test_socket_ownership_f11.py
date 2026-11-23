"""F11: socket lock for reclaim/bind, bind under a restrictive umask, inherited-socket identity."""
from contextlib import closing
import os
import socket
import stat

import pytest

from synapse.daemon import __main__ as daemon_main
from synapse.daemon.__main__ import _inherited_socket, _socket
from synapse.infrastructure.daemon_lock import DaemonAlreadyRunning


@pytest.fixture
def private(tmp_path):
    tmp_path.chmod(0o700)
    return tmp_path


def test_second_owner_is_refused_and_live_socket_untouched(private):
    # Review race: B reclaiming while A is between bind and listen. With the socket
    # lock B never reaches reclaim, so A's socket path is never unlinked.
    path = private / "agentd.sock"
    with _socket(path) as (first, _identity):
        inode = path.lstat().st_ino
        with pytest.raises(DaemonAlreadyRunning):
            with _socket(path):
                pytest.fail("second owner must not bind")
        assert path.lstat().st_ino == inode
        with closing(socket.socket(socket.AF_UNIX)) as client:
            client.connect(str(path))
            assert first.accept()[0] is not None
    assert not path.exists()
    with _socket(path):  # lock released with the first owner
        pass


def test_socket_is_never_created_with_wider_mode_than_0600(private, monkeypatch):
    path = private / "agentd.sock"
    modes = []
    real_chmod = os.chmod
    def spy(p, mode, **kw):  # observe the mode as it is just before the chmod
        modes.append(stat.S_IMODE(os.lstat(p).st_mode))
        return real_chmod(p, mode, **kw)
    monkeypatch.setattr(daemon_main.os, "chmod", spy)
    previous = os.umask(0)  # hostile/permissive inherited umask
    try:
        with _socket(path):
            assert modes and all(m & 0o077 == 0 for m in modes), modes
            assert stat.S_IMODE(path.lstat().st_mode) == 0o600
        assert os.umask(0) == 0  # umask restored after bind
    finally:
        os.umask(previous)


def test_inherited_socket_rejects_path_replaced_by_impostor(private):
    path = private / "agentd.sock"
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as real:
        real.bind(str(path))
        path.chmod(0o600)
        real.listen(4)
        info = path.lstat()
        with _inherited_socket(real.fileno(), path, (info.st_dev, info.st_ino)):
            pass  # genuine path accepted
        # Same-user process swaps the path for its own listener after the supervisor bound.
        path.unlink()
        with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as impostor:
            impostor.bind(str(path))
            path.chmod(0o600)
            impostor.listen(4)
            with pytest.raises(PermissionError, match="replaced"):
                with _inherited_socket(real.fileno(), path, (info.st_dev, info.st_ino)):
                    pass


def _wait_for_socket(path, process, what="process"):
    import time
    for _ in range(150):
        if path.exists():
            return
        if process.poll() is not None:
            break
        time.sleep(0.1)
    if process.poll() is None:
        process.terminate()
    stderr = process.communicate(timeout=10)[1]
    pytest.fail(f"{what} never created {path}; exit={process.returncode}; stderr={stderr!r}")


def _spawn(module, private, *extra):
    import subprocess, sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    cmd = [sys.executable, "-m", module, "--state-dir", str(private), *extra]
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    return cmd, env, subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE)


def _stop(process):
    process.terminate()
    process.wait(timeout=10)
    process.stderr.close()


def test_supervisor_cli_refuses_second_instance(private):
    import subprocess
    cmd, env, first = _spawn("synapse.supervisor", private, "--max-restarts", "0")
    try:
        _wait_for_socket(private / "agentd.sock", first, "first supervisor")
        inode = (private / "agentd.sock").lstat().st_ino
        second = subprocess.run(cmd, env=env, capture_output=True, timeout=30)
        assert second.returncode == 1 and b"DaemonAlreadyRunning" in second.stderr, second.stderr
        assert (private / "agentd.sock").lstat().st_ino == inode  # first owner's socket not unlinked
    finally:
        _stop(first)


def test_standalone_daemon_refused_while_supervisor_holds_lock(private):
    import subprocess, sys
    _, env, sup = _spawn("synapse.supervisor", private, "--max-restarts", "0")
    try:
        _wait_for_socket(private / "agentd.sock", sup, "supervisor")
        # The supervisor binds the socket before its child daemon takes the database lock.
        # A standalone daemon started in that window wins the database lock, and the child
        # (max-restarts 0) then dies and the supervisor unlinks its socket. Wait until the
        # child owns the database lock (operator.token is created only under it).
        _wait_for_socket(private / "operator.token", sup, "supervisor's daemon")
        inode = (private / "agentd.sock").lstat().st_ino
        cmd = [sys.executable, "-m", "synapse.daemon", "--state-dir", str(private)]
        daemon = subprocess.run(cmd, env=env, capture_output=True, timeout=30)
        assert daemon.returncode != 0 and b"DaemonAlreadyRunning" in daemon.stderr, daemon.stderr
        assert (private / "agentd.sock").lstat().st_ino == inode
    finally:
        _stop(sup)


def test_supervisor_refused_while_standalone_daemon_holds_lock(private):
    import subprocess, sys
    _, env, daemon = _spawn("synapse.daemon", private)
    try:
        _wait_for_socket(private / "agentd.sock", daemon, "standalone daemon")
        inode = (private / "agentd.sock").lstat().st_ino
        sup = subprocess.run([sys.executable, "-m", "synapse.supervisor", "--state-dir", str(private)],
                             env=env, capture_output=True, timeout=30)
        assert sup.returncode == 1 and b"DaemonAlreadyRunning" in sup.stderr, sup.stderr
        assert (private / "agentd.sock").lstat().st_ino == inode
    finally:
        _stop(daemon)


def test_socket_refuses_non_private_state_dir(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(PermissionError):
        with _socket(tmp_path / "agentd.sock"):
            pytest.fail("must not bind in a non-private directory")
    assert not (tmp_path / "agentd.sock").exists()


def test_stale_lock_file_from_crashed_process_is_reacquired(private):
    import subprocess, sys, signal
    path = private / "agentd.sock"
    code = ("import sys,time\n"
            "from synapse.infrastructure.daemon_lock import daemon_lock\n"
            "with daemon_lock(sys.argv[1]):\n"
            "    print('locked', flush=True)\n"
            "    time.sleep(60)\n")
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    holder = subprocess.Popen([sys.executable, "-c", code, str(path) + ".lock"], stdout=subprocess.PIPE,
                              env={**os.environ, "PYTHONPATH": root})
    try:
        assert holder.stdout.readline().strip() == b"locked"
        with pytest.raises(DaemonAlreadyRunning):
            with _socket(path):
                pytest.fail("lock is held")
        holder.send_signal(signal.SIGKILL)  # crash: lock file remains, flock is gone
        holder.wait(timeout=10)
    finally:
        holder.stdout.close()
    assert (private / "agentd.sock.lock").exists()
    with _socket(path) as (_sock, identity):
        assert identity == (path.lstat().st_dev, path.lstat().st_ino)


def test_socket_identity_is_captured_at_bind_before_chmod(private, monkeypatch):
    path = private / "agentd.sock"
    seen = []
    real_chmod = os.chmod
    def spy(p, mode, **kw):
        info = os.lstat(p)
        seen.append((info.st_dev, info.st_ino))
        return real_chmod(p, mode, **kw)
    monkeypatch.setattr(daemon_main.os, "chmod", spy)
    with _socket(path) as (_sock, identity):
        assert seen == [identity]


class _FailingBind(socket.socket):
    def bind(self, address):
        raise OSError("bind failed")


def test_bind_failure_releases_lock_and_restores_umask(private, monkeypatch):
    import types
    path = private / "agentd.sock"
    fake = types.SimpleNamespace(socket=_FailingBind, AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM)
    monkeypatch.setattr(daemon_main, "socket", fake)
    previous = os.umask(0o022)
    try:
        with pytest.raises(OSError, match="bind failed"):
            with _socket(path):
                pytest.fail("bind failed")
        assert os.umask(0o022) == 0o022  # restored
    finally:
        os.umask(previous)
    assert not path.exists()
    monkeypatch.undo()
    with _socket(path):  # lock was released
        pass


def test_failure_after_bind_unlinks_socket_and_releases_lock(private, monkeypatch):
    path = private / "agentd.sock"
    def boom(*a, **kw):
        raise OSError("chmod failed")
    monkeypatch.setattr(daemon_main.os, "chmod", boom)
    with pytest.raises(OSError, match="chmod failed"):
        with _socket(path):
            pytest.fail("chmod failed")
    assert not path.exists()
    monkeypatch.undo()
    with _socket(path):
        pass


def test_identity_parser_accepts_only_two_unsigned_integers():
    import argparse
    assert daemon_main._identity("12:345") == (12, 345)
    for bad in ("-1:5", "1:-5", "1:2:3", " 1:2", "1:2 ", "1: 2", "1:2\n", "1", "a:b", "", ":", "+1:2"):
        with pytest.raises(argparse.ArgumentTypeError):
            daemon_main._identity(bad)


def test_socket_identity_without_socket_fd_is_rejected(private, capsys):
    with pytest.raises(SystemExit) as exit_info:
        daemon_main.main(["--state-dir", str(private), "--socket-identity", "1:2"])
    assert exit_info.value.code == 2
    assert "--socket-identity requires --socket-fd" in capsys.readouterr().err
    assert not (private / "agentd.sock").exists()


def test_supervisor_passes_socket_identity_to_child(private, monkeypatch):
    from synapse.supervisor import __main__ as sup
    commands = []
    class FakeChild:
        pid, returncode = 1, 0
        def __init__(self, command, **kw):
            commands.append(command)
        def poll(self):
            return 0
    monkeypatch.setattr(sup.subprocess, "Popen", FakeChild)
    assert sup.run(private, private / "migrations") == 0
    command = commands[0]
    assert "--socket-fd" in command
    value = command[command.index("--socket-identity") + 1]
    dev, ino = daemon_main._identity(value)
    assert value == f"{dev}:{ino}"


def test_swapped_socket_makes_child_exit_nonzero(private):
    import subprocess, sys
    from pathlib import Path
    path = private / "agentd.sock"
    root = Path(__file__).resolve().parents[1]
    with _socket(path) as (real, identity):
        path.unlink()  # same-user swap after the supervisor bound
        with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as impostor:
            impostor.bind(str(path))
            path.chmod(0o600)
            impostor.listen(4)
            cmd = [sys.executable, "-m", "synapse.daemon", "--state-dir", str(private),
                   "--socket-fd", str(real.fileno()), "--socket-identity", f"{identity[0]}:{identity[1]}"]
            result = subprocess.run(cmd, pass_fds=(real.fileno(),), capture_output=True, timeout=30,
                                    env={**os.environ, "PYTHONPATH": str(root / "src")})
            assert result.returncode == daemon_main.EXIT_SOCKET_IDENTITY, result.stderr
            assert b"replaced" in result.stderr
            path.unlink()  # the foreign inode; _socket's cleanup then sees nothing to remove


def test_supervisor_does_not_restart_child_on_socket_identity_failure(private, monkeypatch):
    from synapse.supervisor import __main__ as sup
    launches = []
    class FakeChild:
        pid = 1
        returncode = daemon_main.EXIT_SOCKET_IDENTITY
        def __init__(self, command, **kw):
            launches.append(command)
        def poll(self):
            return self.returncode
    monkeypatch.setattr(sup.subprocess, "Popen", FakeChild)
    assert sup.run(private, private / "migrations", max_restarts=5, base_delay=0.01) == 1
    assert len(launches) == 1
