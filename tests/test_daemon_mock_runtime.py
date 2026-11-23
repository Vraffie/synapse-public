import asyncio
from contextlib import closing
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest

from synapse.daemon.mock_runtime import MockRuntime
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.workflow_store import create_run, publish_workflow

ROOT = Path(__file__).resolve().parents[1]


def setup_run(db):
    publish_workflow(db, workflow_id="wf", version="1", nodes=[{"id": "build", "dependencies": []}, {"id": "review", "dependencies": ["build"]}], principal="operator", idempotency_key="publish")
    return create_run(db, workflow_id="wf", version="1", principal="operator", idempotency_key="create").response["run_id"]


def test_start_replay_and_exact_fixture_verification(tmp_path):
    with closing(open_database(tmp_path / "db")) as db:
        apply_migrations(db, ROOT / "migrations")
        run_id = setup_run(db)
        blobs = tmp_path / "blobs"
        blobs.mkdir(mode=0o700)
        runtime = MockRuntime(db, blobs)
        async def execute():
            first = runtime.start(run_id, "start")
            second = runtime.start(run_id, "start")
            assert not first.replayed and second.replayed
            await runtime._active
        asyncio.run(execute())
        assert db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone() == ("succeeded",)
        assert db.execute("SELECT COUNT(*) FROM verification_results WHERE outcome='approved'").fetchone() == (2,)
        assert db.execute("SELECT fixture FROM mock_run_config").fetchone() == ("fixed-v1",)


def test_startup_marks_interrupted_mock_unknown_and_never_replays(tmp_path):
    with closing(open_database(tmp_path / "db")) as db:
        apply_migrations(db, ROOT / "migrations")
        run_id = setup_run(db)
        blobs = tmp_path / "blobs"
        blobs.mkdir(mode=0o700)
        runtime = MockRuntime(db, blobs)
        async def interrupt_before_worker_starts():
            runtime.start(run_id, "start")
            runtime._active.cancel()
            await asyncio.gather(runtime._active, return_exceptions=True)
        asyncio.run(interrupt_before_worker_starts())
        runtime.recover_interrupted()
        assert db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone() == ("recovering",)
        async def replay():
            assert runtime.start(run_id, "start").replayed
        asyncio.run(replay())
        assert db.execute("SELECT COUNT(*) FROM task_attempts").fetchone() == (0,)


def test_operator_starts_mock_run_on_real_daemon(tmp_path):
    proc = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(tmp_path), "--migrations", str(ROOT / "migrations")], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "agentd.sock").exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError("daemon exited")
            time.sleep(0.02)
        token = (tmp_path / "operator.token").read_text()
        headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "publish", "X-Correlation-ID": "trace"}
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / "agentd.sock")), base_url="http://localhost") as client:
            assert client.post("/api/v1/workflows", headers=headers, json={"workflow_id":"wf", "version":"1", "nodes":[{"id":"build", "dependencies":[]}]}).status_code == 201
            headers["Idempotency-Key"] = "create"
            response = client.post("/api/v1/runs", headers=headers, json={"workflow_id":"wf", "workflow_version":"1"})
            run_id = response.json()["run_id"]
            headers["Idempotency-Key"] = "start"
            assert client.post(f"/api/v1/runs/{run_id}/mock-start", headers=headers, content=b"unexpected").status_code == 400
            assert client.post(f"/api/v1/runs/{run_id}/mock-start", headers=headers).status_code == 202
            assert client.post(f"/api/v1/runs/{run_id}/mock-start", headers=headers).headers["idempotency-replayed"] == "true"
            while time.monotonic() < deadline:
                state = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["run"]["state"]
                if state == "succeeded":
                    break
                time.sleep(0.02)
            assert state == "succeeded"
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)


