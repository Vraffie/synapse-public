import json
import os
from pathlib import Path
import queue
import re
import signal
import sqlite3
import subprocess
import sys
from threading import Thread
import time

import httpx
import pytest

from synapse.cli.__main__ import main
from synapse.persistence.mock_resume import ASSERTION

ROOT = Path(__file__).resolve().parents[1]


def test_cli_sends_exact_evidence_and_idempotency_headers(tmp_path, monkeypatch, capsys):
    from synapse.cli import __main__ as cli
    evidence = b'{"expected_run_version": 3, "tasks": [], "assertion": "fixed-v1-no-external-effects"}'
    path = tmp_path / 'evidence.json'
    path.write_bytes(evidence)
    sent = []
    def request(state, endpoint, **kwargs):
        sent.append((state, endpoint, kwargs))
        return {'run_id': 'run one', 'state': 'running'}
    monkeypatch.setattr(cli, '_request', request)
    args = ['--state-dir', str(tmp_path), 'mock-resume', 'run one', str(path), '--idempotency-key', 'key', '--correlation-id', 'trace']
    assert main(args) == 0
    assert sent == [(tmp_path, '/api/v1/runs/run%20one/mock-resume', {'body': evidence, 'idempotency_key': 'key', 'correlation_id': 'trace'})]
    assert json.loads(capsys.readouterr().out)['state'] == 'running'


@pytest.mark.parametrize('kind', ['symlink', 'large', 'directory'])
def test_cli_rejects_unsafe_evidence_before_transport(tmp_path, monkeypatch, capsys, kind):
    from synapse.cli import __main__ as cli
    path = tmp_path / 'evidence'
    if kind == 'symlink':
        target = tmp_path / 'target'
        target.write_text('{}')
        path.symlink_to(target)
    elif kind == 'large':
        path.write_bytes(b'x' * (1024 * 1024 + 1))
    else:
        path.mkdir()
    def unexpected(*args, **kwargs):
        pytest.fail('must reject evidence before connecting')
    monkeypatch.setattr(cli, '_request', unexpected)
    assert main(['--state-dir', str(tmp_path), 'mock-resume', 'run', str(path), '--idempotency-key', 'key', '--correlation-id', 'trace']) == 1
    assert 'agentctl:' in capsys.readouterr().err


def test_cli_refuses_unsafe_run_and_missing_intent(tmp_path):
    with pytest.raises(SystemExit):
        main(['--state-dir', str(tmp_path), 'mock-resume', '../run', 'evidence', '--idempotency-key', 'key', '--correlation-id', 'trace'])
    with pytest.raises(SystemExit):
        main(['--state-dir', str(tmp_path), 'mock-resume', 'run', 'evidence', '--idempotency-key', 'key'])


