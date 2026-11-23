import json
import os
import time

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from synapse.cli.__main__ import DaemonHTTPError
from synapse.desktop import agent_form, agent_view, approval_decision, runs_view
from synapse.desktop.__main__ import MainWindow, Poller

TOKEN = 'T' * 43


def spin(app, predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert predicate()


def good(**over):
    fields = {'repo': '/work/repo', 'base_ref': 'main', 'prompt': 'fix the bug', 'allowed_tools': ['Read', 'Grep'],
              'permission_mode': 'plan', 'timeout_seconds': 600, 'approval_step': None}
    fields.update(over)
    return fields


class Console:
    def __init__(self, tmp_path, monkeypatch, send_script=None, agent=None, run_state='running', approvals=(), usage=None):
        self.app = QApplication.instance() or QApplication([])
        self.sent = []
        self.confirms = []
        self.answer = True
        self.script = list(send_script or [])
        self.agent = agent
        self.usage = usage
        monkeypatch.setattr(agent_form, 'ask_confirm', lambda parent, title, text: self.confirms.append((title, text)) or self.answer)

        def fetch(_state, endpoint):
            if 'health' in endpoint:
                return {'status': 'ok'}
            if 'events' in endpoint:
                return {'events': []}
            if 'approvals' in endpoint:
                return {'approvals': list(approvals)}
            if '/threads' in endpoint:
                return {'threads': []}
            if endpoint.endswith('/usage'):
                if isinstance(self.usage, int):
                    raise DaemonHTTPError(self.usage)
                return self.usage
            if endpoint.endswith('/agent'):
                if isinstance(self.agent, int):
                    raise DaemonHTTPError(self.agent)
                return self.agent
            return {'run': {'id': endpoint.rsplit('/', 1)[-1], 'state': run_state, 'state_version': 1}, 'tasks': [], 'dependencies': []}

        def send(_state, endpoint, **kwargs):
            self.sent.append((endpoint, kwargs))
            step = self.script.pop(0) if self.script else {'run_id': 'run-1', 'task_id': 't', 'state': 'queued', 'worktree_path': '/w'}
            if isinstance(step, Exception):
                raise step
            return step

        self.settings = QSettings(str(tmp_path / 'ui.ini'), QSettings.Format.IniFormat)
        poller = Poller(tmp_path, fetch=fetch, send=send, interval=0.01)
        self.window = MainWindow(tmp_path, poller=poller)
        self.window.start_panel.agent_form.settings = self.settings
        self.form = self.window.start_panel.agent_form

    def fill(self, **over):
        f = good(**over)
        self.form.repo.setCurrentText(f['repo'])
        self.form.base_ref.setText(f['base_ref'])
        self.form.prompt.setPlainText(f['prompt'])

    def close(self):
        self.window.close()


@pytest.fixture
def console(tmp_path, monkeypatch):
    made = []

    def make(**kw):
        c = Console(tmp_path, monkeypatch, **kw)
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


def test_validation_accepts_good_and_rejects_bad_fields():
    payload, problems = agent_form.validate(good())
    assert problems == [] and payload['allowed_tools'] == ['Read', 'Grep'] and payload['approval_step'] is None
    for bad in ({'repo': ' '}, {'repo': 'https://u:p@host/r.git'}, {'repo': 'a\nb'}, {'base_ref': '-x'}, {'base_ref': 'a b'},
                {'base_ref': 'a..b'}, {'prompt': '  '}, {'prompt': 'x' * 20001}, {'allowed_tools': []},
                {'permission_mode': 'bypassPermissions'}, {'permission_mode': 'auto'}, {'timeout_seconds': 0},
                {'timeout_seconds': 10 ** 9}, {'timeout_seconds': True},
                {'approval_step': {'operation': '', 'target': 't', 'ttl_seconds': 60}},
                {'approval_step': {'operation': 'o', 'target': 't' * 129, 'ttl_seconds': 60}},
                {'timeout_seconds': 3601}, {'approval_step': {'operation': 'o', 'target': 't', 'ttl_seconds': 0}}):
        assert agent_form.validate(good(**bad))[0] is None, bad


def step(**over):
    base = {'operation': 'agent_run.start', 'target': 'repo:abc', 'ttl_seconds': 60}
    base.update(over)
    return {'approval_step': base}


def test_timeout_bounds_match_the_daemon():
    for ok in (1, 10, 3600):
        assert agent_form.validate(good(timeout_seconds=ok))[1] == [], ok
    for bad in (0, -1, 3601, 86400):
        assert agent_form.validate(good(timeout_seconds=bad))[0] is None, bad
    assert (agent_form.MIN_TIMEOUT, agent_form.MAX_TIMEOUT) == (1, 3600)


def test_prompt_bounds_are_characters_and_utf8_bytes():
    assert agent_form.validate(good(prompt='x' * 20000))[1] == []
    assert agent_form.validate(good(prompt='x' * 20001))[0] is None
    assert len('\u00e9' * 16384) <= 20000 and len(('\u00e9' * 16384).encode()) == 32768
    assert agent_form.validate(good(prompt='\u00e9' * 16384))[1] == []
    assert agent_form.validate(good(prompt='\u00e9' * 16384 + 'x'))[0] is None  # 32769 bytes, 16385 chars
    assert agent_form.validate(good(prompt='a\x00b'))[0] is None
    assert agent_form.validate(good(prompt='\ud800'))[0] is None


def test_approval_ttl_bounds():
    for ok in (1, 86400):
        assert agent_form.validate(good(**step(ttl_seconds=ok)))[1] == [], ok
    for bad in (0, 86401, True, 1.5):
        assert agent_form.validate(good(**step(ttl_seconds=bad)))[0] is None, bad


def test_approval_operation_and_target_charset_and_length():
    for field in ('operation', 'target'):
        for ok in ('a', 'a' * 128, 'A.b_c:d/e@f+g-h'):
            assert agent_form.validate(good(**step(**{field: ok})))[1] == [], (field, ok)
        for bad in ('', 'a' * 129, 'has space', 'a#b', 'a\nb', '\u00e9', 'a,b'):
            assert agent_form.validate(good(**step(**{field: bad})))[0] is None, (field, bad)


def test_default_approval_target_is_charset_safe():
    for repo in ('/work/my repo/', 'https://host/org/re po.git', '\u00e9\u00e8', '', '/' + 'r' * 500):
        target = agent_form.default_approval_step(repo, 'p')['target']
        assert agent_form.validate(good(repo='/x', **step(target=target)))[1] == [], (repo, target)
        assert len(target) <= 128 and target.endswith(':' + agent_form.prompt_digest('p')[:16])
    assert agent_form.default_approval_step('/work/repo', 'p')['target'].startswith('repo:')


def test_base_ref_matches_daemon_grammar():
    for bad in ('/x', '.x', 'a//b', 'a/', 'a.', 'a.lock'):
        assert agent_form.validate(good(base_ref=bad))[0] is None, bad
    assert agent_form.validate(good(base_ref='feature/x-1.2'))[1] == []


@pytest.mark.parametrize('status,expected', [(409, 'a run is already active'), (422, "exceeds the daemon's limit")])
def test_start_refusals_are_distinct(console, status, expected):
    c = console(send_script=[DaemonHTTPError(status)])
    c.fill()
    c.form.submit()
    spin(c.app, lambda: expected in c.form.status.text())
    assert c.form.status.text() != agent_form.UNAVAILABLE_TEXT
    if status == 422:
        assert "not allowed by the daemon's operator config" in c.form.status.text()


def test_form_defaults_are_safe(console):
    c = console()
    form = c.form
    assert [t for t, b in form.tool_boxes.items() if b.isChecked()] == ['Read', 'Glob', 'Grep']
    modes = [form.mode.itemText(i) for i in range(form.mode.count())]
    assert set(modes) == {'plan', 'acceptEdits', 'dontAsk', 'manual'} and form.mode.currentText() == 'plan'
    assert not form.require_approval.isChecked() and not form.approval_target.isEnabled()
    form.submit()
    assert 'Repository is required' in form.problems.text() and c.sent == [] and c.confirms == []


def test_submission_payload_headers_and_recent_repos(console, tmp_path):
    c = console()
    c.fill()
    c.form.require_approval.setChecked(True)
    assert c.form.approval_operation.text() == 'agent_run.start'
    assert c.form.approval_target.text().startswith('repo:') and ' ' not in c.form.approval_target.text()
    c.form.tool_boxes['Edit'].setChecked(True)
    c.form.submit()
    assert 'WARNING' in c.confirms[0][1] and 'Approval required' in c.confirms[0][1]
    spin(c.app, lambda: c.sent)
    endpoint, kw = c.sent[0]
    assert endpoint == '/api/v1/agent-runs'
    body = json.loads(kw['body'])
    assert body['repo'] == '/work/repo' and body['base_ref'] == 'main' and body['prompt'] == 'fix the bug'
    assert body['allowed_tools'] == ['Read', 'Glob', 'Grep', 'Edit'] and body['permission_mode'] == 'plan'
    assert body['timeout_seconds'] == 900
    assert set(body['approval_step']) == {'operation', 'target', 'ttl_seconds'}
    assert kw['idempotency_key'] and kw['correlation_id'] and kw['idempotency_key'] != kw['correlation_id']
    assert 'token' not in json.dumps({k: v for k, v in kw.items() if k != 'body'}).lower()
    spin(c.app, lambda: 'run-1' in c.form.status.text())
    assert c.window.run_id.text() == 'run-1'
    assert agent_form.load_recent(c.settings) == ['/work/repo']
    assert 'fix the bug' not in open(tmp_path / 'ui.ini').read()


def test_declined_confirmation_sends_nothing(console):
    c = console()
    c.fill()
    c.answer = False
    c.form.submit()
    time.sleep(0.1)
    c.app.processEvents()
    assert c.sent == [] and 'nothing was sent' in c.form.status.text()


def test_retry_reuses_key_until_the_payload_changes(console):
    c = console(send_script=[OSError('lost'), {'run_id': 'run-1', 'task_id': 't', 'state': 'queued', 'worktree_path': '/w'}])
    c.fill()
    c.form.submit()
    spin(c.app, lambda: 'retrying reuses the same request key' in c.form.status.text())
    assert c.form.submit_button.isEnabled()
    c.form.submit()
    spin(c.app, lambda: len(c.sent) == 2)
    assert c.sent[0][1]['idempotency_key'] == c.sent[1][1]['idempotency_key']
    assert c.sent[0][1]['body'] == c.sent[1][1]['body']
    spin(c.app, lambda: 'run-1' in c.form.status.text())
    c.form.submit()  # after success a new submission gets a fresh key
    spin(c.app, lambda: len(c.sent) == 3)
    assert c.sent[2][1]['idempotency_key'] != c.sent[1][1]['idempotency_key']


def test_edit_between_attempts_changes_key(console):
    c = console(send_script=[OSError('lost')])
    c.fill()
    c.form.submit()
    spin(c.app, lambda: 'retrying' in c.form.status.text())
    c.form.prompt.setPlainText('different')
    c.form.submit()
    spin(c.app, lambda: len(c.sent) == 2)
    assert c.sent[0][1]['idempotency_key'] != c.sent[1][1]['idempotency_key']


@pytest.mark.parametrize('status', [404, 503])
def test_unavailable_state_is_clear(console, status):
    c = console(send_script=[DaemonHTTPError(status)], agent=status)
    c.fill()
    c.form.submit()
    spin(c.app, lambda: c.form.status.text() == agent_form.UNAVAILABLE_TEXT)
    assert str(status) not in c.form.status.text()
    c.window.run_id.setText('run-9')
    c.window._select_run()
    spin(c.app, lambda: c.window.agent_pane.status.text() == agent_form.UNAVAILABLE_TEXT)
    assert not c.window.agent_pane.cancel_button.isEnabled()


def test_other_http_error_is_a_plain_failure(console):
    c = console(send_script=[DaemonHTTPError(500)])
    c.fill()
    c.form.submit()
    spin(c.app, lambda: 'Start failed' in c.form.status.text())
    assert '500' not in c.form.status.text()


DETAIL = {'outcome': None, 'summary_text': 'did things', 'tool_names': ['Read', 'Grep'],
          'worktree': {'status': 'dirty', 'head': 'abc123', 'diff_stat': '1 file changed', 'diff': '+added\n-removed\n', 'truncated': False}}


def select(c, run='run-1'):
    c.window.run_id.setText(run)
    c.window._select_run()


def test_agent_pane_shows_detail_and_bounds_the_diff(console):
    big = dict(DETAIL, worktree=dict(DETAIL['worktree'], diff='+x\x1b[31m\n' * 50000, truncated=True), summary_text='s' * 50000,
               tool_names=['T%d' % i for i in range(200)])
    c = console(agent=big)
    select(c)
    pane = c.window.agent_pane
    spin(c.app, lambda: pane.diff.toPlainText())
    assert pane.diff.isReadOnly() and len(pane.diff.toPlainText()) <= agent_view.MAX_DIFF
    assert '\x1b' not in pane.diff.toPlainText()
    assert 'TRUNCATED' in pane.diff_note.text()
    assert len(pane.summary.toPlainText()) <= agent_view.MAX_SUMMARY
    assert '+150 more' in pane.tools.text()
    assert 'Outcome: not finished yet' in pane.outcome.text()


def test_local_truncation_flag_when_daemon_over_sends():
    detail = agent_view.sanitize_detail(dict(DETAIL, worktree=dict(DETAIL['worktree'], diff='a' * (agent_view.MAX_DIFF + 5))))
    assert len(detail['worktree']['diff']) == agent_view.MAX_DIFF and detail['worktree']['truncated'] is True
    assert agent_view.sanitize_detail({'tool_names': 'nope'}) is None and agent_view.sanitize_detail([]) is None


def test_cancel_requires_confirmation_and_retries_with_same_key(console):
    c = console(agent=DETAIL, send_script=[OSError('lost'), {}])
    select(c)
    pane = c.window.agent_pane
    spin(c.app, lambda: pane.cancel_button.isEnabled())
    c.answer = False
    pane.cancel_button.click()
    time.sleep(0.1)
    c.app.processEvents()
    assert c.sent == [] and c.confirms[-1][0] == 'Cancel agent run'
    c.answer = True
    pane.cancel_button.click()
    spin(c.app, lambda: 'retry uses the same request key' in pane.status.text())
    pane.cancel_button.click()
    spin(c.app, lambda: len(c.sent) == 2)
    assert c.sent[0][0] == '/api/v1/runs/run-1/agent-cancel' == c.sent[1][0]
    assert c.sent[0][1]['body'] == b''  # the daemon rejects any cancel body
    assert c.sent[0][1]['idempotency_key'] == c.sent[1][1]['idempotency_key']
    spin(c.app, lambda: 'Cancellation requested' in pane.status.text())
    assert not pane.cancel_button.isEnabled()


def test_cancel_disabled_when_finished(console):
    c = console(agent=dict(DETAIL, outcome='succeeded'))
    select(c)
    spin(c.app, lambda: 'succeeded' in c.window.agent_pane.outcome.text())
    assert not c.window.agent_pane.cancel_button.isEnabled()


def test_banner_links_to_decision_dialog_with_diff_context(console, monkeypatch):
    approval = {'id': 'ap1', 'run_id': 'run-1', 'state': 'requested', 'state_version': 2, 'operation': 'agent_run.start',
                'target': 'repo', 'candidate_manifest_sha256': 'a' * 64, 'policy_version': 'p1'}
    c = console(agent=DETAIL, run_state='awaiting_approval', approvals=[approval])
    seen = []
    monkeypatch.setattr('synapse.desktop.approval_decision.prompt_decision',
                        lambda parent, decision, binding, context=None: seen.append((decision, binding['approval_id'], context)))
    select(c)
    pane = c.window.agent_pane
    spin(c.app, lambda: not pane.banner.isHidden())
    pane.approve_button.click()
    assert seen[0][:2] == ('approved', 'ap1') and '+added' in seen[0][2]
    assert c.window.tabs.currentWidget() is c.window.approvals_tab


def test_decision_dialog_context_is_read_only_and_token_handling_unchanged():
    from PySide6.QtWidgets import QPlainTextEdit
    QApplication.instance() or QApplication([])
    binding = {'approval_id': 'a', 'expected_version': 1, 'candidate_sha256': 'b' * 64, 'operation': 'o', 'target': 't', 'policy_version': 'p'}
    dialog = approval_decision.DecisionDialog(None, 'approved', binding, 'diff text')
    assert isinstance(dialog.context_view, QPlainTextEdit) and dialog.context_view.isReadOnly()
    assert 'diff text' not in dialog.summary.text()
    dialog.token_edit.setText(TOKEN)
    assert dialog.take() == (TOKEN, None) and dialog.token_edit.text() == ''
    assert approval_decision.DecisionDialog(None, 'approved', binding).context_view is None
    assert agent_view.decision_context(None) is None


def test_mock_mode_still_available(console):
    c = console()
    panel = c.window.start_panel
    assert panel.mode.count() == 2 and panel.stack.currentIndex() == 0
    panel.mode.setCurrentIndex(1)
    assert panel.stack.currentWidget() is panel.agent_form


def test_control_center_composer_sends_and_selects_new_run(console):
    c = console(send_script=[{'run_id': 'run-new', 'task_id': 't', 'state': 'queued', 'worktree_path': '/w'}])
    composer = c.window.control_center.composer
    composer.settings = c.settings
    composer.repo.setCurrentText('/work/repo')
    composer.prompt.setPlainText('fix the bug')
    composer.submit_button.click()
    spin(c.app, lambda: c.window.run_id.text() == 'run-new')
    assert c.confirms and len(c.sent) == 1 and c.sent[0][0].endswith('/agent-runs')
    assert composer.prompt.toPlainText() == ''
    assert c.window.control_center.run_id == 'run-new'


# --- P0 abandon, P1 worktree removal, P1 usage ledger ---

def settle(c, seconds=0.15):
    time.sleep(seconds)
    c.app.processEvents()


def test_abandon_only_enabled_when_recovering(console):
    c = console(agent=DETAIL, run_state='running')
    select(c)
    spin(c.app, lambda: c.window.agent_pane.cancel_button.isEnabled())
    assert not c.window.agent_pane.abandon_button.isEnabled()
    d = console(agent=DETAIL, run_state='recovering')
    select(d)
    spin(d.app, lambda: d.window.agent_pane.abandon_button.isEnabled())
    assert not d.window.agent_pane.remove_button.isEnabled()


def test_abandon_cancel_does_nothing_then_confirms_and_sends_version(console):
    c = console(agent=DETAIL, run_state='recovering', send_script=[{'run_id': 'run-1', 'state': 'failed'}])
    select(c)
    pane = c.window.agent_pane
    spin(c.app, lambda: pane.abandon_button.isEnabled())
    c.answer = False
    pane.abandon_button.click()
    settle(c)
    assert c.sent == []
    title, text = c.confirms[-1]
    assert 'Abandon' in title and 'revoked' in text and 'no agent process' in text and 'not continued' in text.replace('NOT continued', 'not continued')
    c.answer = True
    pane.abandon_button.click()
    spin(c.app, lambda: 'Run abandoned' in pane.op_status.text())
    endpoint, kw = c.sent[0]
    assert endpoint == '/api/v1/runs/run-1/agent-abandon'
    assert json.loads(kw['body']) == {'expected_run_version': 1, 'assertion': 'no-agent-process-remains'}
    assert kw['idempotency_key'] and kw['correlation_id']
    assert not pane.abandon_button.isEnabled()


def test_abandon_errors_are_explicit_and_leak_nothing(console):
    c = console(agent=DETAIL, run_state='recovering', send_script=[DaemonHTTPError(409), DaemonHTTPError(422), DaemonHTTPError(404), OSError('secret path')])
    select(c)
    pane = c.window.agent_pane
    spin(c.app, lambda: pane.abandon_button.isEnabled())
    for expect in ('409', '422', '404/503', 'failed or the response was lost'):
        pane.abandon_button.click()
        spin(c.app, lambda e=expect: e in pane.op_status.text())
        spin(c.app, lambda: pane.abandon_button.isEnabled())
    assert 'secret' not in pane.op_status.text()
    keys = [kw['idempotency_key'] for _e, kw in c.sent]
    assert len(set(keys[:3])) == 3  # a refused request is final: a new attempt gets a fresh key


def test_remove_only_for_finished_runs_and_cancel_does_nothing(console):
    c = console(agent=dict(DETAIL, outcome='succeeded'), run_state='running')
    select(c)
    spin(c.app, lambda: 'succeeded' in c.window.agent_pane.outcome.text())
    assert not c.window.agent_pane.remove_button.isEnabled()
    d = console(agent=dict(DETAIL, outcome='succeeded'), run_state='succeeded', send_script=[{'run_id': 'run-1', 'removed': True, 'tasks': []}])
    select(d)
    pane = d.window.agent_pane
    spin(d.app, lambda: pane.remove_button.isEnabled())
    assert not pane.abandon_button.isEnabled()
    d.answer = False
    pane.remove_button.click()
    settle(d)
    assert d.sent == [] and 'branch' in d.confirms[-1][1] and 'uncommitted' in d.confirms[-1][1]
    d.answer = True
    pane.remove_button.click()
    spin(d.app, lambda: 'Worktree removed' in pane.op_status.text())
    endpoint, kw = d.sent[0]
    assert endpoint == '/api/v1/runs/run-1/agent-worktree-remove' and kw['body'] == b''
    assert not pane.remove_button.isEnabled()


def test_remove_has_no_force_and_conflict_text_is_not_overconfident(console):
    c = console(agent=dict(DETAIL, outcome='failed'), run_state='failed', send_script=[DaemonHTTPError(409)])
    select(c)
    pane = c.window.agent_pane
    assert not hasattr(pane, 'force_remove_button') and not hasattr(runs_view, 'FORCE_REMOVE_TEXT')
    spin(c.app, lambda: pane.remove_button.isEnabled())
    pane.remove_button.click()
    spin(c.app, lambda: '409' in pane.op_status.text())
    text = pane.op_status.text()
    assert 'Nothing was removed' not in text and 'may already have been removed' in text and 'audit trail' in text
    assert 'force' not in c.sent[0][0] and 'Force' not in text
    assert 'same request key' not in runs_view.OP_MESSAGES['remove']['failed'] and 'retried' in runs_view.OP_MESSAGES['remove']['failed']
    assert 'same request key' in runs_view.OP_MESSAGES['abandon']['failed']


def test_busy_slot_and_changed_run_are_reported_not_silent(console, monkeypatch):
    c = console(agent=DETAIL, run_state='recovering')
    select(c)
    pane = c.window.agent_pane
    spin(c.app, lambda: pane.abandon_button.isEnabled())
    c.window.poller.request_agent_op = lambda *a, **k: False
    pane.abandon_button.click()
    assert 'Another command is in progress' in pane.op_status.text()
    pane.op_status.setText('')
    c.window._known_run_version = 1

    def changed(*_a):
        c.window._known_run_version = 2
        return True
    monkeypatch.setattr(agent_form, "ask_confirm", changed)
    pane.abandon_button.click()
    assert 'changed while you were confirming' in pane.op_status.text()
    d = console(agent=dict(DETAIL, outcome='failed'), run_state='failed')
    select(d)
    spin(d.app, lambda: d.window.agent_pane.remove_button.isEnabled())
    d.window.poller.request_agent_op = lambda *a, **k: False
    d.window.agent_pane.remove_button.click()
    assert 'Another command is in progress' in d.window.agent_pane.op_status.text()


def test_agent_op_validates_response_shape(console):
    from synapse.desktop.connection import _op_response_ok as ok
    assert ok('abandon', 'r', {'run_id': 'r', 'state': 'failed', 'cancelled_tasks': 1})
    assert not ok('abandon', 'r', {'run_id': 'r', 'state': 'running'}) and not ok('abandon', 'r', {}) and not ok('abandon', 'r', None)
    assert ok('remove', 'r', {'run_id': 'r', 'removed': True, 'tasks': []})
    assert not ok('remove', 'r', {'run_id': 'r', 'removed': 'yes', 'tasks': []}) and not ok('remove', 'r', [])
    c = console(agent=DETAIL, run_state='recovering', send_script=[{'unexpected': 1}])
    select(c)
    pane = c.window.agent_pane
    spin(c.app, lambda: pane.abandon_button.isEnabled())
    pane.abandon_button.click()
    spin(c.app, lambda: 'unexpected response' in pane.op_status.text())


USAGE = {'run_id': 'run-1', 'attempts_total': 3, 'attempts_with_usage': 2,
         'observations': [
             {'id': 'o1', 'attempt_id': 'a1', 'task_id': 't1', 'profile_id': 'p', 'source': 'claude-cli', 'confidence': 'reported',
              'observed_at': 'x', 'input_tokens': 2, 'output_tokens': 0, 'cache_read_tokens': None, 'cache_write_tokens': 7, 'cost_usd': 0.0131914},
             {'id': 'o2', 'attempt_id': 'a2', 'task_id': 't1', 'profile_id': 'p', 'source': 'claude-cli', 'confidence': 'reported',
              'observed_at': 'x', 'input_tokens': 5, 'output_tokens': 4, 'cache_read_tokens': None, 'cache_write_tokens': None, 'cost_usd': None}],
         'totals': {'input_tokens': 7, 'output_tokens': 4, 'cache_read_tokens': None, 'cache_write_tokens': 7, 'cost_usd': 0.0131914},
         'coverage': {'input_tokens': {'known': 2, 'attempts': 3}, 'output_tokens': {'known': 2, 'attempts': 3},
                      'cache_read_tokens': {'known': 0, 'attempts': 3}, 'cache_write_tokens': {'known': 1, 'attempts': 3},
                      'cost_usd': {'known': 1, 'attempts': 3}}}


def test_usage_table_distinguishes_zero_from_unknown(console):
    c = console(agent=DETAIL, usage=USAGE)
    select(c)
    pane = c.window.usage_pane
    spin(c.app, lambda: pane.table.rowCount() == 2)
    cell = lambda r, col: pane.table.item(r, col).text()
    assert cell(0, 3) == '0' and cell(0, 4) == 'unknown' and cell(1, 6) == 'unknown' and cell(1, 5) == 'unknown'
    assert cell(0, 6) == '0.0132' or cell(0, 6) == '0.0132'[:6]
    assert 'cache read unknown' in pane.totals.text().lower() and 'lower bound, 2/3' in pane.totals.text()
    assert 'estimate' in pane.table.horizontalHeaderItem(6).text() and 'never zero' in pane.intro.text()
    assert '2 of 3' in pane.status.text()


def test_usage_empty_unavailable_and_invalid_states(console):
    empty = {'run_id': 'run-1', 'attempts_total': 0, 'attempts_with_usage': 0, 'observations': [],
             'totals': {f: None for f in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'cost_usd')}, 'coverage': {}}
    c = console(agent=DETAIL, usage=empty)
    select(c)
    spin(c.app, lambda: 'No usage has been recorded' in c.window.usage_pane.status.text())
    assert not c.window.usage_pane.table.isVisible() and c.window.usage_pane.totals.text() == ''
    d = console(agent=DETAIL, usage=404)
    select(d)
    spin(d.app, lambda: 'not available' in d.window.usage_pane.status.text())
    e = console(agent=DETAIL, usage=dict(USAGE, observations=[dict(USAGE['observations'][0], input_tokens=-1)]))
    select(e)
    spin(e.app, lambda: 'could not be read' in e.window.usage_pane.status.text())


