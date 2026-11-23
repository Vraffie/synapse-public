import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel, QLineEdit
from synapse.cli.__main__ import DaemonHTTPError
from synapse.cli.__main__ import main as cli_main
from synapse.desktop import approval_decision
from synapse.desktop.__main__ import MainWindow, Poller
from synapse.infrastructure.daemon_database import daemon_database
from synapse.persistence.approval_store import write_approval_request

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'T0ken_' + 'x' * 37
DIGEST = 'b' * 64
assert len(TOKEN) == 43


def row(**changes):
    base = {'id': 'ap1', 'run_id': 'run', 'operation': 'push', 'target': 'repo', 'candidate_manifest_sha256': DIGEST,
            'policy_version': 'v1', 'state': 'requested', 'state_version': 0}
    return {**base, **changes}


def spin(app, predicate, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def all_text(app):
    texts = []
    for widget in app.allWidgets():
        for attr in ('text', 'toPlainText', 'windowTitle', 'placeholderText'):
            getter = getattr(widget, attr, None)
            if callable(getter):
                texts.append(str(getter()))
        for index in range(getattr(widget, 'count', lambda: 0)() if hasattr(widget, 'item') else 0):
            texts.append(widget.item(index).text())
    return '\n'.join(texts)


class Harness:
    def __init__(self, tmp_path, monkeypatch, send, rows=None, prompt=None):
        self.app = QApplication.instance() or QApplication([])
        self.rows = [row()] if rows is None else rows
        self.fetches = 0
        self.prompts = []
        self.sent = []

        def fetch(_dir, endpoint):
            if 'health' in endpoint:
                return {'status': 'ok'}
            if 'events' in endpoint:
                return {'events': []}
            if '/approvals?' in endpoint:
                self.fetches += 1
                return {'approvals': self.rows}
            raise AssertionError(endpoint)

        def recording(state, endpoint, **kwargs):
            self.sent.append((endpoint, kwargs))
            return send(state, endpoint, **kwargs)

        def default_prompt(_parent, decision, binding):
            self.prompts.append((decision, binding))
            return TOKEN, None
        monkeypatch.setattr('synapse.desktop.approval_decision.prompt_decision', prompt or default_prompt)
        self.window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=fetch, send=recording, interval=0.01))
        spin(self.app, lambda: self.window.approvals_list.count() == len(self.rows))
        if self.rows:
            self.window.approvals_list.setCurrentRow(0)

    def close(self):
        self.window.close()
        assert not self.window.poller.isRunning()


def approved(_state, endpoint, **kwargs):
    return {'approval_id': 'ap1', 'state': json.loads(kwargs['body'])['decision'], 'state_version': 1}


def test_dialog_masks_token_validates_and_clears():
    app = QApplication.instance() or QApplication([])
    binding = approval_decision.binding_from_row(row())
    seen = {}

    def inspect():
        dialog = next(w for w in app.topLevelWidgets() if isinstance(w, QDialog) and w.isVisible())
        ok = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
        token = dialog.findChild(QLineEdit)
        seen['mode'] = dialog.token_edit.echoMode()
        seen['summary'] = dialog.summary.text()
        seen['initial_ok'] = ok.isEnabled()
        dialog.token_edit.setText(TOKEN)
        dialog.reason_edit.setText('ok ' + TOKEN)
        seen['reason_has_token_ok'] = ok.isEnabled()
        dialog.reason_edit.setText(' fine ')
        seen['valid_ok'] = ok.isEnabled()
        assert token is not None
        seen['dialog'] = dialog
        ok.click()
    QTimer.singleShot(0, inspect)
    result = approval_decision.prompt_decision(None, 'approved', binding)
    assert result == (TOKEN, 'fine')
    assert seen['mode'] == QLineEdit.EchoMode.Password
    for needle in ('APPROVE', 'ap1', 'push', 'repo', DIGEST, 'v1'):
        assert needle in seen['summary']
    assert (seen['initial_ok'], seen['reason_has_token_ok'], seen['valid_ok']) == (False, False, True)
    assert seen['dialog'].token_edit.text() == '' and TOKEN not in seen['summary']


