import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import httpx
import pytest
from websockets.sync.client import unix_connect

from synapse.daemon.__main__ import _operator_token
from synapse.infrastructure.daemon_database import daemon_database

ROOT = Path(__file__).resolve().parents[1]


def _start(state):
    return subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(state),
                             "--migrations", str(ROOT / "migrations")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _wait_socket(process, state):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not (state / "agentd.sock").exists():
        if process.poll() is not None:
            raise AssertionError(f"daemon exited: {process.stderr.read().decode()}")
        time.sleep(0.02)
    assert (state / "agentd.sock").exists()


def test_daemon_unix_start_replay_single_owner_and_restart(tmp_path):
    with daemon_database(tmp_path / "synapse.sqlite", ROOT / "migrations") as db:
        db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,type,schema_version,payload_json,occurred_at) VALUES ('evt','system','one','test',1,'{}','now')")
    proc = _start(tmp_path)
    try:
        _wait_socket(proc, tmp_path)
        token = (tmp_path / "operator.token").read_text()
        assert len(token) == 43
        assert (tmp_path / "operator.token").stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "agentd.sock").stat().st_mode & 0o777 == 0o600
        headers = {"Authorization": "Bearer " + token}
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / "agentd.sock")), base_url="http://localhost") as client:
            assert client.get("/api/v1/system/health").status_code == 401
            assert client.get("/api/v1/system/health", headers=headers).json() == {"status": "ok"}
            assert client.get("/api/v1/events", headers=headers).json()["events"][0]["id"] == "evt"
        with unix_connect(str(tmp_path / "agentd.sock"), uri="ws://localhost/api/v1/events/stream?after=0", additional_headers=headers, open_timeout=3) as ws:
            assert '"id":"evt"' in ws.recv(timeout=3)
        second = _start(tmp_path)
        _, error = second.communicate(timeout=5)
        assert second.returncode != 0 and b"Another daemon owns the lock" in error
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
    assert proc.returncode in (0, -signal.SIGTERM)
    assert not (tmp_path / "agentd.sock").exists()
    restarted = _start(tmp_path)
    try:
        _wait_socket(restarted, tmp_path)
        assert (tmp_path / "operator.token").read_text() == token
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / "agentd.sock")), base_url="http://localhost") as client:
            assert client.get("/api/v1/system/health", headers=headers).status_code == 200
    finally:
        restarted.send_signal(signal.SIGTERM)
        restarted.communicate(timeout=8)
    assert restarted.returncode in (0, -signal.SIGTERM)
    assert not (tmp_path / "agentd.sock").exists()


def test_token_rejects_symlink_and_public_file(tmp_path):
    outside = tmp_path / "other"
    outside.write_text("x" * 43)
    token = tmp_path / "operator.token"
    token.symlink_to(outside)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            _operator_token(fd)
        token.unlink()
        token.write_text("x" * 43)
        token.chmod(0o644)
        with pytest.raises(PermissionError):
            _operator_token(fd)
    finally:
        os.close(fd)