def test_recovery_blocks_running_attempt_and_verification(tmp_path):
    with closing(open_database(tmp_path / "db")) as db:
        apply_migrations(db, ROOT / "migrations")
        run_id = setup_run(db)
        blobs = tmp_path / "blobs"
        blobs.mkdir(mode=0o700)
        runtime = MockRuntime(db, blobs)
        async def interrupt_before_worker_starts():
            runtime.start(run_id, "start")
            runtime._active.cancel()
            await asyncio.gather(runtime._active, return_exceptions=True)
        asyncio.run(interrupt_before_worker_starts())
        tasks = db.execute("SELECT id FROM tasks WHERE run_id=? ORDER BY node_id", (run_id,)).fetchall()
        first, second = tasks[0][0], tasks[1][0]
        db.execute("UPDATE tasks SET state='running',state_version=1,attempt_count=1,assigned_profile_id='synapse.builtin.mock.v1' WHERE id=?", (first,))
        db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('attempt',?,1,'synapse.builtin.mock.v1','running')", (first,))
        db.execute("UPDATE tasks SET state='verifying',state_version=1,assigned_profile_id='synapse.builtin.mock.v1' WHERE id=?", (second,))
        runtime.recover_interrupted()
        assert db.execute("SELECT state FROM task_attempts WHERE id='attempt'").fetchone() == ("unknown",)
        assert db.execute("SELECT state FROM tasks WHERE id IN (?,?) ORDER BY id", (first, second)).fetchall() == [("blocked",), ("blocked",)]
        assert db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone() == ("recovering",)
        before = db.execute("SELECT COUNT(*) FROM events").fetchone()
        runtime.recover_interrupted()
        assert db.execute("SELECT COUNT(*) FROM events").fetchone() == before


def test_sigkill_active_mock_restarts_in_recovering_state(tmp_path):
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
run(Path(sys.argv[1]), Path(sys.argv[2]))
'''
    command = [sys.executable, "-c", script, str(tmp_path), str(ROOT / "migrations")]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    second = None
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "agentd.sock").exists() and time.monotonic() < deadline:
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
        # The restarted daemon reclaims the stale socket left by the SIGKILL.
        second = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(tmp_path), "--migrations", str(ROOT / "migrations")], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while True:
            if second.poll() is not None:
                raise AssertionError("daemon restart failed")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(tmp_path / "agentd.sock"))
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("restarted daemon never listened")
                time.sleep(0.02)
            finally:
                probe.close()
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


def test_config_upgrade_preserves_existing_run_and_guards_replace(tmp_path):
    import sqlite3
    directory = tmp_path / "migrations"
    directory.mkdir()
    for source in sorted((ROOT / "migrations").glob("000[1-8]_*.sql")):
        (directory / source.name).write_bytes(source.read_bytes())
    with closing(open_database(tmp_path / "db")) as db:
        assert apply_migrations(db, directory) == list(range(1, 9))
        run_id = setup_run(db)
        source = ROOT / "migrations" / "0009_mock_run_config.sql"
        (directory / source.name).write_bytes(source.read_bytes())
        assert apply_migrations(db, directory) == [9]
        assert db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone() == ("queued",)
        db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) VALUES('p','mock','Mock','mock','now')")
        db.execute("INSERT INTO mock_run_config VALUES(?,'p','fixed-v1')", (run_id,))
        for recursive in (0, 1):
            db.execute(f"PRAGMA recursive_triggers={recursive}")
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute("INSERT OR REPLACE INTO mock_run_config VALUES(?,'p','fixed-v1')", (run_id,))


def test_active_engine_prevents_new_run_after_run_state_changes(tmp_path):
    from synapse.domain.contracts import RunId
    from synapse.domain.runs import RunChange, RunState
    from synapse.persistence.run_store import write_run_transition
    from synapse.persistence.state_store import StateConflict

    with closing(open_database(tmp_path / "db")) as db:
        apply_migrations(db, ROOT / "migrations")
        first = setup_run(db)
        second = create_run(db, workflow_id="wf", version="1", principal="operator", idempotency_key="second").response["run_id"]
        blobs = tmp_path / "blobs"
        blobs.mkdir(mode=0o700)
        runtime = MockRuntime(db, blobs)
        async def exercise():
            runtime.start(first, "first-start")
            db.execute("BEGIN IMMEDIATE")
            write_run_transition(db, RunChange(RunId(first), RunState.RUNNING, RunState.PAUSED, 2), principal="operator")
            db.execute("COMMIT")
            with pytest.raises(StateConflict, match="Another mock run is active"):
                runtime.start(second, "second-start")
            assert runtime.start(first, "first-start").replayed
            assert db.execute("SELECT state FROM runs WHERE id=?", (second,)).fetchone() == ("queued",)
            runtime._active.cancel()
            await asyncio.gather(runtime._active, return_exceptions=True)
        asyncio.run(exercise())
