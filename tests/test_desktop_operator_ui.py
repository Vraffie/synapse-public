"""Offscreen Qt tests for the operator UI: approvals-first workflow, event timeline, status bar, shortcuts."""
import json
import os
import time

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication

from synapse.desktop import approval_decision
from synapse.desktop.connection import API_VERSION, Poller
from synapse.desktop.events_view import MAX_EVENT_ROWS
from synapse.desktop.main_window import MainWindow
from synapse.desktop.notifications import ApprovalNotifier, approval_message
from synapse.desktop.redaction import REDACTED, redact, redacted_json
from synapse.desktop.shortcuts import SHORTCUTS, ShortcutsDialog
from synapse.desktop.start_panel import StartRequest, StartRunPanel

DIGEST = 'c' * 64
TOKEN = 'T0ken_' + 'y' * 37


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


class Console:
    def __init__(self, tmp_path, monkeypatch=None, rows=None, events=None, send=None, version=None, prompt=None):
        self.app = QApplication.instance() or QApplication([])
        self.rows = list(rows or [])
        self.events = list(events or [])
        self.sent = []
        self.refreshes = 0

        def fetch(_dir, endpoint):
            if 'health' in endpoint:
                return {'status': 'ok', **({'version': version} if version else {})}
            if '/events' in endpoint:
                after = int(endpoint.split('after=')[1].split('&')[0])
                return {'events': [e for e in self.events if e['sequence'] > after]}
            if '/approvals?' in endpoint:
                self.refreshes += 1
                return {'approvals': self.rows}
            if '/threads?' in endpoint:
                return {'threads': []}
            if '/runs/' in endpoint:
                return {'run': {'id': endpoint.rsplit('/', 1)[-1], 'state': 'queued'}, 'tasks': [], 'dependencies': []}
            raise AssertionError(endpoint)

        def record(state, endpoint, **kwargs):
            self.sent.append((endpoint, kwargs))
            return send(state, endpoint, **kwargs) if send else {}
        if prompt is not None:
            monkeypatch.setattr('synapse.desktop.approval_decision.prompt_decision', prompt)
        self.window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=fetch, send=record, interval=0.01))
        self.window.notifier = ApprovalNotifier(self.window, tray=False)

    def close(self):
        self.window.close()
        assert not self.window.poller.isRunning()


def event(sequence, **fields):
    return {'sequence': sequence, 'id': f'e{sequence}', 'type': 'task.started', 'aggregate_type': 'task',
            'aggregate_id': f'agg{sequence}', 'run_id': 'run1', 'occurred_at': '2026-01-01T00:00:00Z', 'payload': {}, **fields}


# --- redaction -------------------------------------------------------------------------------------------

def test_redact_masks_credential_like_keys_at_any_depth():
    value = {'ok': 1, 'API_Token': 'a', 'nested': {'client_secret': 'b', 'list': [{'Password': 'c', 'keep': 'd'}]},
             'credentials': {'x': 1}, 'Authorization': 'Bearer zzz', 'private-key': 'k'}
    out = redact(value)
    text = json.dumps(out)
    for leaked in ('"a"', '"b"', '"c"', 'zzz', '"k"'):
        assert leaked not in text
    assert out['API_Token'] == REDACTED and out['credentials'] == REDACTED
    assert out['nested']['list'][0] == {'Password': REDACTED, 'keep': 'd'}
    assert out['ok'] == 1
    assert value['API_Token'] == 'a'  # input untouched
    assert 'truncated' in redacted_json({'blob': 'x' * 50000}) or len(redacted_json({'blob': 'x' * 50000})) < 25000


# --- events view -----------------------------------------------------------------------------------------

