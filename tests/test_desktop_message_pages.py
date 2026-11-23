import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from synapse.desktop.__main__ import MainWindow, Poller
from synapse.infrastructure.daemon_database import daemon_database

ROOT = Path(__file__).resolve().parents[1]


def spin(app, condition, seconds=8):
    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert condition()


def test_real_daemon_pages_and_page_selection_fencing(tmp_path):
    with daemon_database(tmp_path / "synapse.sqlite", ROOT / "migrations") as db:
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        db.execute("INSERT INTO agent_definitions VALUES('agent','1',NULL,'{}')")
        db.execute("INSERT INTO agent_instances(id,run_id,definition_id,definition_version,state,created_at) VALUES('sender','run','agent','1','running','now')")
        for i in range(101):
            db.execute("INSERT INTO threads(id,run_id,subject,created_at) VALUES(?,'run',?,'now')", (f't{i:03}', f'Thread {i}'))
        for i in range(101):
            db.execute("INSERT INTO messages(id,thread_id,sender_instance_id,type,schema_version,payload_json,created_at) VALUES(?,'t100','sender','question',1,?,'now')", (f'm{i:03}', '{"n":' + str(i) + '}'))
    proc = subprocess.Popen([sys.executable, '-m', 'synapse.daemon', '--state-dir', str(tmp_path), '--migrations', str(ROOT / 'migrations')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    app = QApplication.instance() or QApplication([])
    window = None
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / 'agentd.sock').exists() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(0.02)
        poller = Poller(tmp_path, interval=0.02)
        window = MainWindow(tmp_path, poller=poller)
        window.run_id.setText('run')
        window._select_run()
        spin(app, lambda: window.threads_list.count() == 100 and window.threads_next.isEnabled())
        old_page = window._thread_page_generation
        window.threads_next.click()
        spin(app, lambda: window.threads_list.count() == 1 and window.threads_list.item(0).data(Qt.ItemDataRole.UserRole) == 't100')
        window._show_threads('run', window._active_generation, [{'id': 'stale'}], old_page)
        assert window.threads_list.item(0).data(Qt.ItemDataRole.UserRole) == 't100'
        window.threads_list.setCurrentRow(0)
        spin(app, lambda: window.messages_list.count() == 100 and window.messages_next.isEnabled())
        old_message_page = window._message_page_generation
        selected_generation = window._thread_generation
        window.messages_next.click()
        spin(app, lambda: window.messages_list.count() == 1 and 'm100' in window.messages_list.item(0).text())
        window._show_messages('run', window._active_generation, 't100', selected_generation, [{'id': 'stale'}], old_message_page)
        assert 'm100' in window.messages_list.item(0).text()
        assert window.messages_previous.isEnabled()
        window.messages_previous.click()
        spin(app, lambda: window.messages_list.count() == 100 and 'm000' in window.messages_list.item(0).text())
        window.threads_previous.click()
        spin(app, lambda: window.threads_list.count() == 100 and window.threads_next.isEnabled())
        assert window._active_thread == '' and window.messages_list.count() == 0
    finally:
        if window is not None:
            window.close()
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)


def test_failed_next_page_allows_return_and_ignores_stale_errors(tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    app = QApplication.instance() or QApplication([])
    def fetch(_state, endpoint):
        if '/system/health' in endpoint:
            return {'status': 'ok'}
        if '/events?' in endpoint:
            return {'events': []}
        if '/approvals?' in endpoint:
            return {'approvals': []}
        if '/threads?' in endpoint:
            if 'after=100' in endpoint:
                raise OSError('unavailable')
            return {'threads': [{'id': f't{i}', 'cursor': i + 1} for i in range(100)]}
        return {'run': {'id': 'run'}, 'tasks': [], 'dependencies': []}
    poller = Poller(tmp_path, fetch=fetch, interval=0.01)
    window = MainWindow(tmp_path, poller=poller)
    try:
        window.run_id.setText('run')
        window._select_run()
        spin(app, lambda: window.threads_next.isEnabled())
        old_page = window._thread_page_generation
        window.threads_next.setFocus()
        QTest.keyClick(window.threads_next, Qt.Key.Key_Space)
        spin(app, lambda: 'unavailable' in window.threads_status.text() and window.threads_previous.isEnabled())
        window.threads_previous.click()
        spin(app, lambda: window.threads_list.count() == 100)
        window._threads_unavailable('run', window._active_generation, old_page)
        assert 'unavailable' not in window.threads_status.text()
    finally:
        window.close()


def test_malformed_pages_allow_previous_navigation(tmp_path):
    app = QApplication.instance() or QApplication([])
    def fetch(_state, endpoint):
        if '/system/health' in endpoint:
            return {'status': 'ok'}
        if '/events?' in endpoint:
            return {'events': []}
        if '/approvals?' in endpoint:
            return {'approvals': []}
        if '/threads/t0/messages' in endpoint:
            if 'after=100' in endpoint:
                return {'messages': [{'id': None}]}
            return {'messages': [{'id': f'm{i}', 'cursor': i + 1} for i in range(100)]}
        if '/threads?' in endpoint:
            if 'after=100' in endpoint:
                return {'threads': [{'id': None}]}
            return {'threads': [{'id': f't{i}', 'cursor': i + 1} for i in range(100)]}
        return {'run': {'id': 'run'}, 'tasks': [], 'dependencies': []}
    poller = Poller(tmp_path, fetch=fetch, interval=0.01)
    window = MainWindow(tmp_path, poller=poller)
    try:
        window.run_id.setText('run')
        window._select_run()
        spin(app, lambda: window.threads_next.isEnabled())
        window.threads_list.setCurrentRow(0)
        spin(app, lambda: window.messages_next.isEnabled())
        window.messages_next.click()
        spin(app, lambda: 'invalid' in window.messages_status.text() and window.messages_previous.isEnabled())
        assert 'no page loaded' in window.messages_status.text()
        window.messages_previous.click()
        spin(app, lambda: window.messages_next.isEnabled())
        window.threads_next.click()
        spin(app, lambda: 'invalid' in window.threads_status.text() and window.threads_previous.isEnabled())
        assert 'no page loaded' in window.threads_status.text()
        window.threads_previous.click()
        spin(app, lambda: window.threads_next.isEnabled())
    finally:
        window.close()
