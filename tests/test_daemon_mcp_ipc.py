import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def start(state, *, ipc=True):
    args = [sys.executable, '-m', 'synapse.daemon', '--state-dir', str(state), '--migrations', str(ROOT / 'migrations')]
    if ipc:
        args.append('--enable-mock-mcp-ipc')
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def wait_for(path, proc):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            raise AssertionError('daemon exited before socket creation')
        time.sleep(0.02)
    raise AssertionError('daemon did not create socket')


def wait_ready(state, proc):
    from synapse.cli.__main__ import _get
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            if _get(state, '/api/v1/system/health') == {'status': 'ok'}:
                return
        except (OSError, ValueError, RuntimeError):
            pass
        if proc.poll() is not None:
            raise AssertionError('daemon exited before readiness')
        time.sleep(0.02)
    raise AssertionError('daemon did not become ready')


def stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=8)


def test_opt_in_ipc_is_private_fail_closed_and_removed_on_restart(tmp_path):
    tmp_path.chmod(0o700)
    ipc = tmp_path / 'mock-mcp.sock'
    for _ in range(2):
        proc = start(tmp_path)
        try:
            wait_for(ipc, proc)
            wait_ready(tmp_path, proc)
            assert ipc.stat().st_mode & 0o077 == 0
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(3)
                deadline = time.monotonic() + 5
                while True:
                    try:
                        client.connect(str(ipc))
                        break
                    except ConnectionRefusedError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.02)
                client.sendall(b'{"credential":"not-a-session","operation":"claim","arguments":{"idempotency_key":"x"}}\n')
                assert json.loads(client.makefile('rb').readline()) == {'ok': False, 'error': 'request failed'}
        finally:
            stop(proc)
        assert proc.returncode == 0
        assert not ipc.exists()
        assert not (tmp_path / 'agentd.sock').exists()


def test_default_daemon_does_not_create_ipc(tmp_path):
    tmp_path.chmod(0o700)
    proc = start(tmp_path, ipc=False)
    try:
        wait_for(tmp_path / 'agentd.sock', proc)
        wait_ready(tmp_path, proc)
        assert not (tmp_path / 'mock-mcp.sock').exists()
    finally:
        stop(proc)
    assert proc.returncode == 0


def test_existing_ipc_path_is_preserved_on_startup_failure(tmp_path):
    tmp_path.chmod(0o700)
    stale = tmp_path / 'mock-mcp.sock'
    stale.write_text('preserve')
    proc = start(tmp_path)
    try:
        proc.communicate(timeout=8)
        assert proc.returncode != 0
        assert stale.read_text() == 'preserve'
        assert not (tmp_path / 'agentd.sock').exists()
    finally:
        if proc.poll() is None:
            stop(proc)
