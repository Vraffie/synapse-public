import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication
from synapse.desktop.__main__ import MainWindow, Poller
from synapse.infrastructure.daemon_database import daemon_database
from synapse.persistence.workflow_store import create_run, publish_workflow

ROOT = Path(__file__).resolve().parents[1]


def spin(app, predicate, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_cancel_lost_response_retries_same_key_and_waits_for_terminal_state(tmp_path):
    app = QApplication.instance() or QApplication([])
    state = ['running']
    calls = []
    def fetch(_dir, endpoint):
        if 'health' in endpoint:
            return {'status': 'ok'}
        if 'events' in endpoint:
            return {'events': []}
        if 'approvals' in endpoint:
            return {'approvals': []}
        if '/threads?' in endpoint:
            return {'threads': []}
        return {'run': {'id': endpoint.rsplit('/', 1)[-1], 'state': state[0]}, 'tasks': [], 'dependencies': []}
    def send(_dir, endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        if len(calls) == 1:
            raise OSError('response lost')
        return {'run_id': 'run', 'state': 'cancellation_requested'}
    window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=fetch, send=send, interval=0.01))
    try:
        window.run_id.setText('run')
        window._select_run()
        spin(app, lambda: window.cancel_mock_button.isEnabled())
        window.cancel_mock_button.click()
        spin(app, lambda: 'response lost' in window.cancel_mock_status.text())
        assert window.cancel_mock_button.isEnabled()
        window.cancel_mock_button.click()
        spin(app, lambda: 'waiting for terminal' in window.cancel_mock_status.text())
        assert not window.cancel_mock_button.isEnabled()
        assert len(calls) == 2 and all(call[0] == '/api/v1/runs/run/mock-cancel' for call in calls)
        assert calls[0][1]['idempotency_key'] == calls[1][1]['idempotency_key']
        assert calls[0][1]['correlation_id'] == calls[1][1]['correlation_id'] == calls[0][1]['idempotency_key']
        assert calls[0][1]['body'] == b''
        state[0] = 'cancelled'
        spin(app, lambda: 'Run is cancelled' in window.cancel_mock_status.text())
        assert not window.cancel_mock_button.isEnabled()
        window.run_id.setText('other')
        window._select_run()
        assert 'Cancellation requested' not in window.cancel_mock_status.text()
    finally:
        window.close()
        assert not window.poller.isRunning()


