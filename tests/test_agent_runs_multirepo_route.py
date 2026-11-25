"""POST /api/v1/agent-runs accepts the multi-repository shape (ADR 026) and passes it to the service unchanged."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from synapse.api.agent_commands import agent_command_router
from synapse.persistence.idempotency import CommandResult

HEADERS = {"Authorization": "Bearer x", "Idempotency-Key": "k1", "X-Correlation-ID": "c1"}
TASK = {"id": "t1", "prompt": "p", "allowed_tools": ["Read"], "repos": [{"repo_id": "a", "access": "write"}, {"repo_id": "b", "access": "read"}]}
MULTI = {"repos": [{"repo_id": "a", "baseline_ref": "refs/heads/main"}, {"repo_id": "b"}], "tasks": [TASK]}


def _client():
    seen = []

    def start(key, data):
        seen.append(data)
        return CommandResult({"run_id": "r", "task_id": "t", "state": "running"}, False)
    app = FastAPI()
    app.include_router(agent_command_router(start, None, None))
    return TestClient(app), seen


def test_multirepo_request_reaches_the_service_without_null_fields():
    client, seen = _client()
    assert client.post("/api/v1/agent-runs", json=MULTI, headers=HEADERS).status_code == 201
    data = seen[0]
    assert "repo" not in data and "base_ref" not in data
    assert data["repos"] == [{"repo_id": "a", "baseline_ref": "refs/heads/main"}, {"repo_id": "b"}]
    assert data["tasks"][0]["repos"] == TASK["repos"]


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(repo="/x", base_ref="main"),                      # both shapes
    lambda b: b.pop("tasks"),                                            # multi-repo needs tasks
    lambda b: b["tasks"][0].pop("repos"),                                # each task declares repos
    lambda b: b["tasks"][0]["repos"][0].update(access="rw"),             # access is write or read
    lambda b: b["repos"][0].update(repo_id="bad id"),                    # id pattern
    lambda b: b["repos"][0].update(extra=1),                             # unknown key
    lambda b: b.update(prompt="x"),                                      # tasks exclude prompt
])
def test_malformed_multirepo_requests_are_refused_before_the_service(mutate):
    import copy
    client, seen = _client()
    body = copy.deepcopy(MULTI)
    mutate(body)
    assert client.post("/api/v1/agent-runs", json=body, headers=HEADERS).status_code == 422
    assert seen == []


def test_single_repo_requests_still_work_and_cannot_carry_task_repos():
    client, seen = _client()
    single = {"repo": "/r", "base_ref": "main", "prompt": "p", "allowed_tools": ["Read"]}
    assert client.post("/api/v1/agent-runs", json=single, headers=HEADERS).status_code == 201
    assert "repos" not in seen[0]
    tasked = {"repo": "/r", "base_ref": "main", "tasks": [TASK]}
    assert client.post("/api/v1/agent-runs", json=tasked, headers={**HEADERS, "Idempotency-Key": "k2"}).status_code == 422
    assert client.post("/api/v1/agent-runs", json={"base_ref": "main", "prompt": "p", "allowed_tools": ["Read"]}, headers=HEADERS).status_code == 422
