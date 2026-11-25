"""Offscreen tests for the Control Center: narrative, layout, metrics, gates, selection and the prompt composer."""
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['SYNAPSE_REDUCED_MOTION'] = '1'
pytest.importorskip('PySide6')
from PySide6.QtCore import QMimeData, QSettings, Qt, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from synapse.desktop import agent_form, control_model, narrative, prompt_composer
from synapse.desktop.control_center import ControlCenter
from synapse.desktop.topology_view import TopologyView

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import render_control_center as fixture_module  # noqa: E402


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def tasks(spec):
    return ([{'id': f't-{n}', 'node_id': n, 'state': s} for n, s, _ in spec],
            [{'task_id': f't-{n}', 'depends_on_id': f't-{d}'} for n, _s, ds in spec for d in ds])


# ---- narrative -------------------------------------------------------------------------------------
def test_narrative_humanizes_and_falls_back_safely():
    assert narrative.humanize_node('plan-scope') == 'Plan Scope'
    assert narrative.humanize_node('edit_config') == 'Edit Config'
    for junk in (None, 5, {}, [], ''):
        assert isinstance(narrative.humanize_node(junk), str)
    for event in (None, 'x', {}, {'type': 5}, {'type': 'weird.unknown', 'payload': 'no'}):
        assert isinstance(narrative.narrate_event(event), str)
        assert isinstance(narrative.narrate_message(event), str)
    text = narrative.narrate_event({'type': 'task.state_changed', 'aggregate_id': 't-a', 'payload': {'after': 'failed'}}, {'t-a': 'Run Lens'})
    assert text.startswith('Run Lens ') and '\x00' not in text
    assert narrative.clean('a\x00b\x1bc' + 'x' * 500).isprintable() and len(narrative.clean('x' * 500)) <= 121


# ---- layout ----------------------------------------------------------------------------------------
def test_layering_follows_dependencies():
    t, d = tasks([('a', 'completed', []), ('b', 'running', ['a']), ('c', 'running', ['a']), ('d', 'waiting_dependencies', ['b', 'c'])])
    layout = control_model.build_layout(t, d)
    ys = {i: n.y for i, n in layout.nodes.items()}
    assert ys['t-a'] < ys['t-b'] == ys['t-c'] < ys['t-d']
    kinds = {(e.source, e.target): e.kind for e in layout.edges}
    assert kinds[('t-a', 't-b')] == 'parallel' and kinds[('t-b', 't-d')] == 'dependency'
    assert not any(e.kind == 'feedback' for e in layout.edges)
    assert len(layout.phases) == 3


def test_feedback_edge_only_when_changes_required():
    t, d = tasks([('a', 'completed', []), ('r', 'changes_required', ['a'])])
    assert any(e.kind == 'feedback' for e in control_model.build_layout(t, d).edges)


def test_empty_single_and_many_nodes(app):
    view = TopologyView()
    view.resize(600, 400)
    view.show()
    assert not control_model.build_layout([], []).nodes
    view.set_layout(control_model.build_layout([], []))
    assert view.node_items == {}
    t, d = tasks([('only', 'running', [])])
    view.set_layout(control_model.build_layout(t, d))
    assert list(view.node_items) == ['t-only']
    spec = [(f'n{i}', 'running' if i % 3 == 0 else 'completed', [f'n{i - 1}'] if i % 4 else []) for i in range(20)]
    t, d = tasks(spec)
    layout = control_model.build_layout(t, d)
    view.set_layout(layout)
    assert len(view.node_items) == 20 and layout.hidden == 0
    assert not view.animating()  # reduced motion (set in the environment) keeps the timer off
    view.close()


def test_animation_only_when_visible_and_motion_allowed(app):
    t, d = tasks([('a', 'completed', []), ('b', 'running', ['a'])])
    view = TopologyView()
    view.motion = True
    view.set_layout(control_model.build_layout(t, d))
    assert not view.animating()  # hidden
    view.show()
    view.set_layout(control_model.build_layout(t, d))
    assert view.animating()
    view.hide()
    assert not view.animating()
    view.close()


