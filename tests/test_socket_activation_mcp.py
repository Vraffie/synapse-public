"""Exercise both daemon transports with one supervisor-retained listener."""
from contextlib import closing
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import httpx

from synapse.infrastructure.daemon_database import daemon_database

ROOT = Path(__file__).resolve().parents[1]


def test_activated_daemon_opt_in_ipc_shutdown_and_restart(tmp_path):
    api_path = tmp_path / 'agentd.sock'
    ipc_path = tmp_path / 'mock-mcp.sock'
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as listener:
        listener.bind(str(api_path))
        api_path.chmod(0o600)
        listener.listen(128)
        inode = api_path.lstat().st_ino
        def launch():
            return subprocess.Popen([sys.executable, '-m', 'synapse.daemon', '--state-dir', str(tmp_path),
                                     '--migrations', str(ROOT / 'migrations'), '--socket-fd', str(listener.fileno()),
                                     '--enable-mock-mcp-ipc'], pass_fds=(listener.fileno(),),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for iteration in range(2):
            proc = launch()
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        raise AssertionError(proc.stderr.read().decode())
                    if ipc_path.exists() and (tmp_path / 'operator.token').exists():
                        break
                    time.sleep(0.02)
                else:
                    raise AssertionError('IPC did not start')
                token = (tmp_path / 'operator.token').read_text()
                headers = {'Authorization': 'Bearer ' + token, 'Idempotency-Key': f'publish-{iteration}', 'X-Correlation-ID': 'trace'}
                with httpx.Client(transport=httpx.HTTPTransport(uds=str(api_path)), base_url='http://localhost', timeout=5) as client:
                    assert client.get('/api/v1/system/health', headers=headers).json() == {'status': 'ok'}
                    result = client.post('/api/v1/workflows', headers=headers, json={'workflow_id': f'wf-{iteration}', 'version': '1', 'nodes': [{'id': 'task', 'dependencies': []}]})
                    assert result.status_code == 201
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as ipc:
                        ipc.settimeout(5)
                        ipc.connect(str(ipc_path))
                        ipc.sendall(b'{"credential":"not-a-session","operation":"claim","arguments":{"idempotency_key":"x"}}\n')
                        assert json.loads(ipc.makefile('rb').readline()) == {'ok': False, 'error': 'request failed'}
                    assert client.get('/api/v1/system/health', headers=headers).status_code == 200
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                _, stderr = proc.communicate(timeout=8)
            assert proc.returncode == 0, stderr.decode()
            assert not ipc_path.exists()
            assert api_path.exists() and api_path.lstat().st_ino == inode
            with daemon_database(tmp_path / 'synapse.sqlite', ROOT / 'migrations') as db:
                assert db.execute('SELECT COUNT(*) FROM workflow_versions').fetchone() == (iteration + 1,)
