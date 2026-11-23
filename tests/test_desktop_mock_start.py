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
from synapse.persistence.workflow_store import publish_workflow, create_run

ROOT = Path(__file__).resolve().parents[1]


def spin(app, predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert predicate()


def test_failed_start_retries_same_intent_and_discards_other_run_display(tmp_path):
    app = QApplication.instance() or QApplication([])
    calls = []
    def fetch(_state, endpoint):
        if 'health' in endpoint:
            return {'status': 'ok'}
        if 'events' in endpoint:
            return {'events': []}
        if 'approvals' in endpoint:
            return {'approvals': []}
        if '/threads?' in endpoint:
            return {'threads': []}
        return {'run': {'id': endpoint.rsplit('/', 1)[-1], 'state': 'queued'}, 'tasks': [], 'dependencies': []}
    def send(_state, endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        if len(calls) == 1:
            raise OSError('lost response')
        return {'run_id': 'run', 'fixture': 'fixed-v1', 'state': 'running'}
    poller = Poller(tmp_path, fetch=fetch, send=send, interval=0.01)
    window = MainWindow(tmp_path, poller=poller)
    try:
        window.run_id.setText('run')
        window._select_run()
        spin(app, lambda: window.mock_button.isEnabled())
        window.mock_button.click()
        spin(app, lambda: 'response lost' in window.mock_status.text())
        assert window.mock_button.isEnabled()
        window.mock_button.click()
        spin(app, lambda: 'accepted' in window.mock_status.text())
        assert not window.mock_button.isEnabled()
        assert len(calls) == 2
        assert calls[0][1]['idempotency_key'] == calls[1][1]['idempotency_key']
        assert calls[0][1]['body'] == b''
        window.run_id.setText('other')
        window._select_run()
        assert 'accepted' not in window.mock_status.text()
    finally:
        window.close()
        assert not poller.isRunning()


def test_desktop_starts_real_daemon_mock_run(tmp_path):
    with daemon_database(tmp_path / 'synapse.sqlite', ROOT / 'migrations') as db:
        publish_workflow(db, workflow_id='wf', version='1', nodes=[{'id': 'node', 'dependencies': []}], principal='operator', idempotency_key='publish')
        run_id = create_run(db, workflow_id='wf', version='1', principal='operator', idempotency_key='create').response['run_id']
    proc = subprocess.Popen([sys.executable, '-m', 'synapse.daemon', '--state-dir', str(tmp_path), '--migrations', str(ROOT / 'migrations')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    app = QApplication.instance() or QApplication([])
    window = None
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / 'agentd.sock').exists():
            if proc.poll() is not None:
                raise AssertionError(proc.stderr.read().decode())
            if time.monotonic() > deadline:
                raise AssertionError('daemon failed to bind')
            time.sleep(0.02)
        poller = Poller(tmp_path, interval=0.02)
        window = MainWindow(tmp_path, poller=poller)
        window.run_id.setText(run_id)
        window._select_run()
        spin(app, lambda: window.mock_button.isEnabled(), seconds=8)
        window.mock_button.click()
        spin(app, lambda: 'accepted' in window.mock_status.text(), seconds=8)
        spin(app, lambda: 'succeeded' in window.run_status.text(), seconds=8)
    finally:
        if window is not None:
            window.close()
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
    assert proc.returncode == 0