def test_event_filters_by_run_type_and_text_and_detail_is_redacted(tmp_path):
    events = [
        event(1, run_id='run1', type='task.started', payload={'note': 'alpha', 'api_token': TOKEN}),
        event(2, run_id='run2', type='task.finished', aggregate_id='needle-agg', payload={'secret': 'hush'}),
        event(3, run_id='run1', type='task.finished', payload={'password': 'pw', 'hidden': 'payload-only-word'}),
    ]
    c = Console(tmp_path, events=events)
    try:
        w = c.window
        spin(c.app, lambda: w.events.count() == 3)
        w.event_run_filter.setCurrentText('run1')
        assert [w.events.item(i).text().split()[0] for i in range(w.events.count())] == ['1', '3']
        w.event_type_filter.setCurrentText('task.finished')
        assert [w.events.item(i).text().split()[0] for i in range(w.events.count())] == ['3']
        assert 'hidden by filters' in w.events_status.text()
        w.event_run_filter.setCurrentIndex(0)
        w.event_type_filter.setCurrentIndex(0)
        w.event_search.setText('NEEDLE')
        assert [w.events.item(i).text().split()[0] for i in range(w.events.count())] == ['2']
        w.event_search.setText('payload-only-word')  # payload is not searchable
        assert w.events.count() == 0
        w.event_search.setText('')
        assert w.events.count() == 3
        w.events.setCurrentRow(0)
        detail = w.event_detail.toPlainText()
        assert json.loads(detail)['payload'] == {'note': 'alpha', 'api_token': REDACTED}
        assert TOKEN not in detail
        w.events.setCurrentRow(1)
        assert 'hush' not in w.event_detail.toPlainText() and REDACTED in w.event_detail.toPlainText()
        # a filter change keeps the selection when it is still visible
        w.event_type_filter.setCurrentText('task.finished')
        assert w.events.currentRow() == 0 and '"sequence": 2' in w.event_detail.toPlainText()
    finally:
        c.close()


def test_inspected_run_button_filters_to_active_run_and_buffer_bound_holds(tmp_path):
    c = Console(tmp_path, events=[event(1, run_id='run1'), event(2, run_id='run9')])
    try:
        w = c.window
        spin(c.app, lambda: w.events.count() == 2)
        w.run_id.setText('run9')
        w._select_run()
        w.event_inspected_run.click()
        assert w.event_run_filter.currentText() == 'run9' and w.events.count() == 1
        w.event_run_filter.setCurrentIndex(0)
        for n in range(3, MAX_EVENT_ROWS + 6):
            w._add_event(event(n))
        assert w.events.count() == MAX_EVENT_ROWS
        assert w.events.item(0).text().split()[0] == '6'
        assert 'older dropped' in w.events_status.text()
    finally:
        c.close()


# --- approvals-first workflow ----------------------------------------------------------------------------

def test_badge_autoselect_and_notification_for_new_request(tmp_path):
    c = Console(tmp_path, rows=[row(id='a1', operation='merge', target='main'), row(id='a2', state='approved')])
    try:
        w = c.window
        assert w.tabs.currentWidget() is w.approvals_tab
        spin(c.app, lambda: w.approvals_list.count() == 2)
        assert w.pending_count == 1
        assert w.tabs.tabText(w.tabs.indexOf(w.approvals_tab)) == 'Approvals (1)'
        assert w.console_status.pending_button.text() == 'Approvals: 1'
        assert w._selected_approval_id() == 'a1'  # newest *pending*, not the newest row
        assert w.approve_button.isEnabled()
        assert w.notifier.messages == []  # the backlog is not announced
        c.rows = c.rows + [row(id='a3', operation='deploy', target='prod', candidate_manifest_sha256='d' * 64)]
        spin(c.app, lambda: w.pending_count == 2)
        spin(c.app, lambda: w._selected_approval_id() == 'a3')
        channel, title, body = w.notifier.messages[-1]
        assert channel == 'statusbar' and title == 'Approval requested' and body == 'deploy on prod'
        assert 'deploy on prod' in w.statusBar().currentMessage()
        assert 'a3' not in body and 'd' * 64 not in w.statusBar().currentMessage()
        assert len(w.notifier.messages) == 1
        assert w.tabs.tabText(w.tabs.indexOf(w.approvals_tab)) == 'Approvals (2)'
        c.rows = [row(id='a3', state='approved')]
        spin(c.app, lambda: w.pending_count == 0)
        assert w.tabs.tabText(w.tabs.indexOf(w.approvals_tab)) == 'Approvals'
        assert len(w.notifier.messages) == 1
    finally:
        c.close()


