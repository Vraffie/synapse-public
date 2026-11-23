from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from synapse.api.app import create_app
from synapse.persistence.approval_store import write_approval_request
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.run_list import read_run_list

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "r" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "runs.db"
    with closing(open_database(path)) as db:
        apply_migrations(db, ROOT / "migrations")
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
    return path


def add_runs(path, count, *, state="queued", prefix="r", same_time=False):
    with closing(open_database(path)) as db:
        for i in range(count):
            stamp = "2026-01-01T00:00:00+00:00" if same_time else f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00"
            db.execute("INSERT INTO runs VALUES(?,?,?,?,0,?,?)", (f"{prefix}{i:04d}", "wf", "1", state, stamp, stamp))
        db.commit()


@pytest.fixture
def client(path):
    def reader(state, limit, cursor):
        with closing(open_database(path)) as db:
            return read_run_list(db, state=state, limit=limit, cursor=cursor)
    app = create_app(token=TOKEN, read_events=lambda *_: [], read_runs=reader)
    with TestClient(app, client=None) as client:
        yield client


def walk(client, query=""):
    ids, cursor = [], None
    while True:
        url = "/api/v1/runs?limit=7" + query + (f"&cursor={cursor}" if cursor else "")
        body = client.get(url, headers=HEADERS).json()
        ids += [run["id"] for run in body["runs"]]
        cursor = body["next_cursor"]
        if cursor is None:
            return ids


def test_empty_database(client):
    response = client.get("/api/v1/runs", headers=HEADERS)
    assert response.status_code == 200 and response.json() == {"runs": [], "next_cursor": None}
    assert response.headers["cache-control"] == "no-store"


def test_auth_required(client):
    assert client.get("/api/v1/runs").status_code == 401
    assert client.get("/api/v1/runs", headers={"Authorization": "Bearer " + "x" * 43}).status_code == 401


def test_unavailable_without_reader():
    app = create_app(token=TOKEN, read_events=lambda *_: [])
    with TestClient(app, client=None) as client:
        assert client.get("/api/v1/runs", headers=HEADERS).status_code == 503


def test_newest_first_pagination_with_100_plus_runs(path, client):
    add_runs(path, 130)
    expected = [f"r{i:04d}" for i in reversed(range(130))]
    assert walk(client) == expected
    default = client.get("/api/v1/runs", headers=HEADERS).json()
    assert len(default["runs"]) == 50 and default["next_cursor"]
    assert len(client.get("/api/v1/runs?limit=200", headers=HEADERS).json()["runs"]) == 130
    assert client.get("/api/v1/runs?limit=130", headers=HEADERS).json()["next_cursor"] is None


def test_equal_timestamps_are_ordered_by_id_and_stable_under_inserts(path, client):
    add_runs(path, 30, same_time=True)
    first = client.get("/api/v1/runs?limit=10", headers=HEADERS).json()
    add_runs(path, 5, prefix="z", same_time=False)  # older timestamps and ids sorting after: no effect on page 2
    rest, cursor = [], first["next_cursor"]
    while cursor:
        body = client.get(f"/api/v1/runs?limit=10&cursor={cursor}", headers=HEADERS).json()
        rest += [run["id"] for run in body["runs"]]
        cursor = body["next_cursor"]
    ids = [run["id"] for run in first["runs"]] + rest
    assert ids[:30] == [f"r{i:04d}" for i in reversed(range(30))]
    assert len(ids) == len(set(ids))


def test_state_filter_and_summary_fields(path, client):
    add_runs(path, 3, state="running", prefix="a")
    add_runs(path, 2, state="failed", prefix="b")
    assert walk(client, "&state=failed") == ["b0001", "b0000"]
    assert len(walk(client, "&state=running")) == 3
    assert walk(client, "&state=paused") == []
    with closing(open_database(path)) as db:
        for task, state in (("t1", "ready"), ("t2", "ready"), ("t3", "succeeded")):
            db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES(?,?,?,?,'now','now')",
                       (task, "a0001", task, state))
        db.execute("BEGIN IMMEDIATE")
        for name, decided in (("p1", False), ("p2", False)):
            write_approval_request(db, approval_id=name, run_id="a0001", operation="push", target="repo",
                                   candidate_sha256="a" * 64, policy_version="1",
                                   expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        db.execute("COMMIT")
    runs = {r["id"]: r for r in client.get("/api/v1/runs?limit=200", headers=HEADERS).json()["runs"]}
    assert set(runs["a0001"]) == {"id", "workflow_id", "workflow_version", "state", "state_version",
                                  "created_at", "updated_at", "task_counts", "pending_approvals"}
    assert runs["a0001"]["task_counts"] == {"ready": 2, "succeeded": 1}
    assert runs["a0001"]["pending_approvals"] == 2
    assert runs["a0000"]["task_counts"] == {} and runs["a0000"]["pending_approvals"] == 0
    assert "candidate" not in client.get("/api/v1/runs", headers=HEADERS).text


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=abc", "limit=-1", "state=bogus",
                                   "state=", "cursor=", "cursor=not-a-cursor", "cursor=" + "A" * 1025,
                                   "cursor=W1widGhpcyJd", "cursor=e30"])
def test_bad_parameters_are_rejected_without_echo(client, query):
    response = client.get("/api/v1/runs?" + query, headers=HEADERS)
    assert response.status_code == 422 and response.json()["code"] == "validation_error"
    assert "bogus" not in response.text and "not-a-cursor" not in response.text
