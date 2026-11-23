from contextlib import closing
from datetime import datetime, timedelta, timezone
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from synapse.api.app import create_app
from synapse.domain.contracts import MessageEnvelope, MessageId, MessageType, RunId
from synapse.infrastructure.daemon_database import daemon_database
from synapse.persistence.approval_store import write_approval_request
from synapse.persistence.message_store import send_message
from synapse.persistence.workflow_store import create_run, publish_workflow

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 's' * 43
HEADERS = {'Authorization': 'Bearer ' + TOKEN}


def test_errors_preserve_detail_and_use_safe_correlated_envelopes():
    def fail(_after, _limit):
        raise RuntimeError('secret exception should not be exposed')
    app = create_app(token=TOKEN, read_events=fail)
    with TestClient(app, client=None, raise_server_exceptions=False) as client:
        auth = client.get('/api/v1/events')
        assert auth.status_code == 401 and auth.headers['www-authenticate'] == 'Bearer'
        assert auth.json()['code'] == 'unauthorized' and auth.json()['detail'] == 'Authentication required'
        failure = client.get('/api/v1/events', headers={**HEADERS, 'X-Correlation-ID': 'trace'})
        assert failure.status_code == 500 and failure.json()['code'] == 'internal_error'
        assert 'secret' not in failure.text and failure.json()['correlation_id'] == 'trace'
        invalid = client.get('/api/v1/events?after=-1', headers=HEADERS)
        assert invalid.status_code == 422 and invalid.json()['code'] == 'validation_error'
        assert isinstance(invalid.json()['detail'], list)
        assert invalid.json()['details']['errors'] == invalid.json()['detail']
        assert all(item['msg'] == 'Invalid value' for item in invalid.json()['detail'])
        assert 'input' not in invalid.text
        sensitive = client.get('/api/v1/events?after=do-not-echo-this', headers=HEADERS)
        assert sensitive.status_code == 422 and 'do-not-echo-this' not in sensitive.text
        assert sensitive.json()['detail'][0]['msg'] == 'Invalid value'
        missing = client.get('/missing', headers=HEADERS)
        assert missing.status_code == 404 and missing.json()['code'] == 'not_found'
        for response in (auth, failure, invalid, missing):
            assert response.headers['cache-control'] == 'no-store'
            assert response.headers['x-correlation-id'] == response.json()['correlation_id']
            assert set(response.json()) == {'code', 'message', 'details', 'correlation_id', 'detail'}


