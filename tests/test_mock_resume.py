from contextlib import closing
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest

from synapse.infrastructure.daemon_database import daemon_database
from synapse.persistence.mock_resume import ASSERTION, resume_fixed_mock
from synapse.persistence.state_store import StateConflict

ROOT = Path(__file__).resolve().parents[1]
PROFILE = 'synapse.builtin.mock.v1'


def seed(db):
    db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
    db.execute("INSERT INTO runs VALUES('run','wf','1','recovering',4,'now','now')")
    db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) VALUES(?,'mock','Mock','mock','now')", (PROFILE,))
    db.execute("INSERT INTO mock_run_config VALUES('run',?,'fixed-v1')", (PROFILE,))
    db.execute("INSERT INTO tasks(id,run_id,node_id,state,state_version,assigned_profile_id,attempt_count,created_at,updated_at) VALUES('task','run','node','blocked',3,?,1,'now','now')", (PROFILE,))
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('attempt','task',1,?,'unknown')", (PROFILE,))


def params():
    return dict(run_id='run', expected_run_version=4, tasks=[{'task_id':'task','expected_version':3,'unknown_attempt_id':'attempt'}], assertion=ASSERTION, key='resume', correlation_id='trace', no_workers_survive=True)


def test_explicit_reconciliation_atomic_idempotent_and_audited(tmp_path):
    with daemon_database(tmp_path / 'db', ROOT / 'migrations') as db:
        seed(db)
        assert not resume_fixed_mock(db, **params()).replayed
        again = params()
        again['no_workers_survive'] = False
        assert resume_fixed_mock(db, **again).replayed
        assert db.execute('SELECT state,state_version FROM runs').fetchone() == ('running',5)
        assert db.execute('SELECT state,state_version FROM tasks').fetchone() == ('ready',4)
        assert db.execute('SELECT state FROM task_attempts').fetchone() == ('failed',)
        assert db.execute('SELECT action,correlation_id FROM audit_entries').fetchone() == ('mock.resume','trace')
        assert db.execute('SELECT COUNT(*) FROM events').fetchone() == db.execute('SELECT COUNT(*) FROM outbox').fetchone() == (3,)


@pytest.mark.parametrize('mutation', ['not_stopped','wrong_run_version','wrong_task_version','wrong_attempt','missing','exhausted','foreign'])
def test_wrong_evidence_or_unfenced_worker_cannot_mutate(tmp_path, mutation):
    with daemon_database(tmp_path / 'db', ROOT / 'migrations') as db:
        seed(db)
        args = params()
        if mutation == 'not_stopped': args['no_workers_survive'] = False
        elif mutation == 'wrong_run_version': args['expected_run_version'] = 3
        elif mutation == 'wrong_task_version': args['tasks'][0]['expected_version'] = 2
        elif mutation == 'wrong_attempt': args['tasks'][0]['unknown_attempt_id'] = 'other'
        elif mutation == 'missing': args['tasks'] = []
        elif mutation == 'exhausted': db.execute("UPDATE tasks SET attempt_count=2")
        elif mutation == 'foreign': db.execute("UPDATE profiles SET provider='codex' WHERE id=?", (PROFILE,))
        with pytest.raises(StateConflict):
            resume_fixed_mock(db, **args)
        assert db.execute('SELECT state FROM task_attempts').fetchone() == ('unknown',)
        assert db.execute('SELECT state FROM tasks').fetchone() == ('blocked',)
        assert db.execute('SELECT state FROM runs').fetchone() == ('recovering',)
        for table in ('events', 'outbox', 'audit_entries', 'idempotency_keys'):
            assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone() == (0,)


def test_failure_rolls_back_all_reconciliation(tmp_path):
    with daemon_database(tmp_path / 'db', ROOT / 'migrations') as db:
        seed(db)
        db.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON audit_entries BEGIN SELECT RAISE(ABORT,'audit unavailable'); END")
        with pytest.raises(sqlite3.IntegrityError, match='audit unavailable'):
            resume_fixed_mock(db, **params())
        assert db.execute('SELECT state FROM task_attempts').fetchone() == ('unknown',)
        assert db.execute('SELECT state FROM tasks').fetchone() == ('blocked',)
        assert db.execute('SELECT COUNT(*) FROM events').fetchone() == (0,)


