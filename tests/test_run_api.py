"""Run queries use authenticated transport and daemon-owned snapshots."""
from pathlib import Path
import signal
import subprocess
import sys
import time

import httpx

from synapse.infrastructure.daemon_database import daemon_database

ROOT = Path(__file__).resolve().parents[1]


def test_run_queries_over_real_daemon(tmp_path):
    with daemon_database(tmp_path / 'synapse.sqlite', ROOT / 'migrations') as db:
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        db.execute("INSERT INTO runs VALUES('other','wf','1','created',0,'now','now')")
        db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES('task','run','node','created','now','now')")
        db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES('foreign','other','node','created','now','now')")
        db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,state) VALUES('attempt','task',1,'unknown')")
        db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,state) VALUES('foreign-attempt','foreign',1,'unknown')")
        db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,type,schema_version,payload_json,occurred_at) VALUES('event','system','id','created',1,'{}','now')")
    proc = subprocess.Popen([sys.executable, '-m', 'synapse.daemon', '--state-dir', str(tmp_path),
                             '--migrations', str(ROOT / 'migrations')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / 'agentd.sock').exists():
            if proc.poll() is not None:
                raise AssertionError(proc.stderr.read().decode())
            if time.monotonic() > deadline:
                raise AssertionError('daemon did not start')
            time.sleep(0.02)
        token = (tmp_path / 'operator.token').read_text()
        while True:
            try:
                with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / 'agentd.sock')), base_url='http://localhost') as probe:
                    if probe.get('/api/v1/system/health', headers={'Authorization': 'Bearer ' + token}).status_code == 200:
                        break
            except httpx.ConnectError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError('daemon did not become ready')
            time.sleep(0.02)
        headers = {'Authorization': 'Bearer ' + token}
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / 'agentd.sock')), base_url='http://localhost') as client:
            assert client.get('/api/v1/runs/run').status_code == 401
            assert client.get('/api/v1/runs/run/tasks').status_code == 401
            snapshot = client.get('/api/v1/runs/run', headers=headers)
            assert snapshot.status_code == 200
            assert snapshot.json()['run']['id'] == 'run'
            assert snapshot.json()['last_sequence'] == 1
            tasks = client.get('/api/v1/runs/run/tasks', headers=headers)
            assert tasks.status_code == 200
            assert [task['id'] for task in tasks.json()['tasks']] == ['task']
            assert [a['id'] for a in tasks.json()['tasks'][0]['attempts']] == ['attempt']
            assert client.get('/api/v1/runs/missing', headers=headers).status_code == 404
            assert client.get('/api/v1/runs/missing/tasks', headers=headers).status_code == 404
            assert client.get('/api/v1/runs/' + 'x' * 257, headers=headers).status_code == 422
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
    assert proc.returncode == 0
