from contextlib import closing
from pathlib import Path
import os
import signal
import sqlite3
import socket
import subprocess
import sys
import time

import httpx
import pytest

from synapse.daemon.__main__ import _inherited_socket

ROOT = Path(__file__).resolve().parents[1]


def test_rejects_non_listening_wrong_path_and_public_socket(tmp_path):
    path = tmp_path / "agentd.sock"
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as sock:
        sock.bind(str(path))
        path.chmod(0o600)
        with pytest.raises(ValueError, match="listen"):
            with _inherited_socket(sock.fileno(), path):
                pass
        sock.listen(10)
        path.chmod(0o666)
        with pytest.raises(PermissionError):
            with _inherited_socket(sock.fileno(), path):
                pass
        path.chmod(0o600)
        other = tmp_path / "other.sock"
        with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as other_sock:
            other_sock.bind(str(other))
            other.chmod(0o600)
            other_sock.listen(1)
            with pytest.raises(ValueError, match="configured"):
                with _inherited_socket(other_sock.fileno(), path):
                    pass
        with _inherited_socket(sock.fileno(), path) as duplicate:
            assert duplicate.fileno() != sock.fileno()
        assert path.exists() and sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)


def test_supervisor_listener_survives_sigkill_and_daemon_restart(tmp_path):
    path = tmp_path / "agentd.sock"
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(128)
        original = path.stat().st_ino
        def start():
            return subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(tmp_path),
                                     "--migrations", str(ROOT / "migrations"), "--socket-fd", str(listener.fileno())],
                                    pass_fds=(listener.fileno(),), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def health(process):
            deadline = time.monotonic() + 8
            while not (tmp_path / "operator.token").exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError("daemon exited before token creation")
                time.sleep(0.02)
            token = (tmp_path / "operator.token").read_text()
            with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), base_url="http://localhost", timeout=8) as client:
                response = client.get("/api/v1/system/health", headers={"Authorization": "Bearer " + token})
            assert response.status_code == 200
        first = start()
        second = None
        try:
            health(first)
            first.kill()
            first.communicate(timeout=8)
            assert path.exists() and path.stat().st_ino == original
            second = start()
            health(second)
            assert path.stat().st_ino == original
        finally:
            for process in (first, second):
                if process is not None and process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.communicate(timeout=8)
        assert path.exists() and path.stat().st_ino == original


def test_socket_activation_recovers_active_mock_after_sigkill(tmp_path):
    script = '''
import asyncio
from pathlib import Path
import sys
from synapse.providers.mock import MockProvider
from synapse.providers.contracts import ProviderEvent
from synapse.daemon.__main__ import run
async def blocked(self, handle):
    yield ProviderEvent("started")
    await asyncio.Event().wait()
MockProvider.stream = blocked
run(Path(sys.argv[1]), Path(sys.argv[2]), socket_fd=int(sys.argv[3]))
'''
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(tmp_path / "agentd.sock"))
    (tmp_path / "agentd.sock").chmod(0o600)
    listener.listen(128)
    command = [sys.executable, "-c", script, str(tmp_path), str(ROOT / "migrations"), str(listener.fileno())]
    process = subprocess.Popen(command, pass_fds=(listener.fileno(),), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    second = None
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "operator.token").exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("daemon exited")
            time.sleep(0.02)
        token = (tmp_path / "operator.token").read_text()
        headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "publish", "X-Correlation-ID": "trace"}
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / "agentd.sock")), base_url="http://localhost") as client:
            assert client.post("/api/v1/workflows", headers=headers, json={"workflow_id":"wf", "version":"1", "nodes":[{"id":"build", "dependencies":[]}]}).status_code == 201
            headers["Idempotency-Key"] = "create"
            run_id = client.post("/api/v1/runs", headers=headers, json={"workflow_id":"wf", "workflow_version":"1"}).json()["run_id"]
            headers["Idempotency-Key"] = "start"
            assert client.post(f"/api/v1/runs/{run_id}/mock-start", headers=headers).status_code == 202
            while time.monotonic() < deadline:
                with closing(sqlite3.connect(f"file:{tmp_path / 'synapse.sqlite'}?mode=ro", uri=True)) as db:
                    row = db.execute("SELECT state FROM task_attempts").fetchone()
                if row == ("running",):
                    break
                time.sleep(0.02)
            assert row == ("running",)
        process.kill()
        process.communicate(timeout=8)
        second = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(tmp_path), "--migrations", str(ROOT / "migrations"), "--socket-fd", str(listener.fileno())], pass_fds=(listener.fileno(),), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if second.poll() is not None:
                raise AssertionError("daemon restart failed")
            with closing(sqlite3.connect(f"file:{tmp_path / 'synapse.sqlite'}?mode=ro", uri=True)) as db:
                if db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone() == ("recovering",):
                    break
            time.sleep(0.02)
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / "agentd.sock")), base_url="http://localhost") as client:
            replay = client.post(f"/api/v1/runs/{run_id}/mock-start", headers=headers)
            assert replay.status_code == 202 and replay.headers["idempotency-replayed"] == "true"
        with closing(sqlite3.connect(f"file:{tmp_path / 'synapse.sqlite'}?mode=ro", uri=True)) as db:
            assert db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone() == ("recovering",)
            assert db.execute("SELECT state FROM tasks WHERE run_id=?", (run_id,)).fetchone() == ("blocked",)
            assert db.execute("SELECT state FROM task_attempts").fetchone() == ("unknown",)
    finally:
        for proc in (process, second):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.communicate(timeout=8)

        listener.close()
