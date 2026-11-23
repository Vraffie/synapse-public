from contextlib import contextmanager
import json
import os
import socket
from threading import Thread

import pytest

from synapse.cli.__main__ import main

TOKEN = "a" * 43


@pytest.fixture
def state(tmp_path):
    tmp_path.chmod(0o700)
    token = tmp_path / "operator.token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    return tmp_path


@contextmanager
def daemon(state, body=b'{"status":"ok"}', status=200):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = state / "agentd.sock"
    server.bind(str(path))
    path.chmod(0o600)
    server.listen(1)
    received = []
    def serve():
        conn, _ = server.accept()
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            head, _, rest = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value.strip())
            while len(rest) < length:
                rest += conn.recv(4096)
            received.append(data)
            conn.sendall(f"HTTP/1.1 {status} status\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    thread = Thread(target=serve)
    thread.start()
    try:
        yield received
    finally:
        thread.join(timeout=5)
        server.close()
        assert not thread.is_alive()


def test_health_authenticates_over_unix_socket(state, capsys):
    with daemon(state) as received:
        assert main(["--state-dir", str(state), "health"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"status": "ok"}
    assert TOKEN not in output.out + output.err
    assert received[0].startswith(b"GET /api/v1/system/health HTTP/1.1")
    assert b"Authorization: Bearer " + TOKEN.encode() in received[0]


def test_events_uses_cursor_and_page_limit(state, capsys):
    with daemon(state, b'{"events":[]}') as received:
        assert main(["--state-dir", str(state), "events", "--after", "42", "--limit", "5"]) == 0
    assert b"GET /api/v1/events?after=42&limit=5 " in received[0]
    assert json.loads(capsys.readouterr().out) == {"events": []}


@pytest.mark.parametrize("kind", ["permissions", "symlink", "hardlink", "bad_token", "directory"])
def test_rejects_unsafe_token_or_directory_before_connecting(state, kind, capsys):
    token = state / "operator.token"
    if kind == "permissions":
        token.chmod(0o644)
    elif kind == "symlink":
        token.unlink()
        token.symlink_to("other")
    elif kind == "hardlink":
        os.link(token, state / "alias")
    elif kind == "bad_token":
        token.write_text("invalid")
    else:
        state.chmod(0o755)
    assert main(["--state-dir", str(state), "health"]) == 1
    assert TOKEN not in capsys.readouterr().err


def test_daemon_failure_does_not_echo_body(state, capsys):
    with daemon(state, TOKEN.encode(), status=401):
        assert main(["--state-dir", str(state), "health"]) == 1
    captured = capsys.readouterr()
    assert "HTTP 401" in captured.err
    assert TOKEN not in captured.out + captured.err


@pytest.mark.parametrize("arguments", [["--after", "-1"], ["--limit", "1001"], ["--after", "9223372036854775808"]])
def test_invalid_cursor_rejected_before_connecting(state, arguments):
    with pytest.raises(SystemExit) as error:
        main(["--state-dir", str(state), "events", *arguments])
    assert error.value.code == 2


@pytest.mark.parametrize("command,expected", [("run", b"/api/v1/runs/run%20one"), ("tasks", b"/api/v1/runs/run%20one/tasks")])
def test_run_queries_encode_path(state, capsys, command, expected):
    with daemon(state, b'{}') as received:
        assert main(["--state-dir", str(state), command, "run one"]) == 0
    assert received[0].startswith(b"GET " + expected + b" HTTP/1.1")
    assert json.loads(capsys.readouterr().out) == {}


def test_approval_query_encodes_cursor(state, capsys):
    with daemon(state, b'{"approvals":[]}') as received:
        assert main(["--state-dir", str(state), "approvals", "--after", "one & two", "--limit", "7"]) == 0
    assert b"GET /api/v1/approvals?after=one+%26+two&limit=7 " in received[0]
    assert json.loads(capsys.readouterr().out) == {"approvals": []}


@pytest.mark.parametrize("run_id", ["", " ", ".", "..", "other/path", "x" * 257])
def test_invalid_run_id_rejected_before_connecting(state, run_id):
    with pytest.raises(SystemExit) as exc:
        main(["--state-dir", str(state), "run", run_id])
    assert exc.value.code == 2


def test_approval_cursor_limit_checked(state):
    with pytest.raises(SystemExit) as exc:
        main(["--state-dir", str(state), "approvals", "--after", "x" * 257])
    assert exc.value.code == 2


@pytest.mark.parametrize("args,expected", [
    (["threads", "run one", "--after", "42", "--limit", "3"],
     b"/api/v1/runs/run%20one/threads?after=42&limit=3"),
    (["messages", "run&one", "thread?two", "--after", "7", "--limit", "5"],
     b"/api/v1/runs/run%26one/threads/thread%3Ftwo/messages?after=7&limit=5"),
])
def test_message_queries_encode_identifiers_and_pagination(state, capsys, args, expected):
    with daemon(state, b'{}') as received:
        assert main(["--state-dir", str(state), *args]) == 0
    assert received[0].startswith(b"GET " + expected + b" HTTP/1.1")
    assert json.loads(capsys.readouterr().out) == {}


@pytest.mark.parametrize("args", [
    ["threads", "run", "--limit", "101"], ["threads", "run", "--after", "-1"],
    ["messages", "run", "thread", "--after", "9223372036854775808"],
    ["messages", "run", "../bad"], ["messages", "run", ""],
    ["messages", "run", ".."], ["messages", "run", "x" * 257],
    ["threads", "run/other"],
])
def test_invalid_message_query_arguments_rejected_before_connecting(state, args):
    with pytest.raises(SystemExit) as exc:
        main(["--state-dir", str(state), *args])
    assert exc.value.code == 2


def test_message_queries_against_real_daemon(tmp_path, capsys):
    from contextlib import closing
    from pathlib import Path
    import signal
    import subprocess
    import sys
    import time

    from synapse.persistence.migrations import apply_migrations, open_database

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    tmp_path.chmod(0o700)
    with closing(open_database(tmp_path / "synapse.sqlite")) as db:
        apply_migrations(db, migrations)
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        db.execute("INSERT INTO agent_definitions VALUES('agent','1',NULL,'{}')")
        db.execute("INSERT INTO agent_instances(id,run_id,definition_id,definition_version,state,created_at) VALUES('agent','run','agent','1','running','now')")
        db.execute("INSERT INTO threads VALUES('thread','run','subject','open','now')")
        db.execute("INSERT INTO messages(id,thread_id,sender_instance_id,type,schema_version,payload_json,created_at) VALUES('message','thread','agent','question',1,'{\"text\":\"hello\"}','now')")
    (tmp_path / "synapse.sqlite").chmod(0o600)
    proc = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(tmp_path),
                             "--migrations", str(migrations)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if (tmp_path / "agentd.sock").exists():
                break
            if proc.poll() is not None:
                raise AssertionError("daemon exited before listening")
            time.sleep(0.02)
        assert (tmp_path / "agentd.sock").exists()
        ready = 1
        while time.monotonic() < deadline:
            ready = main(["--state-dir", str(tmp_path), "health"])
            capsys.readouterr()
            if ready == 0:
                break
            time.sleep(0.02)
        assert ready == 0
        assert main(["--state-dir", str(tmp_path), "threads", "run", "--limit", "1"]) == 0
        page = json.loads(capsys.readouterr().out)
        assert [thread["id"] for thread in page["threads"]] == ["thread"]
        assert main(["--state-dir", str(tmp_path), "messages", "run", "thread", "--limit", "1"]) == 0
        page = json.loads(capsys.readouterr().out)
        assert page["messages"][0]["payload"] == {"text": "hello"}
        cursor = page["messages"][0]["cursor"]
        assert main(["--state-dir", str(tmp_path), "messages", "run", "thread", "--after", str(cursor)]) == 0
        assert json.loads(capsys.readouterr().out)["messages"] == []
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
    assert proc.returncode == 0


def test_publish_refuses_symlink_and_oversize_before_connecting(state, capsys):
    workflow = state / "workflow.json"
    workflow.write_bytes(b"x" * (1024 * 1024 + 1))
    options = ["--idempotency-key", "key", "--correlation-id", "trace"]
    assert main(["--state-dir", str(state), "publish-workflow", str(workflow), *options]) == 1
    assert "1 MiB" in capsys.readouterr().err
    link = state / "link.json"
    link.symlink_to(workflow)
    assert main(["--state-dir", str(state), "publish-workflow", str(link), *options]) == 1


@pytest.mark.parametrize("value", ["", "bad key", "line\nbreak", "x" * 257])
def test_mutation_headers_are_validated_before_connecting(state, value):
    with pytest.raises(SystemExit) as exc:
        main(["--state-dir", str(state), "queue-run", "wf", "1", "--idempotency-key", value, "--correlation-id", "trace"])
    assert exc.value.code == 2


def test_mock_start_encodes_path_and_sends_required_headers(state, capsys):
    with daemon(state, b'{"state":"running"}', status=202) as received:
        assert main(["--state-dir", str(state), "mock-start", "run one", "--idempotency-key", "start-1", "--correlation-id", "trace-1"]) == 0
    assert received[0].startswith(b"POST /api/v1/runs/run%20one/mock-start HTTP/1.1")
    assert b"Idempotency-Key: start-1" in received[0]
    assert b"X-Correlation-ID: trace-1" in received[0]
    assert json.loads(capsys.readouterr().out) == {"state": "running"}


@pytest.mark.parametrize("run_id", ["", "..", "a/b", "x" * 257])
def test_mock_start_rejects_bad_run_ids_before_connecting(state, run_id):
    with pytest.raises(SystemExit) as exc:
        main(["--state-dir", str(state), "mock-start", run_id, "--idempotency-key", "key", "--correlation-id", "trace"])
    assert exc.value.code == 2


def test_mock_start_over_real_daemon(tmp_path, capsys):
    from contextlib import closing
    from pathlib import Path
    import signal
    import subprocess
    import sys
    import time
    from synapse.persistence.migrations import apply_migrations, open_database
    from synapse.persistence.workflow_store import publish_workflow, create_run

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with closing(open_database(tmp_path / "synapse.sqlite")) as db:
        apply_migrations(db, migrations)
        publish_workflow(db, workflow_id="wf", version="1", nodes=[{"id":"build", "dependencies":[]}], principal="operator", idempotency_key="p")
        run_id = create_run(db, workflow_id="wf", version="1", principal="operator", idempotency_key="c").response["run_id"]
    (tmp_path / "synapse.sqlite").chmod(0o600)
    proc = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(tmp_path), "--migrations", str(migrations)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "agentd.sock").exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError("daemon exited")
            time.sleep(0.02)
        ready = 1
        while time.monotonic() < deadline and ready:
            ready = main(["--state-dir", str(tmp_path), "health"])
            capsys.readouterr()
            if ready:
                time.sleep(0.02)
        assert ready == 0
        args = ["--state-dir", str(tmp_path), "mock-start", run_id, "--idempotency-key", "start", "--correlation-id", "trace"]
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out)["fixture"] == "fixed-v1"
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out)["run_id"] == run_id
        while time.monotonic() < deadline:
            assert main(["--state-dir", str(tmp_path), "run", run_id]) == 0
            state = json.loads(capsys.readouterr().out)["run"]["state"]
            if state == "succeeded":
                break
            time.sleep(0.02)
        assert state == "succeeded"
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)