def test_only_wellformed_requested_rows_are_decidable():
    assert approval_decision.binding_from_row(row()) is not None
    for bad in (row(state='approved'), row(state_version=True), row(state_version=-1), row(candidate_manifest_sha256='x'),
                row(operation=''), row(id='a/b'), row(policy_version=None), 'nope'):
        assert approval_decision.binding_from_row(bad) is None


def test_approve_sends_exact_binding_and_refreshes(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    holder = {}

    def send(_state, endpoint, **kwargs):
        holder['harness'].rows = [row(state='approved', state_version=1)]
        return approved(_state, endpoint, **kwargs)
    h = holder['harness'] = Harness(tmp_path, monkeypatch, send)
    try:
        assert h.window.approve_button.isEnabled() and h.window.reject_button.isEnabled()
        before = h.fetches
        h.window.approve_button.click()
        spin(h.app, lambda: 'recorded' in h.window.approval_decision_status.text())
        endpoint, kwargs = h.sent[0]
        assert endpoint == '/api/v1/approvals/ap1/decision' and kwargs['approver_token'] == TOKEN
        assert json.loads(kwargs['body']) == {'decision': 'approved', 'expected_version': 0, 'operation': 'push', 'target': 'repo',
                                              'candidate_sha256': DIGEST, 'policy_version': 'v1', 'reason': None}
        assert kwargs['idempotency_key'] and kwargs['correlation_id'] and kwargs['idempotency_key'] != kwargs['correlation_id']
        spin(h.app, lambda: h.fetches > before and 'approved' in h.window.approvals_list.item(0).text())
        spin(h.app, lambda: not h.window.approve_button.isEnabled())
        assert not h.window.reject_button.isEnabled()
        assert TOKEN not in all_text(h.app) and TOKEN not in caplog.text
        assert TOKEN not in '\n'.join(h.window.events.item(i).text() for i in range(h.window.events.count()))
    finally:
        h.close()


def test_selection_survives_poll_refresh_and_non_requested_disables(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, approved, rows=[row(id='a'), row(id='b', state='approved')])
    try:
        h.window.approvals_list.setCurrentRow(0)
        before = h.fetches
        spin(h.app, lambda: h.fetches >= before + 3)
        assert h.window.approvals_list.currentRow() == 0 and h.window.approve_button.isEnabled()
        h.window.approvals_list.setCurrentRow(1)
        assert not h.window.approve_button.isEnabled() and not h.window.reject_button.isEnabled()
    finally:
        h.close()


@pytest.mark.parametrize('status,needle', [(401, '401'), (403, '403'), (409, '409')])
def test_error_statuses_map_to_safe_messages(tmp_path, monkeypatch, caplog, status, needle):
    caplog.set_level(logging.DEBUG)

    def send(*_a, **_k):
        raise DaemonHTTPError(status)
    h = Harness(tmp_path, monkeypatch, send)
    try:
        h.window.reject_button.click()
        spin(h.app, lambda: needle in h.window.approval_decision_status.text())
        assert json.loads(h.sent[0][1]['body'])['decision'] == 'rejected'
        assert TOKEN not in all_text(h.app) and TOKEN not in caplog.text
        assert h.window.approve_button.isEnabled()  # a definitive refusal leaves the request decidable
    finally:
        h.close()


def test_wrong_token_then_correct_token_uses_fresh_attempt(tmp_path, monkeypatch):
    tokens = ['W' * 43, TOKEN]

    def send(_s, endpoint, **kwargs):
        if kwargs['approver_token'] != TOKEN:
            raise DaemonHTTPError(403)
        return approved(_s, endpoint, **kwargs)

    def prompt(_p, decision, binding):
        return tokens.pop(0), 'because'
    h = Harness(tmp_path, monkeypatch, send, prompt=prompt)
    try:
        h.window.approve_button.click()
        spin(h.app, lambda: '403' in h.window.approval_decision_status.text())
        assert 'W' * 43 not in all_text(h.app)
        h.window.approve_button.click()
        spin(h.app, lambda: 'recorded' in h.window.approval_decision_status.text())
        first, second = h.sent[0][1], h.sent[1][1]
        assert first['idempotency_key'] != second['idempotency_key']
        assert first['correlation_id'] != second['correlation_id']
    finally:
        h.close()


def test_lost_response_retry_reuses_key_with_fresh_correlation(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    calls = []

    def send(_s, endpoint, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise OSError('response lost ' + kwargs['approver_token'])  # hostile exception text must not surface
        return approved(_s, endpoint, **kwargs)
    h = Harness(tmp_path, monkeypatch, send)
    try:
        h.window.approve_button.click()
        spin(h.app, lambda: 'same idempotency key' in h.window.approval_decision_status.text())
        assert TOKEN not in all_text(h.app) and TOKEN not in caplog.text
        h.window.approve_button.click()
        spin(h.app, lambda: 'recorded' in h.window.approval_decision_status.text())
        assert calls[0]['idempotency_key'] == calls[1]['idempotency_key']
        assert calls[0]['body'] == calls[1]['body']
        assert calls[0]['correlation_id'] != calls[1]['correlation_id']
        h.rows = [row(id='ap1', state_version=0)]  # a later, separate decision gets a new key
        h.window.approve_button.click()
        spin(h.app, lambda: len(calls) == 3)
        assert calls[2]['idempotency_key'] != calls[1]['idempotency_key']
    finally:
        h.close()


def test_ui_stays_responsive_while_request_is_in_flight(tmp_path, monkeypatch):
    started, release = Event(), Event()

    def send(_s, endpoint, **kwargs):
        started.set()
        assert release.wait(5)
        return approved(_s, endpoint, **kwargs)
    h = Harness(tmp_path, monkeypatch, send)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)
    try:
        h.window.approve_button.click()
        spin(h.app, lambda: started.is_set())
        assert not h.window.approve_button.isEnabled() and not h.window.reject_button.isEnabled()
        assert 'Submitting' in h.window.approval_decision_status.text()
        mark = len(ticks)
        spin(h.app, lambda: len(ticks) >= mark + 5)  # event loop kept running during the blocked send
        h.window.approve_button.click()  # ignored while pending
        assert len(h.sent) == 1
        release.set()
        spin(h.app, lambda: 'recorded' in h.window.approval_decision_status.text())
    finally:
        timer.stop()
        release.set()
        h.close()


def test_row_changing_during_dialog_does_not_submit(tmp_path, monkeypatch):
    holder = {}

    def prompt(_p, decision, binding):
        holder['h'].rows = [row(state_version=5)]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and holder['h'].window._approval_rows[0]['state_version'] != 5:
            holder['h'].app.processEvents()
            time.sleep(0.01)
        return TOKEN, None
    h = holder['h'] = Harness(tmp_path, monkeypatch, approved, prompt=prompt)
    try:
        h.window.approve_button.click()
        assert 'changed while the dialog' in h.window.approval_decision_status.text()
        assert h.sent == []
    finally:
        h.close()


def test_cancelled_dialog_sends_nothing(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, approved, prompt=lambda *_a: None)
    try:
        h.window.approve_button.click()
        time.sleep(0.1)
        h.app.processEvents()
        assert h.sent == []
    finally:
        h.close()


def test_gate_click_before_approvals_list_has_the_row_decides_from_the_gate_row(tmp_path, monkeypatch):
    """The gate card and the Approvals list come from separate polls; a click in the gap must still work.

    The decision binds to the approval id and candidate hash the gate showed, without waiting for the list.
    """
    h = Harness(tmp_path, monkeypatch, approved, rows=[])
    try:
        cc = h.window.control_center
        cc.set_run_id('run')
        cc.set_approvals([row()])
        assert cc.gate.state == 'pending' and cc.approve_button.isEnabled()
        assert h.window.approvals_list.count() == 0
        cc.approve_button.click()
        assert [d for d, _ in h.prompts] == ['approved']
        assert h.prompts[0][1]['approval_id'] == 'ap1' and h.prompts[0][1]['candidate_sha256'] == DIGEST
        spin(h.app, lambda: len(h.sent) == 1)
        assert 'ap1' in h.sent[0][0]
        assert 'another page' not in cc.gate_status.text()
    finally:
        h.close()


def test_gate_click_refuses_when_listed_row_differs_from_gate_row(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, approved, rows=[row(candidate_manifest_sha256='c' * 64)])
    try:
        cc = h.window.control_center
        cc.set_run_id('run')
        cc.set_approvals([row()])  # no event processing between here and the click: the list already holds the other row
        assert h.window.approvals_list.count() == 1
        cc.approve_button.click()
        assert h.prompts == [] and h.sent == []
        assert 'changed' in cc.gate_status.text()
    finally:
        h.close()


def test_gate_decision_refused_if_gate_row_goes_stale_during_dialog(tmp_path, monkeypatch):
    holder = {}

    def prompt(_p, decision, binding):
        holder['h'].window.control_center.set_approvals([row(state_version=7)])
        return TOKEN, None
    h = holder['h'] = Harness(tmp_path, monkeypatch, approved, rows=[], prompt=prompt)
    try:
        cc = h.window.control_center
        cc.set_run_id('run')
        cc.set_approvals([row()])
        cc.approve_button.click()
        assert h.sent == []
        assert 'changed while the dialog' in h.window.approval_decision_status.text()
    finally:
        h.close()


def test_gate_click_with_resolved_or_incomplete_row_is_refused_visibly(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, approved, rows=[])
    try:
        cc = h.window.control_center
        cc.set_run_id('run')
        cc.set_approvals([row(candidate_manifest_sha256='nothex')])
        assert cc.approve_button.isEnabled()
        cc.approve_button.click()
        assert h.prompts == [] and h.sent == [] and 'incomplete' in cc.gate_status.text()
    finally:
        h.close()


def test_real_daemon_wrong_token_then_approval(tmp_path, capsys):
    with daemon_database(tmp_path / 'synapse.sqlite', ROOT / 'migrations') as db:
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        db.execute('BEGIN IMMEDIATE')
        write_approval_request(db, approval_id='ap1', run_id='run', operation='push', target='repo',
                               candidate_sha256='a' * 64, policy_version='v1', expires_at=expires,
                               requester_subject='agent-1', requester_kind='agent')
        db.execute('COMMIT')
    proc = subprocess.Popen([sys.executable, '-m', 'synapse.daemon', '--state-dir', str(tmp_path),
                             '--migrations', str(ROOT / 'migrations')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    app = QApplication.instance() or QApplication([])
    window = None
    original = approval_decision.prompt_decision
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / 'agentd.sock').exists() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(0.02)
        assert cli_main(['--state-dir', str(tmp_path), 'approver-init']) == 0
        real = capsys.readouterr().out.strip().split()[-1]
        assert len(real) == 43
        wrong = 'Z' * 43
        tokens = [wrong, real]
        approval_decision.prompt_decision = lambda *_a: (tokens.pop(0), 'ship it')
        window = MainWindow(tmp_path, poller=Poller(tmp_path, interval=0.02))
        spin(app, lambda: window.approvals_list.count() == 1)
        window.approvals_list.setCurrentRow(0)
        window.approve_button.click()
        spin(app, lambda: '403' in window.approval_decision_status.text())
        assert wrong not in all_text(app) and real not in all_text(app)
        window.approve_button.click()
        spin(app, lambda: 'recorded' in window.approval_decision_status.text())
        spin(app, lambda: 'approved' in window.approvals_list.item(0).text() and not window.approve_button.isEnabled())
        assert real not in all_text(app)
    finally:
        approval_decision.prompt_decision = original
        if window is not None:
            window.close()
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=8)
    assert proc.returncode == 0
    assert real.encode() not in err and wrong.encode() not in err
