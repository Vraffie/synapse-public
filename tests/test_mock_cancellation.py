import asyncio
from contextlib import closing
from pathlib import Path
import signal
import subprocess
import sys
import time

import httpx
import pytest

from synapse.daemon.mock_runtime import MockRuntime
from synapse.persistence.idempotency import IdempotencyConflict
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.mock_cancellation import finalize_mock_cancellation
from synapse.persistence.state_store import StateConflict
from test_daemon_mock_runtime import setup_run

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepared(tmp_path):
    with closing(open_database(tmp_path / 'db')) as db:
        apply_migrations(db, ROOT / 'migrations')
        run_id = setup_run(db)
        blobs = tmp_path / 'blobs'
        blobs.mkdir(mode=0o700)
        yield db, run_id, MockRuntime(db, blobs)


def test_cancel_before_start_is_durable_and_replayed(prepared):
    db, run_id, runtime = prepared
    async def run():
        runtime.start(run_id, 'start')
        first = runtime.cancel(run_id, 'cancel')
        assert not first.replayed and runtime.cancel(run_id, 'cancel').replayed
        with pytest.raises(StateConflict):
            runtime.cancel(run_id, 'different-key')
        await runtime._cancelling
        assert runtime.cancel(run_id, 'cancel').replayed
        await runtime._cancelling
    asyncio.run(run())
    assert db.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone() == ('cancelled',)
    assert set(db.execute('SELECT state FROM tasks WHERE run_id=?', (run_id,)).fetchall()) == {('cancelled',)}
    assert db.execute("SELECT COUNT(*) FROM events WHERE type='mock.cancellation_requested'").fetchone() == (1,)
    runtime.recover_interrupted()
    assert db.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone() == ('cancelled',)


def test_active_worker_stops_before_finalization(prepared, monkeypatch):
    from synapse.providers.mock import MockProvider
    db, run_id, runtime = prepared
    entered = asyncio.Event()
    async def stream(self, handle):
        entered.set()
        await asyncio.Event().wait()
        yield None
    monkeypatch.setattr(MockProvider, 'stream', stream)
    async def run():
        runtime.start(run_id, 'start')
        await asyncio.wait_for(entered.wait(), 3)
        runtime.cancel(run_id, 'cancel')
        assert db.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone() == ('running',)
        await asyncio.wait_for(runtime._cancelling, 3)
        assert runtime._active.done()
    asyncio.run(run())
    assert db.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone() == ('cancelled',)
    assert db.execute("SELECT COUNT(*) FROM task_attempts WHERE state NOT IN ('completed','failed','cancelled')").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM task_attempts WHERE state='cancelled'").fetchone() == (1,)
    assert db.execute("SELECT COUNT(*) FROM verification_results").fetchone() == (0,)


def test_restart_finishes_intent_only_after_worker_stop(prepared):
    from synapse.persistence.mock_cancellation import request_mock_cancellation
    db, run_id, runtime = prepared
    async def run():
        runtime.start(run_id, 'start')
        request_mock_cancellation(db, run_id=run_id, key='cancel')
        runtime._active.cancel()
        await asyncio.gather(runtime._active, return_exceptions=True)
    asyncio.run(run())
    with pytest.raises(ValueError, match='stopped-worker'):
        finalize_mock_cancellation(db, run_id=run_id, no_workers_survive=False)
    runtime.recover_interrupted()
    assert db.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone() == ('cancelled',)
    runtime.recover_interrupted()
    assert db.execute("SELECT COUNT(*) FROM events WHERE type='mock.cancellation_requested'").fetchone() == (1,)


def test_nonmock_unresolved_attempt_blocks_finalization_atomically(prepared):
    from synapse.persistence.mock_cancellation import request_mock_cancellation
    db, run_id, runtime = prepared
    async def run():
        runtime.start(run_id, 'start')
        request_mock_cancellation(db, run_id=run_id, key='cancel')
        runtime._active.cancel()
        await asyncio.gather(runtime._active, return_exceptions=True)
    asyncio.run(run())
    task, = db.execute('SELECT id FROM tasks WHERE run_id=? LIMIT 1', (run_id,)).fetchone()
    db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) VALUES('foreign','codex','x','ref','now')")
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('foreign-attempt',?,1,'foreign','unknown')", (task,))
    with pytest.raises(StateConflict, match='non-mock'):
        finalize_mock_cancellation(db, run_id=run_id, no_workers_survive=True)
    assert db.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone() == ('running',)
    assert db.execute("SELECT state FROM task_attempts WHERE id='foreign-attempt'").fetchone() == ('unknown',)


def test_cancel_request_outbox_failure_rolls_back(prepared):
    db, run_id, runtime = prepared
    async def run():
        runtime.start(run_id, 'start')
        db.execute("CREATE TRIGGER fail_cancel_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'forced'); END")
        with pytest.raises(Exception, match='forced'):
            runtime.cancel(run_id, 'cancel')
        assert db.execute('SELECT COUNT(*) FROM mock_cancellation_requests').fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM idempotency_keys WHERE operation='mock.cancel'").fetchone() == (0,)
        db.execute('DROP TRIGGER fail_cancel_outbox')
        runtime._active.cancel()
        await asyncio.gather(runtime._active, return_exceptions=True)
    asyncio.run(run())