def test_autoselect_never_moves_selection_under_an_open_dialog(tmp_path, monkeypatch):
    seen = {}
    holder = {}

    def prompt(_parent, decision, binding):
        w = holder['c'].window
        seen['before'] = w._selected_approval_id()
        holder['c'].rows = holder['c'].rows + [row(id='a9', operation='x', target='y')]
        start = time.monotonic()
        while time.monotonic() - start < 0.4:  # let the poller deliver the new request while the dialog is "open"
            holder['c'].app.processEvents()
            time.sleep(0.01)
        seen['during'] = w._selected_approval_id()
        return None

    c = holder['c'] = Console(tmp_path, monkeypatch, rows=[row(id='a1')], prompt=prompt)
    try:
        w = c.window
        spin(c.app, lambda: w._selected_approval_id() == 'a1')
        w.approve_button.click()
        assert seen == {'before': 'a1', 'during': 'a1'}
        spin(c.app, lambda: w.pending_count == 2)
        assert w._selected_approval_id() == 'a1'  # never yanked away; the request was still announced
        assert w.notifier.messages and w.notifier.messages[-1][2] == 'x on y'
    finally:
        c.close()


def test_notification_uses_tray_when_available_and_limits_fields():
    app = QApplication.instance() or QApplication([])
    from PySide6.QtWidgets import QMainWindow
    window = QMainWindow()
    notifier = ApprovalNotifier(window, tray=False)
    notifier.tray = type('Tray', (), {'isVisible': lambda s: True, 'showMessage': lambda s, *a: s.calls.append(a), 'calls': []})()
    long_row = row(operation='op\n' + 'o' * 500, target='t' * 500)
    notifier.notify([long_row])
    title, body, _icon, _ms = notifier.tray.calls[0]
    assert title == 'Approval requested' and '\n' not in body and len(body) < 200
    assert notifier.messages[0][0] == 'tray'
    assert approval_message([row(), row(operation='b')])[0] == '2 approvals requested'
    assert app is not None


# --- status bar ------------------------------------------------------------------------------------------

def test_status_bar_shows_live_signals_and_keeps_static_facts_in_diagnostics(tmp_path):
    c = Console(tmp_path, version='9.9.9')
    try:
        w = c.window
        s = w.console_status
        spin(c.app, lambda: s.connection.text() == 'Connected')
        assert s.health.text() == 'Health: OK'
        assert s.retry.isHidden()
        # socket path and versions are not permanent fields any more; they are in the tooltip and diagnostics
        assert str(tmp_path / 'agentd.sock') in s.connection.toolTip()
        assert f'API: {API_VERSION} (daemon 9.9.9)' in s.connection.toolTip()
        assert str(tmp_path / 'agentd.sock') in s.diagnostics_text()
        assert 'token' not in s.diagnostics_text().lower()
        w._connection_changed('reconnecting', 1, 4.0)
        assert s.connection.text() == 'Reconnecting (4s)…'
        assert not s.retry.isHidden()
        assert s.health.text() == 'Health: unreachable'
        w._connection_changed('reconnecting', 5, 15.0)
        assert s.connection.text() == 'Daemon offline'
        w._connection_changed('connected', 0, 0.0)
        assert s.retry.isHidden()
    finally:
        c.close()


def test_status_bar_retry_and_approvals_are_clickable(tmp_path):
    c = Console(tmp_path)
    try:
        w = c.window
        from PySide6.QtTest import QSignalSpy
        asked = QSignalSpy(w.console_status.retry_requested)
        w.console_status.retry.click()
        w.console_status.pending_button.click()
        assert asked.count() == 1
        assert w.tabs.currentWidget() is w.approvals_tab
        assert w.console_status.pending_button.accessibleName() == 'Pending approvals'
    finally:
        c.close()


def test_status_bar_reports_outage(tmp_path):
    app = QApplication.instance() or QApplication([])

    def fetch(_dir, endpoint):
        raise OSError('down')
    poller = Poller(tmp_path, fetch=fetch, interval=0.01)
    poller.max_backoff = 0.05
    window = MainWindow(tmp_path, poller=poller)
    try:
        spin(app, lambda: window.console_status.connection.text() != 'Connecting…' and window.console_status.connection.text() != 'Connected')
        assert window.console_status.health.text() == 'Health: unreachable'
    finally:
        window.close()


# --- shortcuts -------------------------------------------------------------------------------------------