def test_desktop_cancels_real_daemon_running_mock(tmp_path):
    import httpx
    with daemon_database(tmp_path / 'synapse.sqlite', ROOT / 'migrations') as db:
        publish_workflow(db, workflow_id='wf', version='1', nodes=[{'id': 'node', 'dependencies': []}], principal='operator', idempotency_key='publish')
        run_id = create_run(db, workflow_id='wf', version='1', principal='operator', idempotency_key='create').response['run_id']
    code = '''
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
    proc = subprocess.Popen([sys.executable, '-c', code, str(tmp_path), str(ROOT / 'migrations')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    app = QApplication.instance() or QApplication([])
    window = None
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / 'agentd.sock').exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError('daemon exited')
            time.sleep(0.02)
        token = (tmp_path / 'operator.token').read_text()
        headers = {'Authorization': 'Bearer ' + token, 'Idempotency-Key': 'start', 'X-Correlation-ID': 'trace'}
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(tmp_path / 'agentd.sock')), base_url='http://localhost') as client:
            while time.monotonic() < deadline:
                try:
                    if client.get('/api/v1/system/health', headers=headers).status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(0.02)
            assert client.post(f'/api/v1/runs/{run_id}/mock-start', headers=headers).status_code == 202
        window = MainWindow(tmp_path, poller=Poller(tmp_path, interval=0.02))
        window.run_id.setText(run_id)
        window._select_run()
        spin(app, lambda: window.cancel_mock_button.isEnabled(), seconds=8)
        window.cancel_mock_button.click()
        spin(app, lambda: 'Run is cancelled' in window.cancel_mock_status.text(), seconds=8)
        assert not window.cancel_mock_button.isEnabled()
        assert 'cancelled' in window.run_status.text()
    finally:
        if window is not None:
            window.close()
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
    assert proc.returncode == 0
    with daemon_database(tmp_path / 'synapse.sqlite', ROOT / 'migrations') as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE type='mock.cancellation_requested' AND run_id=?", (run_id,)).fetchone() == (1,)
        assert db.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone() == ('cancelled',)


def test_late_cancel_response_does_not_apply_to_another_selected_run(tmp_path):
    from threading import Event
    app = QApplication.instance() or QApplication([])
    entered, release = Event(), Event()
    def fetch(_dir, endpoint):
        if 'health' in endpoint:
            return {'status': 'ok'}
        if 'events' in endpoint:
            return {'events': []}
        if 'approvals' in endpoint:
            return {'approvals': []}
        if '/threads?' in endpoint:
            return {'threads': []}
        return {'run': {'id': endpoint.rsplit('/', 1)[-1], 'state': 'running'}, 'tasks': [], 'dependencies': []}
    def send(_dir, endpoint, **kwargs):
        entered.set()
        assert release.wait(4)
        return {'run_id': 'first', 'state': 'cancellation_requested'}
    window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=fetch, send=send, interval=0.01))
    try:
        window.run_id.setText('first')
        window._select_run()
        spin(app, lambda: window.cancel_mock_button.isEnabled())
        window.cancel_mock_button.click()
        assert entered.wait(3)
        window.run_id.setText('second')
        window._select_run()
        release.set()
        spin(app, lambda: window._cancel_results.get('first') == 'accepted')
        assert 'Cancellation requested' not in window.cancel_mock_status.text()
        window.run_id.setText('first')
        window._select_run()
        spin(app, lambda: 'waiting for terminal' in window.cancel_mock_status.text())
    finally:
        release.set()
        window.close()
        assert not window.poller.isRunning()


def test_invalid_run_selection_clears_actions_and_fences_old_snapshot(tmp_path):
    app = QApplication.instance() or QApplication([])
    def fetch(_dir, endpoint):
        if 'health' in endpoint:
            return {'status': 'ok'}
        if 'events' in endpoint:
            return {'events': []}
        if 'approvals' in endpoint:
            return {'approvals': []}
        if '/threads?' in endpoint:
            return {'threads': []}
        return {'run': {'id': 'first', 'state': 'running'}, 'tasks': [], 'dependencies': []}
    calls = []
    def send(_dir, endpoint, **kwargs):
        calls.append(endpoint)
        return {'run_id': 'first', 'state': 'cancellation_requested'}
    window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=fetch, send=send, interval=0.01))
    try:
        window.run_id.setText('first')
        window._select_run()
        spin(app, lambda: window.cancel_mock_button.isEnabled())
        old_generation = window._active_generation
        window.run_id.setText('invalid/id')
        window._select_run()
        assert window._active_run == '' and window._active_generation > old_generation
        assert not window.cancel_mock_button.isEnabled() and not window.mock_button.isEnabled()
        assert window.graph_scene.items() == []
        assert window.run_status.text() == 'Enter a valid run ID.'
        window._show_run('first', old_generation, {'run': {'id': 'first', 'state': 'running'}, 'tasks': [], 'dependencies': []})
        window.cancel_mock_button.click()
        assert not window.cancel_mock_button.isEnabled() and calls == []
        assert window.run_status.text() == 'Enter a valid run ID.'
    finally:
        window.close()
        assert not window.poller.isRunning()


def test_editing_run_input_disables_controls_until_selection_matches(tmp_path):
    app = QApplication.instance() or QApplication([])
    def fetch(_dir, endpoint):
        if 'health' in endpoint:
            return {'status': 'ok'}
        if 'events' in endpoint:
            return {'events': []}
        if 'approvals' in endpoint:
            return {'approvals': []}
        if '/threads?' in endpoint:
            return {'threads': []}
        return {'run': {'id': 'first', 'state': 'running'}, 'tasks': [], 'dependencies': []}
    calls = []
    def send(_dir, endpoint, **kwargs):
        calls.append(endpoint)
        return {'run_id': 'first', 'state': 'cancellation_requested'}
    window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=fetch, send=send, interval=0.01))
    try:
        window.run_id.setText('first')
        window._select_run()
        spin(app, lambda: window.cancel_mock_button.isEnabled())
        window.run_id.setText('different')
        assert not window.cancel_mock_button.isEnabled() and not window.mock_button.isEnabled()
        window.cancel_mock_button.click()
        assert calls == []
        window.run_id.setText('first')
        assert window.cancel_mock_button.isEnabled()
    finally:
        window.close()


def test_invalid_run_graph_never_enables_mock_actions(tmp_path):
    app = QApplication.instance() or QApplication([])
    def fetch(_dir, endpoint):
        if 'health' in endpoint:
            return {'status': 'ok'}
        if 'events' in endpoint:
            return {'events': []}
        if 'approvals' in endpoint:
            return {'approvals': []}
        if '/threads?' in endpoint:
            return {'threads': []}
        return {'run': {'id': 'first', 'state': 'running'}, 'tasks': [{'id': 'duplicate'}, {'id': 'duplicate'}], 'dependencies': []}
    window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=fetch, interval=0.01))
    try:
        window.run_id.setText('first')
        window._select_run()
        spin(app, lambda: 'invalid' in window.run_status.text())
        assert window._known_run_state is None
        assert not window.cancel_mock_button.isEnabled() and not window.mock_button.isEnabled()
        window._mock_results['first'] = 'failed'
        window._update_mock_controls()
        assert not window.mock_button.isEnabled()
    finally:
        window.close()