def test_click_selects_node(app):
    center = fixture_module.build()
    center.show()
    got = []
    center.node_selected.connect(got.append)
    center.topology.node_clicked.emit('t-edit-config')
    app.processEvents()
    assert center.selected == 't-edit-config' and 'Edit Config' in center.sel_name.text()
    center.select('t-run-lens')
    assert center.selected == 't-run-lens'
    center.close()


# ---- metrics and gates ----------------------------------------------------------------------------
def test_metrics_use_real_counts():
    run, ts, deps, approvals, messages, _e, _d = fixture_module.fixture()
    layout = control_model.build_layout(ts, deps)
    tiles = dict((t, (v, c)) for t, v, c in control_model.metrics(run, layout, len(messages), approvals))
    assert tiles['Agents working'][0] == '2 / 8'
    assert tiles['Checks passed'][0] == '0 / 4'
    assert tiles['Messages exchanged'][0] == '4'
    assert tiles['Approval gate'][0] == 'Waiting for you'
    plain = control_model.build_layout(*tasks([('x', 'completed', []), ('y', 'running', ['x'])]))
    titles = [t for t, _v, _c in control_model.metrics({'id': 'r', 'state': 'running'}, plain, 0, [])]
    assert 'Steps done' in titles and 'Checks passed' not in titles
    assert control_model.metrics(None, control_model.GraphLayout(), 0, [])[0][1] == 'No run'


@pytest.mark.parametrize('row_state,gate,decidable', [(None, 'none', False), ('requested', 'pending', True), ('approved', 'approved', False),
                                                       ('rejected', 'rejected', False), ('expired', 'expired', False)])
def test_gate_states(row_state, gate, decidable):
    rows = [] if row_state is None else [{'id': 'a', 'run_id': 'r', 'operation': 'deploy', 'target': 'x', 'state': row_state}]
    result = control_model.gate_for_run(rows, 'r')
    assert result.state == gate and result.decidable is decidable
    assert control_model.gate_for_run(rows, 'other').state == 'none'


def test_gate_panel_buttons_follow_state(app):
    center = fixture_module.build()
    center.show()
    assert center.gate.state == 'pending' and center.approve_button.isEnabled() and center.reject_button.isEnabled()
    center.set_approvals([{'id': 'ap-1', 'run_id': 'run-1', 'operation': 'deploy', 'target': 't', 'state': 'approved'}])
    assert center.gate.state == 'approved' and not center.approve_button.isEnabled()
    center.close()


def test_empty_state_text(app):
    center = ControlCenter(settings=QSettings(os.devnull, QSettings.Format.IniFormat))
    assert center.topology.empty_text == 'No runs yet — start one'
    assert not center.topology.node_items
    center.close()


# ---- composer -------------------------------------------------------------------------------------
@pytest.fixture
def composer(app, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_form, 'START_DELAY_MS', 0)
    box = prompt_composer.PromptComposer(QSettings(str(tmp_path / 'c.ini'), QSettings.Format.IniFormat))
    box.sent = []
    box.submit_requested.connect(box.sent.append)
    box.set_profiles(None)  # the daemon explicitly lists no accounts: the default account, so Start is not gated
    box.repo.setCurrentText('/work/repo')
    box.show()
    yield box
    box.close()


