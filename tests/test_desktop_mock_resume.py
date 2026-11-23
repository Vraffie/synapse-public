import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QDialogButtonBox
from PySide6.QtCore import QTimer
from synapse.desktop.__main__ import MainWindow, Poller
from synapse.desktop.mock_resume import ASSERTION, PROFILE, collect_evidence, confirm_evidence
from synapse.infrastructure.daemon_database import daemon_database

ROOT = Path(__file__).resolve().parents[1]


def snapshots():
    task = {'id': 'task', 'state': 'blocked', 'state_version': 3, 'attempt_count': 1, 'assigned_profile_id': PROFILE}
    snapshot = {'run': {'id': 'run', 'state': 'recovering', 'state_version': 4},
                'mock_config': {'fixture': 'fixed-v1', 'profile_id': PROFILE, 'provider': 'mock'},
                'tasks': [task], 'dependencies': []}
    detail = {'tasks': [{**task, 'attempts': [{'id': 'attempt', 'attempt_no': 1, 'profile_id': PROFILE, 'state': 'unknown'}]}]}
    return snapshot, detail


def spin(app, predicate, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_evidence_requires_matching_snapshots_and_fixture():
    before, details = snapshots()
    body, preview = collect_evidence('run', before, details, before)
    assert body == {'expected_run_version': 4, 'tasks': [{'task_id': 'task', 'expected_version': 3, 'unknown_attempt_id': 'attempt'}], 'assertion': ASSERTION}
    assert preview['mock_config']['fixture'] == 'fixed-v1' and preview['tasks'][0]['attempts'][0]['state'] == 'unknown'
    changed = {**before, 'run': {**before['run'], 'state_version': 5}}
    with pytest.raises(ValueError):
        collect_evidence('run', before, details, changed)
    with pytest.raises(ValueError):
        collect_evidence('run', {**before, 'mock_config': None}, details, before)
    wrong = {'tasks': [{**details['tasks'][0], 'attempts': [{**details['tasks'][0]['attempts'][0], 'state': 'running'}]}]}
    with pytest.raises(ValueError):
        collect_evidence('run', before, wrong, before)


def test_confirmation_checkbox_is_required():
    app = QApplication.instance() or QApplication([])
    snapshot, detail = snapshots()
    _, preview = collect_evidence('run', snapshot, detail, snapshot)
    observed = []
    def inspect_dialog():
        dialog = next(widget for widget in app.topLevelWidgets() if isinstance(widget, QDialog) and widget.isVisible())
        checkbox = dialog.findChild(QCheckBox)
        buttons = dialog.findChild(QDialogButtonBox)
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        observed.append((checkbox.isChecked(), ok.isEnabled()))
        checkbox.setChecked(True)
        observed.append((checkbox.isChecked(), ok.isEnabled()))
        ok.click()
    QTimer.singleShot(0, inspect_dialog)
    assert confirm_evidence(None, preview)
    assert observed == [(False, False), (True, True)]


def test_lost_response_retries_identical_confirmed_evidence(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    snapshot, details = snapshots()
    requests, inspected = [], []
    def fetch(_dir, endpoint):
        if 'health' in endpoint:
            return {'status': 'ok'}
        if 'events' in endpoint:
            return {'events': []}
        if 'approvals' in endpoint:
            return {'approvals': []}
        if '/threads?' in endpoint:
            return {'threads': []}
        if '/tasks?' in endpoint:
            return details
        return snapshot
    def send(_dir, endpoint, **kwargs):
        requests.append((endpoint, kwargs))
        if len(requests) == 1:
            raise OSError('response lost')
        return {'run_id': 'run', 'state': 'running', 'assertion': ASSERTION}
    monkeypatch.setattr('synapse.desktop.runs_view.confirm_evidence', lambda _parent, preview: inspected.append(preview) or True)
    window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=fetch, send=send, interval=0.01))
    try:
        window.run_id.setText('run')
        window._select_run()
        spin(app, lambda: window.resume_mock_button.isEnabled())
        window.resume_mock_button.click()
        spin(app, lambda: 'response lost' in window.resume_mock_status.text())
        assert len(inspected) == 1 and inspected[0]['request']['tasks'][0]['unknown_attempt_id'] == 'attempt'
        assert not window.discard_resume_button.isEnabled() and 'run' in window._resume_intents
        window.discard_resume_button.click()
        assert 'run' in window._resume_intents
        window.resume_mock_button.click()
        spin(app, lambda: 'Resume accepted' in window.resume_mock_status.text())
        assert len(requests) == 2
        assert requests[0][0] == requests[1][0] == '/api/v1/runs/run/mock-resume'
        assert requests[0][1]['idempotency_key'] == requests[1][1]['idempotency_key']
        assert requests[0][1]['body'] == requests[1][1]['body']
        assert json.loads(requests[0][1]['body']) == inspected[0]['request']
        assert len(inspected) == 1
    finally:
        window.close()


def test_changed_selection_during_confirmation_does_not_submit(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    snapshot, details = snapshots()
    calls = []
    def fetch(_dir, endpoint):
        if 'health' in endpoint: return {'status': 'ok'}
        if 'events' in endpoint: return {'events': []}
        if 'approvals' in endpoint: return {'approvals': []}
        if '/threads?' in endpoint: return {'threads': []}
        if '/tasks?' in endpoint: return details
        return snapshot
    window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=fetch, send=lambda *a, **k: calls.append(a), interval=0.01))
    def change_input(parent, _preview):
        parent.run_id.setText('different')
        return True
    monkeypatch.setattr('synapse.desktop.runs_view.confirm_evidence', change_input)
    try:
        window.run_id.setText('run')
        window._select_run()
        spin(app, lambda: window.resume_mock_button.isEnabled())
        window.resume_mock_button.click()
        spin(app, lambda: window._resume_pending is None)
        assert calls == [] and not window.resume_mock_button.isEnabled()
    finally:
        window.close()


