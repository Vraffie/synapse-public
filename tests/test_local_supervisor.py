from pathlib import Path
import os
import queue
import re
import signal
import subprocess
import sys
from threading import Thread
import time

import httpx
import pytest

from synapse.supervisor.__main__ import run

ROOT = Path(__file__).resolve().parents[1]


def test_invalid_policy_fails_without_socket(tmp_path):
    with pytest.raises(ValueError):
        run(tmp_path, ROOT / "migrations", base_delay=2, max_delay=1)
    assert not (tmp_path / "agentd.sock").exists()


def test_existing_path_is_not_replaced(tmp_path):
    path = tmp_path / "agentd.sock"
    path.write_text("unrelated")
    with pytest.raises(OSError):
        run(tmp_path, ROOT / "migrations")
    assert path.read_text() == "unrelated"


def test_supervisor_restarts_killed_child_and_stops_gracefully(tmp_path):
    command = [sys.executable, "-m", "synapse.supervisor", "--state-dir", str(tmp_path),
               "--migrations", str(ROOT / "migrations"), "--base-delay", "0.01", "--max-delay", "0.02"]
    supervisor = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)
    lines = queue.Queue()
    def collect():
        for line in supervisor.stderr:
            lines.put(line)
    reader = Thread(target=collect, daemon=True)
    reader.start()
    def next_pid():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                if supervisor.poll() is not None:
                    raise AssertionError("supervisor exited unexpectedly")
                continue
            match = re.search(r"started daemon pid=(\d+)", line)
            if match:
                return int(match.group(1))
        raise AssertionError("daemon did not start")
    def health():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (tmp_path / "operator.token").exists():
                token = (tmp_path / "operator.token").read_text()
                try:
                    with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / "agentd.sock")), base_url="http://localhost", timeout=1) as client:
                        if client.get("/api/v1/system/health", headers={"Authorization": "Bearer " + token}).status_code == 200:
                            return
                except httpx.HTTPError:
                    pass
            time.sleep(0.02)
        raise AssertionError("daemon did not become healthy")
    second = None
    try:
        first = next_pid()
        health()
        inode = (tmp_path / "agentd.sock").stat().st_ino
        os.kill(first, signal.SIGKILL)
        second = next_pid()
        assert second != first
        health()
        assert (tmp_path / "agentd.sock").stat().st_ino == inode
        supervisor.send_signal(signal.SIGTERM)
        assert supervisor.wait(timeout=10) == 0
        assert not (tmp_path / "agentd.sock").exists()
        with pytest.raises(ProcessLookupError):
            os.kill(second, 0)
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.wait(timeout=10)
        reader.join(timeout=2)


def test_restart_limit_is_bounded_and_socket_cleaned(tmp_path):
    command = [sys.executable, "-m", "synapse.supervisor", "--state-dir", str(tmp_path),
               "--migrations", str(tmp_path / "missing"), "--max-restarts", "1",
               "--base-delay", "0.01", "--max-delay", "0.02"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert result.stderr.count("started daemon pid=") == 2
    assert "restart limit reached" in result.stderr
    assert not (tmp_path / "agentd.sock").exists()


def test_shutdown_interrupts_backoff_without_starting_another_child(tmp_path):
    command = [sys.executable, "-m", "synapse.supervisor", "--state-dir", str(tmp_path),
               "--migrations", str(tmp_path / "missing"), "--max-restarts", "5",
               "--base-delay", "5", "--max-delay", "5"]
    supervisor = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)
    lines = queue.Queue()
    def collect():
        for line in supervisor.stderr:
            lines.put(line)
    reader = Thread(target=collect, daemon=True)
    reader.start()
    seen = []
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            seen.append(line)
            if "retry 1 in 5s" in line:
                break
        else:
            raise AssertionError("supervisor did not reach backoff")
        supervisor.send_signal(signal.SIGTERM)
        assert supervisor.wait(timeout=2) == 0
        reader.join(timeout=2)
        while not lines.empty():
            seen.append(lines.get_nowait())
        assert sum("started daemon pid=" in line for line in seen) == 1
        assert not (tmp_path / "agentd.sock").exists()
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=2)