def test_shortcuts_switch_tabs_and_focus_search(tmp_path):
    c = Console(tmp_path)
    try:
        w = c.window
        keys = {action_id: key for action_id, key, _ in SHORTCUTS}
        for action_id, tab in (('tab_runs', w.runs_tab), ('tab_events', w.events_tab),
                               ('tab_messages', w.messages_tab), ('tab_approvals', w.approvals_tab)):
            assert w.shortcut_actions[action_id].shortcut().toString() == keys[action_id]
            w.shortcut_actions[action_id].trigger()
            assert w.tabs.currentWidget() is tab
        w.shortcut_actions['find_events'].trigger()
        assert w.tabs.currentWidget() is w.events_tab
        assert len({k for k in keys.values()}) == len(keys)  # no duplicate key sequences
    finally:
        c.close()


def test_shortcut_refresh_wakes_poller_and_reloads_approvals(tmp_path):
    c = Console(tmp_path, rows=[row()])
    try:
        w = c.window
        w.poller.interval = 30  # a normal tick would take 30 s
        spin(c.app, lambda: c.refreshes >= 1)
        time.sleep(0.2)
        before = c.refreshes
        w.shortcut_actions['refresh'].trigger()
        spin(c.app, lambda: c.refreshes > before, seconds=3)
        assert w.shortcut_actions['refresh'].shortcut().toString() == 'F5'
    finally:
        c.close()


def test_approve_and_reject_shortcuts_open_the_existing_dialog_without_leaking_token(tmp_path, monkeypatch):
    prompts = []

    def prompt(_parent, decision, binding):
        prompts.append((decision, binding['approval_id']))
        return None
    c = Console(tmp_path, monkeypatch, rows=[row(id='a1')], prompt=prompt)
    try:
        w = c.window
        w.tabs.setCurrentWidget(w.events_tab)
        spin(c.app, lambda: w._selected_approval_id() == 'a1')
        w.shortcut_actions['approve'].trigger()
        w.shortcut_actions['reject'].trigger()
        assert prompts == [('approved', 'a1'), ('rejected', 'a1')]
        assert w.tabs.currentWidget() is w.approvals_tab
        assert c.sent == []
        c.rows = []
        spin(c.app, lambda: w.pending_count == 0)
        w._approval_rows = []
        w.approvals_list.clear()
        w.shortcut_actions['approve'].trigger()
        assert prompts == [('approved', 'a1'), ('rejected', 'a1')]
        assert 'Select a requested approval' in w.approval_decision_status.text()
    finally:
        c.close()


def test_help_shortcuts_dialog_lists_every_shortcut(tmp_path):
    c = Console(tmp_path)
    try:
        dialog = ShortcutsDialog()
        rows = {dialog.table.item(r, 0).text(): dialog.table.item(r, 1).text() for r in range(dialog.table.rowCount())}
        assert rows == {key: description for _id, key, description in SHORTCUTS}
        help_menu = next(a for a in c.window.menuBar().actions() if a.text() == '&Help').menu()
        assert [a.text() for a in help_menu.actions()] == ['&Shortcuts']
        assert c.window.shortcut_actions['shortcuts'].shortcut().toString() == 'F1'
    finally:
        c.close()


# --- start run panel -------------------------------------------------------------------------------------

def test_start_panel_emits_requests_only_when_enabled_and_has_a_target():
    app = QApplication.instance() or QApplication([])
    panel = StartRunPanel()
    got = []
    panel.start_requested.connect(got.append)
    panel.button.click()  # disabled
    panel.set_target('r1')
    panel.button.click()  # still disabled
    assert got == [] and panel.build_request() == StartRequest('r1', 'mock-fixture')
    panel.set_state(True, 'ready')
    panel.button.click()
    assert got == [StartRequest('r1')] and panel.status.text() == 'ready'
    assert app is not None


def test_start_panel_in_window_only_calls_the_mock_start_endpoint(tmp_path):
    def send(_state, endpoint, **kwargs):
        return {'run_id': 'run1', 'fixture': 'fixed-v1', 'state': 'running'}
    c = Console(tmp_path, send=send)
    try:
        w = c.window
        assert w.start_panel.button is w.mock_button and w.start_panel.isAncestorOf(w.mock_button)
        w.run_id.setText('run1')
        w._select_run()
        spin(c.app, lambda: w.mock_button.isEnabled())
        w.mock_button.click()
        spin(c.app, lambda: 'accepted' in w.start_panel.status.text())
        assert [endpoint for endpoint, _ in c.sent] == ['/api/v1/runs/run1/mock-start']
        assert not w.mock_button.isEnabled()
    finally:
        c.close()


def test_decision_dialog_still_the_existing_one():
    assert callable(approval_decision.prompt_decision)