def test_cli_explicit_resume_after_supervised_sigkill(tmp_path, capsys):
    harness = tmp_path / 'harness'
    harness.mkdir()
    marker = harness / 'unblock'
    (harness / 'sitecustomize.py').write_text('''
import asyncio,os
from pathlib import Path
if not Path(os.environ['SYNAPSE_TEST_UNBLOCK']).exists():
    from synapse.providers.mock import MockProvider
    from synapse.providers.contracts import ProviderEvent
    async def blocked(self, handle):
        yield ProviderEvent('started')
        await asyncio.Event().wait()
    MockProvider.stream = blocked
''')
    env = dict(os.environ, SYNAPSE_TEST_UNBLOCK=str(marker))
    env['PYTHONPATH'] = str(harness) + os.pathsep + env.get('PYTHONPATH', '')
    command = [sys.executable, '-m', 'synapse.supervisor', '--state-dir', str(tmp_path), '--migrations', str(ROOT / 'migrations'), '--base-delay', '0.01', '--max-delay', '0.02']
    supervisor = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)
    output = queue.Queue()
    def collect():
        for line in supervisor.stderr:
            output.put(line)
    reader = Thread(target=collect, daemon=True)
    reader.start()
    def next_pid():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try: line = output.get(timeout=.1)
            except queue.Empty: continue
            match = re.search(r'started daemon pid=(\d+)', line)
            if match: return int(match.group(1))
        raise AssertionError('daemon did not start')
    def wait_health():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if supervisor.poll() is not None:
                raise AssertionError('supervisor exited before daemon became ready')
            try:
                token = (tmp_path / 'operator.token').read_text()
                with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / 'agentd.sock')), base_url='http://localhost', timeout=1) as client:
                    response = client.get('/api/v1/system/health', headers={'Authorization': 'Bearer ' + token})
                    if response.status_code == 200 and response.json() == {'status': 'ok'}:
                        return token
            except (OSError, ValueError, httpx.HTTPError):
                pass
            time.sleep(.02)
        raise AssertionError('authenticated daemon health did not become ready')
    try:
        pid = next_pid()
        token = wait_health()
        headers = {'Authorization': 'Bearer ' + token, 'Idempotency-Key': 'p', 'X-Correlation-ID': 'trace'}
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / 'agentd.sock')), base_url='http://localhost', timeout=8) as client:
            assert client.post('/api/v1/workflows', headers=headers, json={'workflow_id':'wf', 'version':'1', 'nodes':[{'id':'task','dependencies':[]}]}).status_code == 201
            headers['Idempotency-Key'] = 'c'
            run_id = client.post('/api/v1/runs', headers=headers, json={'workflow_id':'wf', 'workflow_version':'1'}).json()['run_id']
            headers['Idempotency-Key'] = 's'
            assert client.post(f'/api/v1/runs/{run_id}/mock-start', headers=headers).status_code == 202
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with sqlite3.connect(f"file:{tmp_path / 'synapse.sqlite'}?mode=ro", uri=True) as db:
                attempt = db.execute('SELECT id,state FROM task_attempts').fetchone()
            if attempt and attempt[1] == 'running': break
            time.sleep(.02)
        assert attempt and attempt[1] == 'running'
        marker.touch()
        os.kill(pid, signal.SIGKILL)
        assert next_pid() != pid
        assert wait_health() == token
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with sqlite3.connect(f"file:{tmp_path / 'synapse.sqlite'}?mode=ro", uri=True) as db:
                run = db.execute('SELECT state,state_version FROM runs WHERE id=?', (run_id,)).fetchone()
                task = db.execute('SELECT id,state,state_version FROM tasks WHERE run_id=?', (run_id,)).fetchone()
            if run[0] == 'recovering': break
            time.sleep(.02)
        assert run[0] == 'recovering' and task[1] == 'blocked'
        evidence = {'expected_run_version':run[1], 'tasks':[{'task_id':task[0], 'expected_version':task[2], 'unknown_attempt_id':attempt[0]}], 'assertion':ASSERTION}
        file = tmp_path / 'operator-evidence.json'
        file.write_text(json.dumps(evidence))
        args = ['--state-dir', str(tmp_path), 'mock-resume', run_id, str(file), '--idempotency-key', 'resume', '--correlation-id', 'trace']
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out)['state'] == 'running'
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out)['run_id'] == run_id
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with sqlite3.connect(f"file:{tmp_path / 'synapse.sqlite'}?mode=ro", uri=True) as db:
                state = db.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone()[0]
            if state == 'succeeded': break
            time.sleep(.02)
        assert state == 'succeeded'
        with sqlite3.connect(f"file:{tmp_path / 'synapse.sqlite'}?mode=ro", uri=True) as db:
            assert db.execute('SELECT state FROM task_attempts ORDER BY attempt_no').fetchall() == [('failed',), ('completed',)]
            assert db.execute("SELECT count(*) FROM audit_entries WHERE action='mock.resume'").fetchone() == (1,)
    finally:
        if supervisor.poll() is None:
            supervisor.send_signal(signal.SIGTERM)
            supervisor.wait(timeout=10)
        reader.join(timeout=2)
