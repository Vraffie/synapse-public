from fastapi.testclient import TestClient
import pytest

from synapse.api.app import create_app
from synapse.persistence.state_store import StateConflict

TOKEN = "t" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "k-1", "X-Correlation-ID": "trace-1"}
WORKFLOW = {"workflow_id": "wf", "version": "1", "nodes": [{"id": "build", "dependencies": []}]}


def _conflict(*_):
    raise StateConflict("secret internal row wf@1 version mismatch")


@pytest.mark.parametrize("path, body", [
    ("/api/v1/workflows", WORKFLOW),
    ("/api/v1/runs", {"workflow_id": "wf", "workflow_version": "1"}),
])
def test_state_conflict_maps_to_409_without_internals(path, body):
    app = create_app(token=TOKEN, read_events=lambda *_: [], publish_workflow=_conflict, create_run=_conflict)
    with TestClient(app, client=None, raise_server_exceptions=False) as client:
        response = client.post(path, headers=HEADERS, json=body)
    assert response.status_code == 409
    assert response.headers["x-correlation-id"] == "trace-1"
    assert response.json()["code"] == "conflict"
    assert "secret" not in response.text and "version mismatch" not in response.text


def test_409_documented_for_both_routes():
    app = create_app(token=TOKEN, read_events=lambda *_: [])
    schema = app.openapi()
    for path in ("/api/v1/workflows", "/api/v1/runs"):
        assert "409" in schema["paths"][path]["post"]["responses"]
