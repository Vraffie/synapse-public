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
        db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES('task2','run','node2','created','now','now')")
        db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,enabled,max_concurrency,created_at) "
                   "VALUES('p','claude','p','c',1,1,'now')")
        for n, attempt in ((1, 'attempt'), (2, 'attempt'), (3, 'foreign-attempt')):
            db.execute("INSERT INTO usage_observations(id,attempt_id,profile_id,source,input_tokens,confidence,observed_at,"
                       "source_event_id) VALUES(?,?,'p','claude-cli',?,'reported',?,?)",
                       ('u%d' % n, attempt, 10 * n, '2026-11-20T00:00:0%d+00:00' % n, 'e%d' % n))
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
            assert [task['id'] for task in tasks.json()['tasks']] == ['task', 'task2']
            assert tasks.json()['next_cursor'] is None
            assert [a['id'] for a in tasks.json()['tasks'][0]['attempts']] == ['attempt']
            # The daemon lambdas pass limit and cursor through to the persistence readers.
            first = client.get('/api/v1/runs/run/tasks', params={'limit': 1}, headers=headers).json()
            assert [t['id'] for t in first['tasks']] == ['task'] and first['next_cursor']
            second = client.get('/api/v1/runs/run/tasks', params={'limit': 1, 'cursor': first['next_cursor']},
                                headers=headers).json()
            assert [t['id'] for t in second['tasks']] == ['task2'] and second['next_cursor'] is None
            assert client.get('/api/v1/runs/other/tasks', params={'cursor': first['next_cursor']},
                              headers=headers).status_code == 422
            assert client.get('/api/v1/runs/run/tasks', params={'limit': 501}, headers=headers).status_code == 422
            usage = client.get('/api/v1/runs/run/usage', params={'limit': 1}, headers=headers).json()
            assert [o['id'] for o in usage['observations']] == ['u1'] and usage['totals']['input_tokens'] == 30
            usage2 = client.get('/api/v1/runs/run/usage', params={'limit': 1, 'cursor': usage['next_cursor']},
                                headers=headers).json()
            assert [o['id'] for o in usage2['observations']] == ['u2'] and usage2['next_cursor'] is None
            assert usage2['totals'] == usage['totals'] and usage2['coverage'] == usage['coverage']
            assert client.get('/api/v1/runs/run/usage', params={'cursor': first['next_cursor']},
                              headers=headers).status_code == 422
            assert client.get('/api/v1/runs/missing/usage', headers=headers).status_code == 404
            assert client.get('/api/v1/runs/missing', headers=headers).status_code == 404
            assert client.get('/api/v1/runs/missing/tasks', headers=headers).status_code == 404
            assert client.get('/api/v1/runs/' + 'x' * 257, headers=headers).status_code == 422
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
    assert proc.returncode == 0