def type_text(widget, text):
    """Type like a user: QTest.keyClicks for ASCII (QTest only maps those), real key events for everything else."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    for ch in text:
        if ord(ch) < 128:
            QTest.keyClicks(widget, ch)
        else:
            for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
                QApplication.sendEvent(widget, QKeyEvent(kind, 0, Qt.KeyboardModifier.NoModifier, ch))


@pytest.mark.parametrize('text', ['Rett feilen i æøå ünï ß', 'Fix — 日本語 ✓ ünï'])
def test_typed_equals_pasted_including_multibyte(composer, text):
    composer.prompt.setFocus()
    type_text(composer.prompt, text)
    typed = composer.prompt.toPlainText()
    composer.prompt.clear()
    mime = QMimeData()
    mime.setText(text)
    composer.prompt.insertFromMimeData(mime)
    assert composer.prompt.toPlainText() == typed == text
    assert composer.counts() == (len(typed), len(typed.encode('utf-8')))
    composer.prompt.clear()
    emoji = QMimeData()
    emoji.setText('🚀 ok')
    composer.prompt.insertFromMimeData(emoji)
    assert composer.counts() == (4, 7)


def test_enter_sends_shift_enter_is_newline(composer):
    from PySide6.QtCore import Qt
    composer.prompt.setFocus()
    QTest.keyClicks(composer.prompt, 'one')
    QTest.keyClick(composer.prompt, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClicks(composer.prompt, 'two')
    assert composer.prompt.toPlainText() == 'one\ntwo' and not composer.sent
    QTest.keyClick(composer.prompt, Qt.Key.Key_Return)  # Enter sends directly: no confirmation dialog
    assert len(composer.sent) == 1
    composer.finished('lost')
    QTest.keyClick(composer.prompt, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)  # Ctrl+Enter sends too
    assert len(composer.sent) == 2
    assert 'Enter sends' in composer.hint.text() and 'Shift+Enter' in composer.hint.text()


def test_paste_strips_rich_text(composer):
    mime = QMimeData()
    mime.setHtml('<b style="color:red">bold</b> <i>text</i>')
    composer.prompt.insertFromMimeData(mime)
    assert composer.prompt.toPlainText() == 'bold text'


def test_paste_button_uses_clipboard(composer):
    QGuiApplication.clipboard().setText('from clipboard\r\nline')
    composer.paste_button.click()
    assert composer.prompt.toPlainText() == 'from clipboard\nline'


def test_limits_disable_send(composer):
    composer.prompt.setPlainText('a' * 100)
    assert composer.submit_button.isEnabled()
    composer.prompt.setPlainText('é' * 17000)  # under 20000 chars but over 32768 bytes
    assert composer.over_limit() and not composer.submit_button.isEnabled()
    assert 'too long' in composer.counter.text()
    composer.prompt.setPlainText('')
    assert not composer.submit_button.isEnabled()


def test_huge_paste_is_guarded(composer):
    mime = QMimeData()
    mime.setText('x' * (prompt_composer.PASTE_CAP + 5000))
    composer.prompt.insertFromMimeData(mime)
    assert len(composer.prompt.toPlainText()) == prompt_composer.PASTE_CAP
    assert 'very large' in composer.notice_label.text() and not composer.submit_button.isEnabled()


def test_nul_paste_refused(composer):
    mime = QMimeData()
    mime.setText('a\x00b')
    composer.prompt.insertFromMimeData(mime)
    assert composer.prompt.toPlainText() == '' and 'NUL' in composer.notice_label.text()


def test_file_drop(composer, tmp_path):
    good = tmp_path / 'p.txt'
    good.write_text('hei æøå', encoding='utf-8')
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(good))])
    composer.prompt.insertFromMimeData(mime)
    assert composer.prompt.toPlainText() == 'hei æøå'
    composer.prompt.clear()
    for name, data in (('b.bin', b'\x00\x01\x02binary'), ('l.txt', b'\xff\xfe\xfa bad utf8')):
        path = tmp_path / name
        path.write_bytes(data)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        composer.prompt.insertFromMimeData(mime)
        assert composer.prompt.toPlainText() == '' and composer.notice_label.text()
    big = tmp_path / 'big.txt'
    big.write_text('y' * (prompt_composer.FILE_CAP + 100))
    text, note = prompt_composer.read_text_file(str(big))
    assert len(text) == prompt_composer.FILE_CAP and 'larger' in note


def test_cancelled_start_sends_nothing_and_retry_is_idempotent(composer, monkeypatch):
    composer.prompt.setPlainText('do the thing')
    monkeypatch.setattr(agent_form, 'START_DELAY_MS', 4000)
    composer.submit_button.click()
    assert composer._counting and not composer.sent and not composer.submit_button.isEnabled()
    composer.cancel_start_button.click()
    assert not composer.sent and not composer._counting and 'nothing was sent' in composer.status.text()
    monkeypatch.setattr(agent_form, 'START_DELAY_MS', 0)
    composer.submit_button.click()
    composer.finished('lost')  # response lost: the prompt stays and the key is reused
    assert composer.prompt.toPlainText() == 'do the thing'
    composer.submit_button.click()
    first, second = composer.sent
    assert first.key == second.key and first.body == second.body
    composer.prompt.setPlainText('different')
    composer.submit_button.click()
    assert composer.sent[-1].key != first.key


def test_accepted_clears_prompt_and_never_echoes_it(composer):
    secret = 'SECRET-PROMPT-TEXT'
    composer.prompt.setPlainText(secret)
    composer.submit_button.click()
    composer.finished('accepted', {'run_id': 'run-9', 'state': 'queued'}, '/work/repo')
    assert composer.prompt.toPlainText() == ''
    for label in (composer.status, composer.notice_label, composer.problems, composer.counter):
        assert secret not in label.text()


def test_safe_defaults(composer):
    fields = composer.fields()
    assert fields['permission_mode'] == 'plan' and fields['approval_step'] is None
    assert not composer.options.isVisible() or composer.options_toggle.isChecked()


def _feed_poller(fetch):
    from synapse.desktop.connection import Poller
    return Poller(Path('.'), fetch=fetch)


def test_feed_pages_forward_with_a_per_thread_cursor():
    calls, got = [], []

    def fetch(_dir, path):
        calls.append(path)
        after = int(path.split('after=')[1].split('&')[0])
        return {'messages': [{'id': f'm{n}', 'cursor': n} for n in range(after + 1, min(after + 50, 120) + 1)]}

    poller = _feed_poller(fetch)
    poller.run_feed.connect(lambda run, gen, msgs: got.extend(m['cursor'] for m in msgs))
    for _ in range(4):
        poller._poll_control_center('run-1', 0, [{'id': 'th-1'}])
    assert got == list(range(1, 121))  # newest messages arrive too, none repeated
    assert any('after=100' in c for c in calls)


def test_feed_visits_every_thread_round_robin_and_resets_per_run():
    seen = []

    def fetch(_dir, path):
        if '/messages' in path:
            seen.append(path.split('/threads/')[1].split('/')[0])
        return {'messages': [], 'runs': []}

    poller = _feed_poller(fetch)
    threads = [{'id': f'th-{i}'} for i in range(12)]
    poller._poll_control_center('run-1', 0, threads)
    poller._poll_control_center('run-1', 0, threads)
    assert set(seen) == {f'th-{i}' for i in range(12)}


def test_control_center_approvals_are_fetched_independently_of_developer_paging():
    rows = []

    def fetch(_dir, path):
        if '/approvals' in path:
            return {'approvals': [{'id': 'ap-9', 'run_id': 'run-1', 'state': 'requested'}]}
        return {}

    poller = _feed_poller(fetch)
    poller.cc_approvals.connect(rows.extend)
    poller._poll_control_center('', 0, [])
    assert [r['id'] for r in rows] == ['ap-9']


# ---- roster click handler and garbage collection ---------------------------------------------------
def test_roster_card_click_selects(app):
    center = fixture_module.build()
    center.show()
    card = center.roster_cards['t-edit-config']
    QTest.mouseClick(card, Qt.MouseButton.LeftButton)
    assert center.selected == 't-edit-config'
    center.close()


def test_dropped_control_center_survives_gc_on_another_thread(app):
    import gc
    import threading
    import weakref
    center = fixture_module.build()  # snapshot + feed rows, no explicit teardown
    center.show()
    ref = weakref.ref(center)
    del center
    errors = []

    def collect():
        try:
            gc.collect()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)
    worker = threading.Thread(target=collect)
    worker.start()
    worker.join()
    assert not errors
    gc.collect()
    assert ref() is None