def test_supervisor_recovers_active_mock_after_daemon_sigkill(tmp_path):
    import sqlite3
    from contextlib import closing

    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "sitecustomize.py").write_text('''
import asyncio
from synapse.providers.mock import MockProvider
from synapse.providers.contracts import ProviderEvent
async def blocked(self, handle):
    yield ProviderEvent("started")
    await asyncio.Event().wait()
MockProvider.stream = blocked
''')
    env = dict(os.environ)
    env["PYTHONPATH"] = str(harness) + os.pathsep + env.get("PYTHONPATH", "")
    command = [sys.executable, "-m", "synapse.supervisor", "--state-dir", str(tmp_path),
               "--migrations", str(ROOT / "migrations"), "--base-delay", "0.01", "--max-delay", "0.02"]
    supervisor = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
    lines = queue.Queue()
    def collect():
        for line in supervisor.stderr:
            lines.put(line)
    reader = Thread(target=collect, daemon=True)
    reader.start()
    def next_pid():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                if supervisor.poll() is not None:
                    raise AssertionError("supervisor exited")
                continue
            match = re.search(r"started daemon pid=(\d+)", line)
            if match:
                return int(match.group(1))
        raise AssertionError("daemon did not start")
    try:
        first = next_pid()
        deadline = time.monotonic() + 10
        while not (tmp_path / "operator.token").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        token = (tmp_path / "operator.token").read_text()
        headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "publish", "X-Correlation-ID": "trace"}
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / "agentd.sock")), base_url="http://localhost", timeout=8) as client:
            assert client.post("/api/v1/workflows", headers=headers, json={"workflow_id": "wf", "version": "1", "nodes": [{"id": "build", "dependencies": []}]}).status_code == 201
            headers["Idempotency-Key"] = "create"
            run_id = client.post("/api/v1/runs", headers=headers, json={"workflow_id": "wf", "workflow_version": "1"}).json()["run_id"]
            headers["Idempotency-Key"] = "start"
            assert client.post(f"/api/v1/runs/{run_id}/mock-start", headers=headers).status_code == 202
        while time.monotonic() < deadline:
            with closing(sqlite3.connect(f"file:{tmp_path / 'synapse.sqlite'}?mode=ro", uri=True)) as db:
                attempt = db.execute("SELECT state FROM task_attempts").fetchone()
            if attempt == ("running",):
                break
            time.sleep(0.02)
        assert attempt == ("running",)
        inode = (tmp_path / "agentd.sock").stat().st_ino
        os.kill(first, signal.SIGKILL)
        assert next_pid() != first
        while time.monotonic() < deadline:
            with closing(sqlite3.connect(f"file:{tmp_path / 'synapse.sqlite'}?mode=ro", uri=True)) as db:
                run_state = db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
                attempt_state = db.execute("SELECT state FROM task_attempts").fetchone()
            if run_state == ("recovering",):
                break
            time.sleep(0.02)
        assert run_state == ("recovering",) and attempt_state == ("unknown",)
        assert (tmp_path / "agentd.sock").stat().st_ino == inode
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / "agentd.sock")), base_url="http://localhost", timeout=8) as client:
            replay = client.post(f"/api/v1/runs/{run_id}/mock-start", headers=headers)
            assert replay.status_code == 202 and replay.headers["idempotency-replayed"] == "true"
    finally:
        if supervisor.poll() is None:
            supervisor.send_signal(signal.SIGTERM)
            supervisor.wait(timeout=10)
        reader.join(timeout=2)


def test_empty_operator_token_from_interrupted_creation_is_repaired(tmp_path):
    from synapse.daemon.__main__ import _operator_token
    tmp_path.chmod(0o700)
    (tmp_path / "operator.token").touch(mode=0o600)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        token = _operator_token(fd)
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
        assert (tmp_path / "operator.token").read_text() == token
        assert _operator_token(fd) == token
    finally:
        os.close(fd)


def test_healthy_runtime_resets_restart_budget(tmp_path):
    command = [sys.executable, "-m", "synapse.supervisor", "--state-dir", str(tmp_path),
               "--migrations", str(tmp_path / "missing"), "--max-restarts", "1",
               "--base-delay", "0.01", "--max-delay", "0.02", "--healthy-seconds", "0"]
    supervisor = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            supervisor.wait(timeout=8)  # budget keeps resetting, so it never gives up (8 s: daemon import alone ~2 s)
    finally:
        supervisor.terminate()
        stderr = supervisor.communicate(timeout=10)[1]
    assert stderr.count("started daemon pid=") > 2
    assert "restart limit reached" not in stderr
