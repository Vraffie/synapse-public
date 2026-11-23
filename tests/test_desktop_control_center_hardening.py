"""Hardening tests for the Control Center: markup injection, malformed rows, topology rebuilds, composer, counts, gate note."""
import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['SYNAPSE_REDUCED_MOTION'] = '1'
pytest.importorskip('PySide6')
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtWidgets import QApplication, QLabel

from synapse.desktop import agent_form, control_model, narrative, prompt_composer
from synapse.desktop.control_center import ControlCenter, stamp_key

EVIL = ['<h1>x</h1>', '<img src=file:///etc/passwd>']


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def make(evil='<h1>x</h1>', node_id=None):
    center = ControlCenter(settings=QSettings(os.devnull, QSettings.Format.IniFormat))
    center.set_run_id('r1')
    center.set_snapshot('r1', {'run': {'id': 'r1', 'state': 'running'},
                               'tasks': [{'id': 't1', 'node_id': node_id or evil, 'state': 'running'}], 'dependencies': []})
    return center


def dispose(center):
    """Destroy the widget tree on the test thread, in Qt's order, instead of leaving it to the cyclic GC (a known segfault source)."""
    center.close()
    center.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def no_markup(text, payload):
    return payload not in text and '<h1>' not in text and '<img' not in text


@pytest.mark.parametrize('evil', EVIL)
def test_plain_labels_never_render_markup(app, evil):
    center = make(evil)
    center.set_approvals([{'id': 'a1', 'run_id': 'r1', 'state': 'requested', 'operation': evil, 'target': evil}])
    center.set_agent_detail({'summary_text': evil, 'worktree': {'diff_stat': evil + ' | 2 +-'}})
    center.set_connection(evil)
    center.set_messages('r1', [{'id': 'm1', 'type': evil, 'created_at': '2026-01-01T00:00:00Z', 'payload': {'task_id': 't1'}}])
    center.add_event({'sequence': 1, 'type': evil, 'run_id': 'r1', 'aggregate_id': 't1', 'occurred_at': '2026-01-01T00:00:01Z', 'payload': {}})
    center.set_runs([{'id': evil, 'state': 'running'}])
    plain = [center.gate_body, center.gate_context, center.sel_name, center.connection_pill, center.gate_title, center.gate_status]
    feed = [center.feed_box.itemAt(i).widget() for i in range(center.feed_box.count())]
    assert feed
    for frame in feed:
        plain += frame.findChildren(QLabel)
    assert center.gate_context.text() and evil in center.gate_body.text()
    for label in plain:
        assert label.textFormat() == Qt.TextFormat.PlainText, label.objectName()
    # rich labels carry the payload only escaped
    for label in (center.sel_state, center.sel_links, center.sel_summary, center.sel_changes, center.phases):
        assert label.textFormat() == Qt.TextFormat.RichText
        assert no_markup(label.text(), evil)
    assert center.sel_summary.text() and center.sel_changes.text()
    assert no_markup(center.roster_cards['t1'].findChildren(QLabel)[0].text(), evil)
    # tooltips are never raw markup
    tips = [center.roster_cards['t1'].toolTip(), center.run_combo.itemData(0, Qt.ItemDataRole.ToolTipRole),
            center.topology.node_items['t1'].toolTip()] + [frame.toolTip() for frame in feed]
    for tip in tips:
        assert tip and no_markup(tip, evil)
    dispose(center)


def test_safe_tip_escapes():
    tip = narrative.safe_tip('<h1>x</h1>')
    assert '<h1>' not in tip and '&lt;h1&gt;' in tip and narrative.safe_tip(None)


@pytest.mark.parametrize('state', [['requested'], {'a': 1}, 5, None, 'bogus', True])
def test_malformed_approval_state_is_coerced(state):
    gate = control_model.gate_for_run([{'id': 'a', 'run_id': 'r', 'state': state, 'operation': 'x', 'target': 'y'}], 'r')
    assert gate.state == 'none' and not gate.decidable
    control_model.gate_for_run([{'id': 'a', 'run_id': ['r'], 'state': state}], 'r')