def test_real_daemon_explicit_recovery_from_visible_fixture_evidence(tmp_path, monkeypatch):
    with daemon_database(tmp_path / 'synapse.sqlite', ROOT / 'migrations') as db:
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','recovering',4,'now','now')")
        db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) VALUES(?,'mock','Mock','mock','now')", (PROFILE,))
        db.execute("INSERT INTO mock_run_config VALUES('run',?,'fixed-v1')", (PROFILE,))
        db.execute("INSERT INTO tasks(id,run_id,node_id,state,state_version,assigned_profile_id,attempt_count,created_at,updated_at) VALUES('task','run','node','blocked',3,?,1,'now','now')", (PROFILE,))
        db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('attempt','task',1,?,'unknown')", (PROFILE,))
    proc = subprocess.Popen([sys.executable, '-m', 'synapse.daemon', '--state-dir', str(tmp_path), '--migrations', str(ROOT / 'migrations')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    app = QApplication.instance() or QApplication([])
    inspected = []
    monkeypatch.setattr('synapse.desktop.runs_view.confirm_evidence', lambda _parent, preview: inspected.append(preview) or True)
    window = None
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / 'agentd.sock').exists() and time.monotonic() < deadline:
            if proc.poll() is not None: raise AssertionError('daemon exited')
            time.sleep(0.02)
        window = MainWindow(tmp_path, poller=Poller(tmp_path, interval=0.02))
        window.run_id.setText('run')
        window._select_run()
        spin(app, lambda: window.resume_mock_button.isEnabled(), seconds=8)
        window.resume_mock_button.click()
        spin(app, lambda: 'Resume accepted' in window.resume_mock_status.text(), seconds=8)
        assert inspected[0]['mock_config'] == {'fixture': 'fixed-v1', 'profile_id': PROFILE, 'provider': 'mock'}
        assert inspected[0]['request'] == {'expected_run_version': 4, 'tasks': [{'task_id': 'task', 'expected_version': 3, 'unknown_attempt_id': 'attempt'}], 'assertion': ASSERTION}
        spin(app, lambda: 'succeeded' in window.run_status.text(), seconds=8)
    finally:
        if window is not None: window.close()
        if proc.poll() is None: proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
    assert proc.returncode == 0
    with daemon_database(tmp_path / 'synapse.sqlite', ROOT / 'migrations') as db:
        assert db.execute("SELECT state FROM task_attempts ORDER BY attempt_no").fetchall() == [('failed',), ('completed',)]
        assert db.execute("SELECT COUNT(*) FROM audit_entries WHERE action='mock.resume'").fetchone() == (1,)


def test_definite_conflict_can_be_discarded_only_explicitly_then_reinspected(tmp_path, monkeypatch):
    from synapse.cli.__main__ import DaemonHTTPError
    app = QApplication.instance() or QApplication([])
    initial, first_details = snapshots()
    state = {'snapshot': initial, 'details': first_details}
    inspected, requests = [], []
    def fetch(_dir, endpoint):
        if 'health' in endpoint: return {'status': 'ok'}
        if 'events' in endpoint: return {'events': []}
        if 'approvals' in endpoint: return {'approvals': []}
        if '/threads?' in endpoint: return {'threads': []}
        if '/tasks?' in endpoint: return state['details']
        return state['snapshot']
    def send(_dir, endpoint, **kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            raise DaemonHTTPError(409)
        return {'run_id': 'run', 'state': 'running', 'assertion': ASSERTION}
    monkeypatch.setattr('synapse.desktop.runs_view.confirm_evidence', lambda _parent, preview: inspected.append(preview) or True)
    window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=fetch, send=send, interval=0.01))
    try:
        window.run_id.setText('run')
        window._select_run()
        spin(app, lambda: window.resume_mock_button.isEnabled())
        window.resume_mock_button.click()
        spin(app, lambda: window.discard_resume_button.isEnabled())
        assert 'rejected (HTTP 409)' in window.resume_mock_status.text()
        assert not window.resume_mock_button.isEnabled()
        assert 'run' in window._resume_intents and len(inspected) == 1
        updated_task = {**initial['tasks'][0], 'state_version': 4}
        state['snapshot'] = {**initial, 'run': {**initial['run'], 'state_version': 5}, 'tasks': [updated_task]}
        state['details'] = {'tasks': [{**first_details['tasks'][0], 'state_version': 4}]}
        spin(app, lambda: window._known_run_version == 5)
        assert window.discard_resume_button.isEnabled() and 'run' in window._resume_intents
        window.discard_resume_button.click()
        assert 'run' not in window._resume_intents and not window.discard_resume_button.isEnabled()
        spin(app, lambda: window.resume_mock_button.isEnabled())
        window.resume_mock_button.click()
        spin(app, lambda: 'Resume accepted' in window.resume_mock_status.text())
        assert len(inspected) == 2 and inspected[-1]['request']['expected_run_version'] == 5
        assert requests[0]['idempotency_key'] != requests[1]['idempotency_key']
        assert requests[0]['body'] != requests[1]['body']
    finally:
        window.close()