def test_sanitize_usage_rejects_bad_values_and_bounds_rows():
    from synapse.desktop import agent_usage
    bad = dict(USAGE['observations'][0])
    for patch in ({'input_tokens': True}, {'input_tokens': 1.5}, {'cost_usd': float('nan')}, {'cost_usd': -1}, {'output_tokens': 'x'}):
        assert agent_usage.sanitize_usage(dict(USAGE, observations=[dict(bad, **patch)])) is None, patch
    many = dict(USAGE, observations=[bad] * 500)
    out = agent_usage.sanitize_usage(many)
    assert len(out['rows']) == agent_usage.MAX_ROWS and out['rows_total'] == 500
    assert agent_usage.sanitize_usage([]) is None


def test_usage_hardening(console):
    from synapse.desktop import agent_usage
    obs = USAGE['observations'][0]
    huge = agent_usage.sanitize_usage(dict(USAGE, observations=[dict(obs, cost_usd=10 ** 400)]))
    assert huge is not None and huge['rows'][0]['cost_usd'] is None
    bad = dict(USAGE, coverage=dict(USAGE['coverage'], input_tokens={'known': 5, 'attempts': 3}))
    out = agent_usage.sanitize_usage(bad)
    assert 'inconsistent coverage' in agent_usage.format_total(out, 'input_tokens')
    c = console(agent=DETAIL, usage=dict(USAGE, observations=[dict(obs, task_id='t' * 40)] * 1))
    select(c)
    pane = c.window.usage_pane
    spin(c.app, lambda: pane.table.rowCount() == 1)
    assert pane.table.accessibleName() and pane.table.accessibleDescription()
    assert pane.table.item(0, 0).toolTip() == 't' * 40 and len(pane.table.item(0, 0).text()) == 12
    from PySide6.QtCore import Qt
    assert all(w.textFormat() == Qt.TextFormat.PlainText for w in (pane.intro, pane.status, pane.totals))
    gen = c.window._active_generation
    c.window._agent_detail_error(c.window._active_run, gen, 'unavailable')
    assert pane.table.rowCount() == 0 and pane.totals.text() == ''
