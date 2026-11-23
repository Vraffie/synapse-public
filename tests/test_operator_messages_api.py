from contextlib import closing
from pathlib import Path
import signal
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from synapse.api.app import create_app
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.operator_messages import read_messages, read_threads

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "a" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "synapse.sqlite"
    with closing(open_database(path)) as db:
        apply_migrations(db, ROOT / "migrations")
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        db.execute("INSERT INTO runs VALUES('other','wf','1','created',0,'now','now')")
        db.execute("INSERT INTO agent_definitions VALUES('agent','1',NULL,'{}')")
        db.execute("INSERT INTO agent_instances(id,run_id,definition_id,definition_version,state,created_at) VALUES('agent','run','agent','1','running','now')")
        db.execute("INSERT INTO threads VALUES('thread','run','subject','open','now')")
        db.execute("INSERT INTO threads VALUES('foreign','other','secret','open','now')")
        for mid in ("one", "two"):
            db.execute("INSERT INTO messages(id,thread_id,sender_instance_id,type,schema_version,payload_json,created_at) VALUES(?,'thread','agent','question',1,'{\"text\":\"hello\"}','now')", (mid,))
    path.chmod(0o600)
    return path


def reader(path, fn, *args):
    with closing(open_database(path)) as db:
        return fn(db, *args)


def test_run_scoped_paginated_queries_and_auth(database):
    app = create_app(token=TOKEN, read_events=lambda *_: [],
                     read_threads=lambda *a: reader(database, read_threads, *a),
                     read_messages=lambda *a: reader(database, read_messages, *a))
    with TestClient(app, client=None) as client:
        path = "/api/v1/runs/run/threads"
        assert client.get(path).status_code == 401
        assert client.get(path, headers={**HEADERS, "Origin": "http://localhost"}).status_code == 401
        response = client.get(path, headers=HEADERS)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert [t["id"] for t in response.json()["threads"]] == ["thread"]
        assert client.get("/api/v1/runs/run/threads/foreign/messages", headers=HEADERS).status_code == 404
        assert client.get(path + "?limit=101", headers=HEADERS).status_code == 422
        assert client.get(path + "?after=-1", headers=HEADERS).status_code == 422
        messages = path + "/thread/messages"
        first = client.get(messages + "?limit=1", headers=HEADERS).json()["messages"]
        assert first[0]["id"] == "one" and first[0]["payload"] == {"text": "hello"}
        second = client.get(messages + "?after=" + str(first[0]["cursor"]), headers=HEADERS).json()["messages"]
        assert [m["id"] for m in second] == ["two"]
        assert client.post(messages, headers=HEADERS, json={}).status_code == 405


def test_query_works_over_real_daemon_socket(database):
    state = database.parent
    process = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(state),
                                "--migrations", str(ROOT / "migrations")],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not (state / "agentd.sock").exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("daemon exited before listening")
            time.sleep(0.02)
        token = (state / "operator.token").read_text()
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(state / "agentd.sock")), base_url="http://localhost") as client:
            response = client.get("/api/v1/runs/run/threads/thread/messages", headers={"Authorization": "Bearer " + token})
            assert response.status_code == 200
            assert [m["id"] for m in response.json()["messages"]] == ["one", "two"]
    finally:
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=8)