def test_mock_cancel_encodes_path_and_sends_command_headers(state, capsys):
    response = b'{"run_id":"run one","state":"cancellation_requested"}'
    with daemon(state, response, status=202) as received:
        assert main(['--state-dir', str(state), 'mock-cancel', 'run one',
                     '--idempotency-key', 'cancel-1', '--correlation-id', 'trace-1']) == 0
    assert received[0].startswith(b'POST /api/v1/runs/run%20one/mock-cancel HTTP/1.1')
    assert b'Idempotency-Key: cancel-1' in received[0]
    assert b'X-Correlation-ID: trace-1' in received[0]
    assert json.loads(capsys.readouterr().out)['state'] == 'cancellation_requested'


@pytest.mark.parametrize('args', [
    ['run', '--correlation-id', 'trace'],
    ['run', '--idempotency-key', 'key'],
    ['run', '--idempotency-key', 'bad key', '--correlation-id', 'trace'],
    ['../run', '--idempotency-key', 'key', '--correlation-id', 'trace'],
])
def test_mock_cancel_rejects_invalid_headers_and_run_before_connecting(state, args):
    with pytest.raises(SystemExit) as exc:
        main(['--state-dir', str(state), 'mock-cancel', *args])
    assert exc.value.code == 2


def test_mock_cancel_cli_against_real_daemon(tmp_path, capsys):
    from pathlib import Path
    import signal
    import subprocess
    import sys
    import time
    import httpx

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
    root = Path(__file__).resolve().parents[1]
    tmp_path.chmod(0o700)
    proc = subprocess.Popen([sys.executable, '-c', script, str(tmp_path), str(root / 'migrations')],
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
            response = client.post('/api/v1/workflows', headers=headers, json={'workflow_id': 'wf', 'version': '1', 'nodes': [{'id': 'task', 'dependencies': []}]})
            assert response.status_code == 201
            headers['Idempotency-Key'] = 'create'
            run_id = client.post('/api/v1/runs', headers=headers, json={'workflow_id': 'wf', 'workflow_version': '1'}).json()['run_id']
            headers['Idempotency-Key'] = 'start'
            assert client.post(f'/api/v1/runs/{run_id}/mock-start', headers=headers).status_code == 202
            while time.monotonic() < deadline:
                snapshot = client.get(f'/api/v1/runs/{run_id}', headers=headers).json()
                if snapshot['tasks'][0]['state'] == 'running':
                    break
                time.sleep(0.02)
            assert snapshot['tasks'][0]['state'] == 'running'
            args = ['--state-dir', str(tmp_path), 'mock-cancel', run_id,
                    '--idempotency-key', 'cancel', '--correlation-id', 'trace']
            assert main(args) == 0
            result = capsys.readouterr()
            assert json.loads(result.out)['state'] == 'cancellation_requested'
            assert token not in result.out + result.err
            assert main(args) == 0
            assert json.loads(capsys.readouterr().out)['state'] == 'cancellation_requested'
            while time.monotonic() < deadline:
                snapshot = client.get(f'/api/v1/runs/{run_id}', headers=headers).json()
                if snapshot['run']['state'] == 'cancelled':
                    break
                time.sleep(0.02)
            assert snapshot['run']['state'] == 'cancelled'
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
    assert proc.returncode == 0


def test_http_rejection_exposes_status_without_response_body(state):
    from synapse.cli.__main__ import DaemonHTTPError, _request
    with daemon(state, b'sensitive server response', status=409):
        with pytest.raises(DaemonHTTPError) as exc:
            _request(state, '/api/v1/runs/run/mock-resume', body=b'{}', idempotency_key='key', correlation_id='trace')
    assert exc.value.status == 409
    assert 'sensitive server response' not in str(exc.value)


def test_runs_encodes_query_and_prints_table(state, capsys):
    body = (b'{"runs":[{"id":"run1","workflow_id":"wf","workflow_version":"1","state":"running","state_version":2,'
            b'"created_at":"t","updated_at":"t","task_counts":{"ready":1,"succeeded":2},"pending_approvals":1}],"next_cursor":"abc"}')
    with daemon(state, body) as received:
        assert main(["--state-dir", str(state), "runs", "--state", "running", "--limit", "5", "--cursor", "c&d"]) == 0
    assert b"GET /api/v1/runs?limit=5&state=running&cursor=c%26d " in received[0]
    out = capsys.readouterr().out
    assert "run1" in out and "wf@1" in out and "ready=1,succeeded=2" in out and "next: --cursor abc" in out


def test_runs_json_and_validation(state, capsys):
    with daemon(state, b'{"runs":[],"next_cursor":null}'):
        assert main(["--state-dir", str(state), "runs", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"runs": [], "next_cursor": None}
    for bad in (["--limit", "201"], ["--state", "bogus"]):
        with pytest.raises(SystemExit):
            main(["--state-dir", str(state), "runs", *bad])
