from contextlib import closing
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from synapse.api.app import create_app
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.workflow_store import create_run, publish_workflow

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "publish-1", "X-Correlation-ID": "trace-1"}
WORKFLOW = {"workflow_id": "wf", "version": "1", "nodes": [{"id": "build", "dependencies": []}, {"id": "review", "dependencies": ["build"]}]}


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "db"
    with closing(open_database(path)) as db:
        apply_migrations(db, ROOT / "migrations")
    def publish(wid, version, nodes, key):
        with closing(open_database(path)) as db:
            return publish_workflow(db, workflow_id=wid, version=version, nodes=nodes, principal="local-operator", idempotency_key=key)
    def create(wid, version, key):
        with closing(open_database(path)) as db:
            return create_run(db, workflow_id=wid, version=version, principal="local-operator", idempotency_key=key)
    app = create_app(token=TOKEN, read_events=lambda *_: [], publish_workflow=publish, create_run=create)
    with TestClient(app, client=None) as instance:
        yield instance


def test_publish_create_and_replay(client):
    assert client.post("/api/v1/workflows", json=WORKFLOW).status_code == 401
    assert client.post("/api/v1/workflows", headers={**HEADERS, "Origin": "https://evil"}, json=WORKFLOW).status_code == 401
    first = client.post("/api/v1/workflows", headers=HEADERS, json=WORKFLOW)
    assert first.status_code == 201
    assert first.headers["x-correlation-id"] == "trace-1" and first.headers["idempotency-replayed"] == "false"
    assert first.headers["cache-control"] == "no-store"
    second = client.post("/api/v1/workflows", headers=HEADERS, json=WORKFLOW)
    assert second.json() == first.json() and second.headers["idempotency-replayed"] == "true"
    modified = {**WORKFLOW, "version": "2"}
    assert client.post("/api/v1/workflows", headers=HEADERS, json=modified).status_code == 409
    assert client.post("/api/v1/workflows", headers={**HEADERS, "Idempotency-Key": "another"}, json=WORKFLOW).status_code == 409
    request = {"workflow_id": "wf", "workflow_version": "1"}
    response = client.post("/api/v1/runs", headers={**HEADERS, "Idempotency-Key": "run"}, json=request)
    assert response.status_code == 202 and response.json()["state"] == "queued"
    repeat = client.post("/api/v1/runs", headers={**HEADERS, "Idempotency-Key": "run"}, json=request)
    assert repeat.json() == response.json() and repeat.headers["idempotency-replayed"] == "true"


def test_bad_headers_inputs_and_bounded_body(client):
    assert client.post("/api/v1/workflows", headers={"Authorization": HEADERS["Authorization"]}, json=WORKFLOW).status_code == 400
    duplicate = [("Authorization", HEADERS["Authorization"]), ("Idempotency-Key", "a"), ("Idempotency-Key", "b"), ("X-Correlation-ID", "c")]
    assert client.post("/api/v1/workflows", headers=duplicate, json=WORKFLOW).status_code == 400
    assert client.post("/api/v1/workflows", headers=HEADERS, content="{} ").status_code == 415
    headers = {**HEADERS, "Content-Type": "application/json"}
    assert client.post("/api/v1/workflows", headers=headers, content='{"workflow_id":"a","workflow_id":"b"}').status_code == 422
    assert client.post("/api/v1/workflows", headers=headers, content=b" " * (1024 * 1024 + 1)).status_code == 413
    cyclic = {**WORKFLOW, "nodes": [{"id": "a", "dependencies": ["a"]}]}
    assert client.post("/api/v1/workflows", headers=HEADERS, json=cyclic).status_code == 422
    assert client.post("/api/v1/workflows", headers=HEADERS, json={**WORKFLOW, "extra": 1}).status_code == 422
    assert client.post("/api/v1/runs", headers=HEADERS, json={"workflow_id": "missing", "workflow_version": "1"}).status_code == 422
    schema = client.get("/openapi.json", headers=HEADERS).json()
    assert schema["paths"]["/api/v1/workflows"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["nodes"]["items"]["properties"]["id"]


def test_real_daemon_uds_publish_and_queue(tmp_path):
    import signal
    import subprocess
    import sys
    import time
    import httpx
    proc = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(tmp_path), "--migrations", str(ROOT / "migrations")], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / "agentd.sock").exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError("daemon exited")
            time.sleep(0.02)
        token = (tmp_path / "operator.token").read_text()
        headers = {**HEADERS, "Authorization": "Bearer " + token}
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / "agentd.sock")), base_url="http://localhost") as c:
            assert c.post("/api/v1/workflows", headers=headers, json=WORKFLOW).status_code == 201
            result = c.post("/api/v1/runs", headers={**headers, "Idempotency-Key": "run"}, json={"workflow_id": "wf", "workflow_version": "1"})
            assert result.status_code == 202
            snapshot = c.get("/api/v1/runs/" + result.json()["run_id"], headers=headers)
            assert snapshot.json()["run"]["state"] == "queued"
            assert len(snapshot.json()["tasks"]) == 2
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