def test_approvals_slot_survives_bad_rows(app, tmp_path):
    from synapse.desktop.__main__ import MainWindow, Poller
    window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=lambda *a: {}, interval=0.01))
    try:
        rows = [{'id': 'a', 'run_id': ['r'], 'state': ['requested']}, {'id': 'b', 'run_id': {'x': 1}, 'state': {'k': 1}},
                {'id': 'c', 'run_id': 7, 'state': 5}, {'id': 'd', 'run_id': 'r', 'state': 'requested'}]
        window._show_approvals(rows, window._approval_page_generation)
        assert window.approvals_list.count() == 4 and 'Approvals page' in window.approvals_status.text()
    finally:
        window.close()


def test_identical_snapshots_rebuild_topology_once(app):
    center = make(node_id='plan')
    calls = []
    original = center.topology.set_layout
    center.topology.set_layout = lambda layout: (calls.append(1), original(layout))[1]
    snap = {'run': {'id': 'r1', 'state': 'running'}, 'tasks': [{'id': 't1', 'node_id': 'plan', 'state': 'running'}], 'dependencies': []}
    center.set_snapshot('r1', snap)
    center.set_snapshot('r1', dict(snap))
    assert calls == []  # identical to the build in make(): no scene rebuild
    center.set_snapshot('r1', {**snap, 'tasks': [{'id': 't1', 'node_id': 'plan', 'state': 'completed'}]})
    assert calls == [1]
    dispose(center)


def test_composer_keeps_text_typed_while_pending(app, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_form, 'ask_confirm', lambda *a: True)
    box = prompt_composer.PromptComposer(QSettings(str(tmp_path / 'c.ini'), QSettings.Format.IniFormat))
    box.repo.setCurrentText('/work/repo')
    box.submit_requested.connect(lambda _s: None)
    box.prompt.setPlainText('first')
    box.submit()
    box.set_pending(True)
    box.prompt.setPlainText('first and more')
    box.finished('accepted', {'run_id': 'r'}, '')
    assert box.prompt.toPlainText() == 'first and more'
    box.prompt.setPlainText('second')
    box.submit()
    box.set_pending(True)
    box.finished('accepted', {'run_id': 'r'}, '')
    assert box.prompt.toPlainText() == ''
    box.close()


def test_message_count_not_capped_or_reduced_by_events(app):
    center = make()
    center.set_messages('r1', [{'id': f'm{i}', 'type': 'ack', 'created_at': f'2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z'} for i in range(450)])
    assert center.message_count == 450
    for seq in range(1, 500):
        center.add_event({'sequence': seq, 'type': 'task.state_changed', 'run_id': 'r1', 'occurred_at': '2027-01-01T00:00:00+00:00', 'payload': {}})
    center.set_messages('r1', [{'id': 'm0', 'type': 'ack'}, {'id': 'new', 'type': 'ack'}])
    assert center.message_count == 451 and center.tiles[3][1].text() == '451'
    assert len(center._feed) <= 400
    dispose(center)


def test_stamp_key_orders_by_instant_and_tolerates_junk():
    assert stamp_key('2026-01-01T10:00:00+02:00') < stamp_key('2026-01-01T09:00:00Z')  # 08:00Z is earlier, though "10" > "09"
    assert stamp_key('garbage') < stamp_key('2026-01-01T00:00:00Z') and stamp_key(None)


def test_event_without_sequence_is_skipped_or_keyed_by_id(app):
    center = make()
    center.add_event({'type': 'x', 'run_id': 'r1', 'occurred_at': '2026-01-01T00:00:00Z'})
    center.add_event({'sequence': None, 'type': 'x', 'run_id': 'r1'})
    assert not center._feed
    center.add_event({'sequence': None, 'id': 'ev-1', 'type': 'x', 'run_id': 'r1'})
    assert list(center._feed) == ['eev-1']
    dispose(center)


def test_gate_note_is_tied_to_the_approval(app):
    center = make()
    center.set_approvals([{'id': 'A', 'run_id': 'r1', 'state': 'requested', 'requested_at': 'a'}])
    center.set_gate_note('note about A')
    assert center.gate_status.text() == 'note about A'
    center.set_approvals([{'id': 'A', 'run_id': 'r1', 'state': 'requested', 'requested_at': 'a'},
                          {'id': 'B', 'run_id': 'r1', 'state': 'requested', 'requested_at': 'z'}])
    assert center.gate.row['id'] == 'B' and center.gate_status.text() == 'Waiting for you.'
    center.set_gate_note('note about B')
    center.set_run_id('r2')
    center.set_approvals([{'id': 'B', 'run_id': 'r2', 'state': 'requested'}])
    assert 'note about B' not in center.gate_status.text()
    dispose(center)
