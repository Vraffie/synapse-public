import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from threading import Event
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication
from synapse.desktop.__main__ import MainWindow, Poller
from synapse.infrastructure.daemon_database import daemon_database
from synapse.persistence.approval_store import write_approval_request

ROOT = Path(__file__).resolve().parents[1]


def spin(app, predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_approval_pages_on_real_daemon(tmp_path):
    with daemon_database(tmp_path / 'synapse.sqlite', ROOT / 'migrations') as db:
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        for index in range(101):
            db.execute('BEGIN IMMEDIATE')
            write_approval_request(db, approval_id=f'a{index:03}', run_id='run', operation='push', target='repo',
                                   candidate_sha256='a' * 64, policy_version='v1', expires_at=expires)
            db.execute('COMMIT')
    proc = subprocess.Popen([sys.executable, '-m', 'synapse.daemon', '--state-dir', str(tmp_path),
                             '--migrations', str(ROOT / 'migrations')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    app = QApplication.instance() or QApplication([])
    window = None
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / 'agentd.sock').exists() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(0.02)
        window = MainWindow(tmp_path, poller=Poller(tmp_path, interval=0.02))
        spin(app, lambda: window.approvals_list.count() == 100 and window.approvals_next.isEnabled())
        generation = window._approval_page_generation
        window.approvals_next.click()
        spin(app, lambda: window.approvals_list.count() == 1 and 'a100' in window.approvals_list.item(0).text())
        window._show_approvals([{'id': 'stale'}], generation)
        window._approvals_unavailable(generation)
        assert 'a100' in window.approvals_list.item(0).text()
        assert 'unavailable' not in window.approvals_status.text()
        window.approvals_previous.click()
        spin(app, lambda: window.approvals_list.count() == 100 and 'a000' in window.approvals_list.item(0).text())
    finally:
        if window is not None:
            window.close()
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
    assert proc.returncode == 0


def test_invalid_or_unavailable_page_allows_previous_and_encodes_cursor(tmp_path):
    app = QApplication.instance() or QApplication([])
    calls = []
    failure = ['invalid']
    def fetch(_state, endpoint):
        if 'health' in endpoint:
            return {'status': 'ok'}
        if 'events' in endpoint:
            return {'events': []}
        if '/approvals?' in endpoint:
            calls.append(endpoint)
            cursor = parse_qs(urlsplit(endpoint).query).get('after', [''])[0]
            if cursor:
                if failure[0] == 'invalid':
                    return {'approvals': [{'id': None}]}
                raise OSError('offline')
            return {'approvals': [{'id': f'a{i:03}'} for i in range(99)] + [{'id': 'z & +'}]}
        raise AssertionError(endpoint)
    window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=fetch, interval=0.01))
    try:
        spin(app, lambda: window.approvals_next.isEnabled())
        window.approvals_next.click()
        spin(app, lambda: 'invalid' in window.approvals_status.text() and window.approvals_previous.isEnabled())
        assert 'no page loaded' in window.approvals_status.text()
        assert any('after=z+%26+%2B' in call for call in calls)
        window.approvals_previous.click()
        spin(app, lambda: window.approvals_next.isEnabled())
        failure[0] = 'offline'
        window.approvals_next.click()
        spin(app, lambda: 'unavailable' in window.approvals_status.text() and window.approvals_previous.isEnabled())
        window.approvals_previous.click()
        spin(app, lambda: window.approvals_next.isEnabled())
    finally:
        window.close()
        assert not window.poller.isRunning()


def test_out_of_order_approval_response_is_rejected(tmp_path):
    app = QApplication.instance() or QApplication([])
    def fetch(_state, endpoint):
        if 'health' in endpoint:
            return {'status': 'ok'}
        if 'events' in endpoint:
            return {'events': []}
        if 'approvals' in endpoint:
            return {'approvals': [{'id': 'b'}, {'id': 'a'}]}
        raise AssertionError(endpoint)
    window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=fetch, interval=0.01))
    try:
        spin(app, lambda: 'invalid' in window.approvals_status.text())
        assert not window.approvals_next.isEnabled()
        assert window.approvals_list.count() == 0
    finally:
        window.close()


def test_late_poll_response_cannot_replace_new_approval_page(tmp_path):
    app = QApplication.instance() or QApplication([])
    old_started, release_old = Event(), Event()
    first_count = [0]
    def fetch(_state, endpoint):
        if 'health' in endpoint:
            return {'status': 'ok'}
        if 'events' in endpoint:
            return {'events': []}
        if '/approvals?' in endpoint:
            after = parse_qs(urlsplit(endpoint).query).get('after', [''])[0]
            if after == 'a099':
                return {'approvals': [{'id': 'a100'}]}
            first_count[0] += 1
            if first_count[0] == 2:
                old_started.set()
                assert release_old.wait(4)
            return {'approvals': [{'id': f'a{i:03}'} for i in range(100)]}
        raise AssertionError(endpoint)
    window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=fetch, interval=0.01))
    try:
        spin(app, lambda: window.approvals_next.isEnabled())
        assert old_started.wait(3)
        window.approvals_next.click()
        release_old.set()
        spin(app, lambda: window.approvals_list.count() == 1 and 'a100' in window.approvals_list.item(0).text())
        assert 'page 2' in window.approvals_status.text()
    finally:
        release_old.set()
        window.close()
