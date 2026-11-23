from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from synapse.api.app import create_app
from synapse.persistence.approval_query import read_approvals
from synapse.persistence.approval_store import write_approval_request
from synapse.persistence.migrations import apply_migrations, open_database

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "b" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "approvals.db"
    with closing(open_database(path)) as db:
        apply_migrations(db, ROOT / "migrations")
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        for name in ("b", "a"):
            db.execute("BEGIN IMMEDIATE")
            write_approval_request(db, approval_id=name, run_id="run", operation="push", target="repo",
                                   candidate_sha256="a" * 64, policy_version="policy-1",
                                   expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
            db.execute("COMMIT")
    return path


def test_query_is_authenticated_paginated_and_read_only(path):
    def reader(after, limit):
        with closing(open_database(path)) as db:
            return read_approvals(db, after=after, limit=limit)
    app = create_app(token=TOKEN, read_events=lambda *_: [], read_approvals=reader)
    with TestClient(app, client=None) as client:
        assert client.get("/api/v1/approvals").status_code == 401
        assert client.get("/api/v1/approvals", headers={**HEADERS, "Origin": "http://localhost"}).status_code == 401
        first = client.get("/api/v1/approvals?limit=1", headers=HEADERS)
        assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
        row, = first.json()["approvals"]
        assert (row["id"], row["state"], row["state_version"]) == ("a", "requested", 0)
        assert row["candidate_manifest_sha256"] == "a" * 64
        assert row["subject"] is None  # not an agent-run approval
        assert client.get("/api/v1/approvals?after=a", headers=HEADERS).json()["approvals"][0]["id"] == "b"
        assert client.get("/api/v1/approvals?limit=1001", headers=HEADERS).status_code == 422
        assert client.post("/api/v1/approvals/a/decision", headers=HEADERS, json={}).status_code in (400, 401, 503)  # no decision backend is wired here
    with closing(open_database(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM approval_requests WHERE state='requested'").fetchone() == (2,)
        assert db.execute("SELECT COUNT(*) FROM events").fetchone() == (2,)


def test_query_unavailable_without_reader():
    app = create_app(token=TOKEN, read_events=lambda *_: [])
    with TestClient(app, client=None) as client:
        assert client.get("/api/v1/approvals", headers=HEADERS).status_code == 503


def test_query_through_real_daemon(path):
    import signal
    import subprocess
    import sys
    import time
    import httpx

    state = path.parent
    path.rename(state / "synapse.sqlite")
    (state / "synapse.sqlite").chmod(0o600)
    proc = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(state),
                             "--migrations", str(ROOT / "migrations")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not (state / "agentd.sock").exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError("daemon exited before listening")
            time.sleep(0.02)
        token = (state / "operator.token").read_text()
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(state / "agentd.sock")), base_url="http://localhost") as client:
            response = client.get("/api/v1/approvals?limit=1", headers={"Authorization": "Bearer " + token})
            assert response.status_code == 200
            assert response.json()["approvals"][0]["id"] == "a"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