def test_cancel_via_real_daemon_with_active_synthetic_worker(tmp_path):
    script = '''
import asyncio
import sys
from synapse.providers.mock import MockProvider
from synapse.daemon.__main__ import main
async def hold(self, handle):
    await asyncio.sleep(60)
    yield None
MockProvider.stream = hold
sys.argv = ["agentd", "--state-dir", sys.argv[1], "--migrations", sys.argv[2]]
main()
'''
    tmp_path.chmod(0o700)
    proc = subprocess.Popen([sys.executable, '-c', script, str(tmp_path), str(ROOT / 'migrations')],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / 'agentd.sock').exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError('daemon exited')
            time.sleep(0.02)
        token = (tmp_path / 'operator.token').read_text()
        headers = {'Authorization': 'Bearer ' + token, 'Idempotency-Key': 'publish', 'X-Correlation-ID': 'trace'}
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / 'agentd.sock')), base_url='http://localhost') as client:
            while time.monotonic() < deadline:
                try:
                    if client.get('/api/v1/system/health', headers=headers).status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(0.02)
            assert client.post('/api/v1/workflows', headers=headers, json={'workflow_id':'wf', 'version':'1', 'nodes':[{'id':'task', 'dependencies':[]}]}).status_code == 201
            headers['Idempotency-Key'] = 'create'
            run_id = client.post('/api/v1/runs', headers=headers, json={'workflow_id':'wf', 'workflow_version':'1'}).json()['run_id']
            headers['Idempotency-Key'] = 'start'
            assert client.post(f'/api/v1/runs/{run_id}/mock-start', headers=headers).status_code == 202
            while time.monotonic() < deadline:
                snapshot = client.get(f'/api/v1/runs/{run_id}', headers=headers).json()
                if snapshot['tasks'][0]['state'] == 'running':
                    break
                time.sleep(0.02)
            assert snapshot['tasks'][0]['state'] == 'running'
            headers['Idempotency-Key'] = 'cancel'
            assert client.post(f'/api/v1/runs/{run_id}/mock-cancel').status_code == 401
            assert client.post(f'/api/v1/runs/{run_id}/mock-cancel', headers=headers, content=b'x').status_code == 400
            response = client.post(f'/api/v1/runs/{run_id}/mock-cancel', headers=headers)
            assert response.status_code == 202 and response.json()['state'] == 'cancellation_requested'
            assert client.post(f'/api/v1/runs/{run_id}/mock-cancel', headers=headers).headers['idempotency-replayed'] == 'true'
            while time.monotonic() < deadline:
                snapshot = client.get(f'/api/v1/runs/{run_id}', headers=headers).json()
                if snapshot['run']['state'] == 'cancelled':
                    break
                time.sleep(0.02)
            assert snapshot['run']['state'] == 'cancelled'
            assert snapshot['tasks'][0]['state'] == 'cancelled'
            tasks = client.get(f'/api/v1/runs/{run_id}/tasks', headers=headers).json()['tasks']
            assert tasks[0]['attempts'][0]['state'] == 'cancelled'
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
    assert proc.returncode == 0


def test_mock_cancellation_migration_upgrades_existing_run(tmp_path):
    directory = tmp_path / 'old-migrations'
    directory.mkdir()
    for path in sorted((ROOT / 'migrations').glob('*.sql')):
        if int(path.name[:4]) < 11:  # every later migration is applied afterwards or not at all
            (directory / path.name).write_bytes(path.read_bytes())
    with closing(open_database(tmp_path / 'upgrade.db')) as db:
        apply_migrations(db, directory)
        run_id = setup_run(db)
        source = ROOT / 'migrations' / '0011_mock_cancellation.sql'
        (directory / source.name).write_bytes(source.read_bytes())
        assert apply_migrations(db, directory) == [11]
        assert db.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone() == ('queued',)
        assert db.execute('SELECT COUNT(*) FROM mock_cancellation_requests').fetchone() == (0,)


def test_cancel_retry_with_same_key_after_failed_finalization(prepared, monkeypatch):
    import synapse.daemon.mock_runtime as module
    db, run_id, runtime = prepared
    real = module.finalize_mock_cancellation
    calls = []
    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('transient')
        return real(*args, **kwargs)
    monkeypatch.setattr(module, 'finalize_mock_cancellation', flaky)
    async def run():
        runtime.start(run_id, 'start')
        runtime.cancel(run_id, 'cancel')
        await runtime._cancelling
        assert db.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone() == ('running',)
        assert runtime.cancel(run_id, 'cancel').replayed
        await runtime._cancelling
    asyncio.run(run())
    assert db.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone() == ('cancelled',)


def test_startup_recovery_continues_after_one_run_fails(prepared, monkeypatch, caplog):
    db, run_id, runtime = prepared
    async def started():
        runtime.start(run_id, 'start')
        runtime._active.cancel()
        await asyncio.gather(runtime._active, return_exceptions=True)
    asyncio.run(started())
    seen = []
    def boom(run):
        seen.append(run)
        raise StateConflict('inconsistent')
    monkeypatch.setattr(runtime, '_recover_run', boom)
    runtime.recover_interrupted()  # must not raise
    assert seen == [run_id]
    assert 'Mock startup recovery failed' in caplog.text