def test_openapi_schemas_match_real_daemon_responses(tmp_path):
    with daemon_database(tmp_path / 'synapse.sqlite', ROOT / 'migrations') as db:
        publish_workflow(db, workflow_id='wf', version='1', nodes=[{'id': 'task', 'dependencies': []}], principal='operator', idempotency_key='publish')
        run_id = create_run(db, workflow_id='wf', version='1', principal='operator', idempotency_key='create').response['run_id']
        db.execute("INSERT INTO agent_definitions VALUES('agent','1',NULL,'{}')")
        for instance in ('a', 'b'):
            db.execute("INSERT INTO agent_instances(id,run_id,definition_id,definition_version,state,created_at) VALUES(?,?,'agent','1','running','now')", (instance, run_id))
        db.execute("INSERT INTO threads VALUES('thread',?,'Review','open','now')", (run_id,))
        message = MessageEnvelope(id=MessageId('msg'), run_id=RunId(run_id), thread_id='thread', sender_instance_id='a', type=MessageType.QUESTION, payload={'text':'hi'}, created_at='2026-01-01T00:00:00+00:00')
        send_message(db, message, recipients=('b',), principal='a', idempotency_key='send')
        db.execute('BEGIN IMMEDIATE')
        write_approval_request(db, approval_id='approval', run_id=run_id, operation='push', target='repo', candidate_sha256='a'*64, policy_version='1', expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
        db.execute('COMMIT')
    script = '''
import asyncio, sys
from synapse.providers.mock import MockProvider
from synapse.daemon.__main__ import main
async def hold(self, handle):
    await asyncio.sleep(60)
    yield None
MockProvider.stream = hold
sys.argv = ["agentd", "--state-dir", sys.argv[1], "--migrations", sys.argv[2]]
main()
'''
    proc = subprocess.Popen([sys.executable, '-c', script, str(tmp_path), str(ROOT / 'migrations')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / 'agentd.sock').exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError('daemon exited')
            time.sleep(0.02)
        token = (tmp_path / 'operator.token').read_text()
        headers = {'Authorization': 'Bearer ' + token, 'Idempotency-Key': 'key', 'X-Correlation-ID': 'trace'}
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / 'agentd.sock')), base_url='http://localhost', timeout=5) as client:
            while time.monotonic() < deadline:
                try:
                    if client.get('/api/v1/system/health', headers=headers).status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(0.02)
            openapi = client.get('/openapi.json', headers=headers).json()
            def verify(path_template, method, response, status):
                assert response.status_code == status
                schema = openapi['paths'][path_template][method]['responses'][str(status)]['content']['application/json']['schema']
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(response.json())
            for template, path in [
                ('/api/v1/system/health', '/api/v1/system/health'),
                ('/api/v1/events', '/api/v1/events'),
                ('/api/v1/runs', '/api/v1/runs'),
                ('/api/v1/runs/{run_id}', f'/api/v1/runs/{run_id}'),
                ('/api/v1/runs/{run_id}/tasks', f'/api/v1/runs/{run_id}/tasks'),
                ('/api/v1/runs/{run_id}/threads', f'/api/v1/runs/{run_id}/threads'),
                ('/api/v1/runs/{run_id}/threads/{thread_id}/messages', f'/api/v1/runs/{run_id}/threads/thread/messages'),
                ('/api/v1/approvals', '/api/v1/approvals'),
            ]:
                verify(template, 'get', client.get(path, headers=headers), 200)
            headers['Idempotency-Key'] = 'publish-2'
            response = client.post('/api/v1/workflows', headers=headers, json={'workflow_id':'wf2','version':'1','nodes':[{'id':'task','dependencies':[]}]})
            verify('/api/v1/workflows', 'post', response, 201)
            headers['Idempotency-Key'] = 'create-2'
            verify('/api/v1/runs', 'post', client.post('/api/v1/runs', headers=headers, json={'workflow_id':'wf2','workflow_version':'1'}), 202)
            headers['Idempotency-Key'] = 'start'
            verify('/api/v1/runs/{run_id}/mock-start', 'post', client.post(f'/api/v1/runs/{run_id}/mock-start', headers=headers), 202)
            headers['Idempotency-Key'] = 'cancel'
            verify('/api/v1/runs/{run_id}/mock-cancel', 'post', client.post(f'/api/v1/runs/{run_id}/mock-cancel', headers=headers), 202)
            verify('/api/v1/runs', 'get', client.get('/api/v1/runs?limit=1', headers=headers), 200)
            verify('/api/v1/runs', 'get', client.get('/api/v1/runs?limit=0', headers=headers), 422)
            error = client.get('/api/v1/events?limit=0', headers=headers)
            verify('/api/v1/events', 'get', error, 422)
            params = openapi['paths']['/api/v1/runs/{run_id}/mock-cancel']['post']['parameters']
            assert {p['name'] for p in params if p['in'] == 'header' and p['required']} == {'Idempotency-Key', 'X-Correlation-ID'}
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)


def test_health_documents_only_reachable_error_statuses():
    paths = create_app(token=TOKEN, read_events=lambda _a, _l: []).openapi()["paths"]
    assert set(paths["/api/v1/system/health"]["get"]["responses"]) == {"200", "401"}
    for route, operations in paths.items():
        if route == "/api/v1/system/health":
            continue
        for operation in operations.values():
            assert {"401", "422", "500"} <= set(operation["responses"]), route