def test_real_daemon_crash_requires_explicit_evidence_before_resume(tmp_path):
    harness = '''
import asyncio,sys
from pathlib import Path
from synapse.providers.mock import MockProvider
from synapse.providers.contracts import ProviderEvent
from synapse.daemon.__main__ import run
async def blocked(self, handle):
    yield ProviderEvent('started')
    await asyncio.Event().wait()
MockProvider.stream = blocked
run(Path(sys.argv[1]), Path(sys.argv[2]))
'''
    proc = subprocess.Popen([sys.executable,'-c',harness,str(tmp_path),str(ROOT/'migrations')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    restarted = None
    def ready(child):
        deadline = time.monotonic()+10
        while not (tmp_path/'agentd.sock').exists() and time.monotonic()<deadline:
            if child.poll() is not None: raise AssertionError('daemon exited')
            time.sleep(.02)
        assert (tmp_path/'agentd.sock').exists()
    try:
        ready(proc)
        token = (tmp_path/'operator.token').read_text()
        headers = {'Authorization':'Bearer '+token,'Idempotency-Key':'publish','X-Correlation-ID':'trace'}
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path/'agentd.sock')),base_url='http://localhost',timeout=5) as client:
            assert client.post('/api/v1/workflows',headers=headers,json={'workflow_id':'wf','version':'1','nodes':[{'id':'task','dependencies':[]}]}).status_code==201
            headers['Idempotency-Key']='create'
            result=client.post('/api/v1/runs',headers=headers,json={'workflow_id':'wf','workflow_version':'1'}).json()
            run_id=result['run_id']
            headers['Idempotency-Key']='start'
            assert client.post(f'/api/v1/runs/{run_id}/mock-start',headers=headers).status_code==202
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                with closing(sqlite3.connect(f"file:{tmp_path/'synapse.sqlite'}?mode=ro",uri=True)) as db:
                    attempt=db.execute('SELECT id,state FROM task_attempts').fetchone()
                if attempt and attempt[1]=='running': break
                time.sleep(.02)
            assert attempt and attempt[1]=='running'
        proc.kill(); proc.communicate(timeout=5)
        (tmp_path/'agentd.sock').unlink()  # The test owns the killed process and private socket.
        restarted=subprocess.Popen([sys.executable,'-m','synapse.daemon','--state-dir',str(tmp_path),'--migrations',str(ROOT/'migrations')],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        ready(restarted)
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path/'agentd.sock')),base_url='http://localhost',timeout=5) as client:
            snapshot=client.get(f'/api/v1/runs/{run_id}',headers=headers).json()
            assert snapshot['run']['state']=='recovering' and snapshot['tasks'][0]['state']=='blocked'
            evidence={'expected_run_version':snapshot['run']['state_version'], 'tasks':[{'task_id':snapshot['tasks'][0]['id'],'expected_version':snapshot['tasks'][0]['state_version'],'unknown_attempt_id':attempt[0]}], 'assertion':ASSERTION}
            headers['Idempotency-Key']='resume'
            bad=dict(evidence, assertion='trust-me')
            assert client.post(f'/api/v1/runs/{run_id}/mock-resume',headers=headers,json=bad).status_code==422
            response=client.post(f'/api/v1/runs/{run_id}/mock-resume',headers=headers,json=evidence)
            assert response.status_code==202 and response.headers['idempotency-replayed']=='false'
            from jsonschema import Draft202012Validator
            operation=client.get('/openapi.json', headers=headers).json()['paths']['/api/v1/runs/{run_id}/mock-resume']['post']
            Draft202012Validator(operation['responses']['202']['content']['application/json']['schema']).validate(response.json())
            assert {'400', '409', '413', '415', '422', '503'} <= set(operation['responses'])
            replay=client.post(f'/api/v1/runs/{run_id}/mock-resume',headers=headers,json=evidence)
            assert replay.status_code==202 and replay.headers['idempotency-replayed']=='true'
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                snapshot=client.get(f'/api/v1/runs/{run_id}',headers=headers).json()
                if snapshot['run']['state']=='succeeded': break
                time.sleep(.02)
            assert snapshot['run']['state']=='succeeded'
            with closing(sqlite3.connect(f"file:{tmp_path/'synapse.sqlite'}?mode=ro",uri=True)) as db:
                assert db.execute('SELECT state FROM task_attempts ORDER BY attempt_no').fetchall()==[('failed',),('completed',)]
                assert db.execute("SELECT count(*) FROM audit_entries WHERE action='mock.resume'").fetchone()==(1,)
    finally:
        for child in (proc,restarted):
            if child and child.poll() is None:
                child.send_signal(signal.SIGTERM)
                child.communicate(timeout=8)


def test_openapi_resume_schema_has_no_dangling_local_definitions():
    from synapse.api.app import create_app
    app = create_app(token='x'*32, read_events=lambda after, limit: [])
    schema = app.openapi()['paths']['/api/v1/runs/{run_id}/mock-resume']['post']
    body = schema['requestBody']['content']['application/json']['schema']
    assert '$ref' not in str(body)
    assert body['properties']['tasks']['items']['properties']['unknown_attempt_id']
    assert {h['name'] for h in schema['parameters']} >= {'Idempotency-Key', 'X-Correlation-ID'}


def test_inconsistent_older_unknown_attempt_cannot_be_reconciled(tmp_path):
    with daemon_database(tmp_path / 'db', ROOT / 'migrations') as db:
        seed(db)
        db.execute("UPDATE task_attempts SET attempt_no=0 WHERE id='attempt'")
        db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('latest','task',1,?,'completed')", (PROFILE,))
        with pytest.raises(StateConflict, match='history'):
            resume_fixed_mock(db, **params())
        assert db.execute("SELECT state FROM task_attempts WHERE id='attempt'").fetchone() == ('unknown',)
        assert db.execute("SELECT state FROM runs").fetchone() == ('recovering',)
