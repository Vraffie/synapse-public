"""Multi-repository read-only views (ADR 026): view model, pane and poller wiring against ADR-shaped fixtures."""
import copy
import hashlib
import os
import time

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from synapse.cli.__main__ import DaemonHTTPError
from synapse.desktop import multirepo_model as m
from synapse.desktop import multirepo_routes as routes
from synapse.desktop.__main__ import MainWindow, Poller
from synapse.desktop.multirepo_view import MultiRepoPane

RUN = 'run-multi-1'
BASE_P, CAND_P = 'a' * 40, 'b' * 40
BASE_T = 'c' * 40
BASE_U = 'd' * 40
DIFF = 'diff --git a/x.xml b/x.xml\n+<a/>\n'
DIFF_SHA = hashlib.sha256(DIFF.encode()).hexdigest()
EV = 'e' * 64
MSHA = 'f' * 64
BSHA = '1' * 64


def run_repos():
    """GET /runs/{id}/repos: run_repo_bindings, attempt_worktrees and attempt_repo_commits rows (ADR 026 D10)."""
    return {'run_id': RUN, 'bindings_sha256': BSHA,
            'repos': [
                {'repo_id': 'portfolio', 'baseline_ref': 'refs/heads/main', 'baseline_sha': BASE_P, 'writer_task_id': 'writer',
                 'entry_sha256': EV},
                {'repo_id': 'tenant-specification', 'baseline_ref': 'refs/heads/main', 'baseline_sha': BASE_T, 'writer_task_id': None,
                 'entry_sha256': EV},
                {'repo_id': 'unchanged-repo', 'baseline_ref': 'refs/heads/main', 'baseline_sha': BASE_U, 'writer_task_id': 'writer',
                 'entry_sha256': EV}],
            'attempt_worktrees': [
                {'attempt_id': 'att-1', 'repo_id': 'portfolio', 'access': 'write', 'path': '/w/run/writer/1/portfolio',
                 'branch': f'synapse/{RUN}/writer/1', 'created_at': '2026-11-24T00:00:00Z'}],
            'commits': [
                {'attempt_id': 'att-1', 'repo_id': 'portfolio', 'seq': 1, 'state': 'intent', 'head_before': BASE_P},
                {'attempt_id': 'att-1', 'repo_id': 'portfolio', 'seq': 2, 'state': 'done', 'candidate_sha': CAND_P},
                {'attempt_id': 'att-1', 'repo_id': 'unchanged-repo', 'seq': 2, 'state': 'unchanged', 'candidate_sha': BASE_U}]}


def manifest_v2():
    """ADR 026 D8 example, verbatim field names."""
    return {'v': 2, 'run_id': RUN, 'bindings_sha256': BSHA, 'repos': [
        {'repo_id': 'portfolio', 'access': 'write', 'baseline_ref': 'refs/heads/main', 'baseline_sha': BASE_P, 'candidate_sha': CAND_P,
         'source': {'task_id': 'writer', 'attempt_no': 1}, 'diff_sha256': DIFF_SHA, 'diff_bytes': len(DIFF), 'changed_paths': 1,
         'validations': [{'name': 'paths_editable', 'outcome': 'passed', 'evidence_sha256': EV},
                         {'name': 'xml_well_formed', 'outcome': 'passed', 'evidence_sha256': EV}]},
        {'repo_id': 'tenant-specification', 'access': 'read', 'baseline_ref': 'refs/heads/main', 'baseline_sha': BASE_T},
        {'repo_id': 'unchanged-repo', 'access': 'write', 'baseline_ref': 'refs/heads/main', 'baseline_sha': BASE_U, 'candidate_sha': BASE_U,
         'source': {'task_id': 'writer', 'attempt_no': 1}, 'diff_sha256': hashlib.sha256(b'').hexdigest(), 'diff_bytes': 0, 'changed_paths': 0,
         'validations': [{'name': 'paths_editable', 'outcome': 'passed', 'evidence_sha256': EV},
                         {'name': 'xml_well_formed', 'outcome': 'passed', 'evidence_sha256': EV}]}]}


def manifest_detail(invalidations=()):
    return {'manifest_sha256': MSHA, 'manifest': manifest_v2(), 'invalidations': list(invalidations),
            'diffs': [{'repo_id': 'portfolio', 'diff': DIFF, 'truncated': False}]}


def manifest_list(invalidations=()):
    return {'manifests': [{'manifest_sha256': MSHA, 'version': 2, 'seq': 1, 'created_at': 'x', 'invalidations': list(invalidations)}]}


def registry():
    return {'registry_sha256': EV, 'repos': [
        {'id': 'portfolio', 'baseline_refs': ['refs/heads/main'], 'default_baseline_ref': 'refs/heads/main'},
        {'id': 'tenant-specification', 'baseline_refs': ['refs/heads/main'], 'default_baseline_ref': 'refs/heads/main', 'deploy': 'none'},
        {'id': 'unchanged-repo', 'baseline_refs': ['refs/heads/main'], 'default_baseline_ref': 'refs/heads/main'}]}


def full_view(invalidations=(), detail=None):
    return m.build_view(m.sanitize_run_repos(run_repos(), RUN), m.sanitize_manifest_list(manifest_list(invalidations)),
                        m.sanitize_manifest_detail(detail or manifest_detail(invalidations), RUN), m.sanitize_registry(registry()))


def rows_by_id(view):
    return {r['repo_id']: r for r in view['rows']}


# ------------------------------------------------------------------ routes
def test_route_constants_are_the_single_source_and_marked_proposal():
    assert routes.PROPOSAL is True
    assert routes.run_repos('a/b') == '/api/v1/runs/a%2Fb/repos'
    assert routes.run_manifests(RUN) == f'/api/v1/runs/{RUN}/candidate-manifests'
    assert routes.run_manifest(RUN, MSHA) == f'/api/v1/runs/{RUN}/candidate-manifests/{MSHA}'
    assert routes.registry() == '/api/v1/repos'
    import synapse.desktop.connection as connection
    source = open(connection.__file__, encoding='utf-8').read()
    assert 'candidate-manifests' not in source and '/repos"' not in source  # paths only live in multirepo_routes


# ------------------------------------------------------------------ view model
def test_rows_read_write_unchanged_and_deploy_none():
    view = full_view()
    assert view['visible'] is True and view['dropped'] == 0
    rows = rows_by_id(view)
    assert [r['repo_id'] for r in view['rows']] == sorted(rows)
    p, t, u = rows['portfolio'], rows['tenant-specification'], rows['unchanged-repo']
    assert (p['access'], p['state'], p['candidate_sha']) == ('write', 'changed', CAND_P)
    assert p['branch'] == f'synapse/{RUN}/writer/1' and p['changed_paths'] == 1
    assert (t['access'], t['state'], t['label']) == ('read', 'read', m.READ_TEXT)
    assert t['deploy_none'] and t['deploy'] == 'generated by the pipeline, not deployed by Synapse'
    assert (u['access'], u['state'], u['label']) == ('write', 'unchanged', m.UNCHANGED_TEXT)
    assert not p['deploy_none'] and not u['deploy_none']


def test_valid_manifest_and_diff_matches_its_digest():
    view = full_view()
    assert view['manifest']['validity'] == 'valid'
    assert rows_by_id(view)['portfolio']['diff']['verified'] is True
    head, banner = m.manifest_summary(view['manifest'])
    assert 'valid' in head and banner == ''


@pytest.mark.parametrize('reason, words', [('baseline_moved', 'baseline ref moved'), ('candidate_invalid', 'candidate commit'),
                                           ('operator', 'operator'), ('<b>evil</b>\x1b[2J', 'invalidated (<b>evil</b>[2J)')])
def test_invalidated_manifest_banner(reason, words):
    view = full_view(invalidations=[{'reason': reason, 'actor': 'daemon', 'at': 'x'}])
    assert view['manifest']['validity'] == 'invalidated'
    head, banner = m.manifest_summary(view['manifest'])
    assert 'INVALIDATED' in head and 'cannot be approved or deployed' in banner and words in banner
    assert '\x1b' not in banner


def test_invalidation_only_in_list_row_still_invalidates():
    view = m.build_view(m.sanitize_run_repos(run_repos(), RUN),
                        m.sanitize_manifest_list(manifest_list([{'reason': 'baseline_moved'}])),
                        m.sanitize_manifest_detail(manifest_detail(), RUN))
    assert view['manifest']['validity'] == 'invalidated'


def test_tampered_diff_is_not_claimed_to_match():
    detail = manifest_detail()
    detail['diffs'][0]['diff'] = DIFF + '+sneaky\n'
    assert rows_by_id(full_view(detail=detail))['portfolio']['diff']['verified'] is False


def test_local_problems_are_reported_without_claiming_validity():
    bad = manifest_detail()
    bad['manifest']['repos'].reverse()  # not sorted
    bad['manifest']['repos'][0]['validations'][0]['outcome'] = 'failed'
    bad['manifest']['run_id'] = 'other-run'
    bad['manifest']['bindings_sha256'] = '2' * 64
    view = full_view(detail=bad)
    assert view['manifest']['validity'] == 'problems'
    problems = ' '.join(view['manifest']['problems'])
    assert 'not sorted' in problems and 'did not pass' in problems and 'different run' in problems and 'bindings_sha256' in problems
    nochange = manifest_detail()
    nochange['manifest']['repos'][0]['candidate_sha'] = BASE_P
    assert any('no_changes' in p for p in full_view(detail=nochange)['manifest']['problems'])


def test_commit_states_without_manifest():
    raw = run_repos()
    raw['commits'] = [{'attempt_id': 'a', 'repo_id': 'portfolio', 'seq': 2, 'state': 'failed', 'reason': 'commit_failed:portfolio:hook\x07'},
                      {'attempt_id': 'a', 'repo_id': 'unchanged-repo', 'seq': 3, 'state': 'unknown'}]
    rows = rows_by_id(m.build_view(m.sanitize_run_repos(raw, RUN), None, None))
    assert rows['portfolio']['label'] == 'commit failed: commit_failed:portfolio:hook'
    assert rows['unchanged-repo']['label'].startswith('unknown after a crash')
    raw['commits'] = [{'repo_id': 'portfolio', 'state': 'weird'}]
    assert rows_by_id(m.build_view(m.sanitize_run_repos(raw, RUN), None, None))['portfolio']['label'] == 'state weird'


def test_single_repo_and_old_daemon_stay_hidden():
    assert m.build_view(None, None, None)['visible'] is False
    one = run_repos()
    one['repos'] = one['repos'][:1]
    assert m.build_view(m.sanitize_run_repos(one, RUN), [], None)['visible'] is False
    assert m.build_view(m.sanitize_run_repos(one, RUN), m.sanitize_manifest_list(manifest_list()), None)['visible'] is True


@pytest.mark.parametrize('raw', [None, [], 'x', 7, {}, {'repos': 'x'}, {'bindings': None}, {'run': {'id': RUN}, 'tasks': []}])
def test_not_understood_shapes_return_none(raw):
    assert m.sanitize_run_repos(raw) is None
    assert m.sanitize_manifest_list(raw) is None
    assert m.sanitize_manifest_detail(raw) is None
    assert m.sanitize_registry(raw) is None


def test_missing_fields_degrade_to_dashes():
    raw = {'repos': [{'repo_id': 'a'}, {'repo_id': 'b', 'writer_task_id': None}, {'repo_id': '../etc'}, {'nope': 1}, 'plain/string']}
    view = m.build_view(m.sanitize_run_repos(raw), None, None)
    rows = rows_by_id(view)
    assert set(rows) == {'a', 'b'} and view['dropped'] == 3
    assert rows['a']['access'] == 'unknown' and rows['a']['baseline_sha'] is None and rows['b']['access'] == 'read'
    detail = m.sanitize_manifest_detail({'manifest_sha256': MSHA, 'manifest': {'repos': [{'repo_id': 'a'}]}})
    assert detail['version'] is None and 'unknown manifest version' in detail['problems']
    assert detail['entries'][0]['candidate_sha'] is None


def test_v1_manifest_from_single_repo_daemon_is_readable():
    v1 = {'v': 1, 'run_id': RUN, 'repos': [{'repo_id': 'repo', 'baseline_sha': BASE_P, 'candidate_sha': CAND_P, 'diff_sha256': DIFF_SHA,
                                             'diff_bytes': 10, 'validations': []}]}
    detail = m.sanitize_manifest_detail({'manifest_sha256': MSHA, 'manifest': v1}, RUN)
    assert detail['version'] == 1 and detail['entries'][0]['access'] == 'write' and detail['problems'] == []


def test_hostile_ids_refs_and_shas_are_rejected():
    raw = run_repos()
    raw['repos'][0].update(baseline_ref='refs/heads/../../x', baseline_sha='<script>' * 5)
    raw['repos'].append({'repo_id': '<b>x</b>', 'writer_task_id': None})
    raw['repos'].append({'repo_id': 'r‮evil', 'writer_task_id': None})
    rows = rows_by_id(m.build_view(m.sanitize_run_repos(raw, RUN), None, None))
    assert '<b>x</b>' not in rows and all('‮' not in k for k in rows)
    assert rows['portfolio']['baseline_ref'] is None and rows['portfolio']['baseline_sha'] is None


def test_too_many_repos_are_bounded():
    raw = {'repos': [{'repo_id': f'r{i:03d}', 'writer_task_id': None} for i in range(500)]}
    view = m.build_view(m.sanitize_run_repos(raw), None, None)
    assert len(view['rows']) == m.MAX_REPOS and view['dropped'] == 500 - m.MAX_REPOS


def test_huge_and_hostile_diff_is_bounded_plain_text():
    hostile = '<img src=x onerror=alert(1)>\x1b]8;;file:///etc/passwd\x07link\x1b]8;;\x07‮txt.exe\x9b31m\r\n' * 100000
    diff = m.sanitize_diff(hostile)
    assert diff['truncated'] is True and len(diff['text']) <= m.MAX_DIFF_CHARS
    assert diff['text'].count('\n') < m.MAX_DIFF_LINES
    for bad in ('\x1b', '\x07', '‮', '\x9b', '\r'):
        assert bad not in diff['text']
    assert '<img src=x onerror=alert(1)>' in diff['text']  # shown literally
    many_lines = m.sanitize_diff('+\n' * (m.MAX_DIFF_LINES + 10))
    assert many_lines['truncated'] and many_lines['text'].count('\n') == m.MAX_DIFF_LINES - 1
    assert m.sanitize_diff(None) == {'text': '', 'truncated': False, 'raw_sha256': hashlib.sha256(b'').hexdigest()}
    assert m.sanitize_diff('x', truncated_flag='yes')['truncated'] is False  # only a real boolean True counts


# ------------------------------------------------------------------ pane
@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_pane_hidden_by_default_and_for_single_repo(app):
    pane = MultiRepoPane()
    assert pane.isHidden()
    pane.set_view(m.build_view(None, None, None))
    assert pane.isHidden()


def test_pane_shows_rows_diff_and_plain_text_everywhere(app):
    pane = MultiRepoPane()
    pane.set_view(full_view())
    assert not pane.isHidden() and pane.table.rowCount() == 3
    for label in pane.findChildren(QLabel):
        assert label.textFormat() == Qt.TextFormat.PlainText
    cells = {pane.table.item(r, 0).text(): [pane.table.item(r, c).text() for c in range(6)] for r in range(3)}
    assert cells['tenant-specification'][1] == 'read' and cells['tenant-specification'][5] == m.DEPLOY_NONE_TEXT
    assert cells['unchanged-repo'][4] == m.UNCHANGED_TEXT and cells['portfolio'][4] == 'changed'
    assert not pane.table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable
    assert pane.table.currentRow() == 0 and pane.diff.toPlainText() == DIFF  # the changed repo is preselected
    assert 'matches the manifest' in pane.diff_note.text() and pane.diff.isReadOnly()
    assert pane.banner.isHidden() and 'valid' in pane.manifest.text()
    pane.table.setCurrentCell(1, 0)
    assert pane.diff.toPlainText() == '' and 'Read-only repository' in pane.diff_note.text()
    assert m.DEPLOY_NONE_TEXT in pane.detail.text()
    pane.table.setCurrentCell(2, 0)
    assert 'No changes' in pane.diff_note.text()
    pane.set_view(full_view())  # a refresh keeps the selected repository
    assert pane.table.currentRow() == 2


def test_pane_invalidated_banner_and_hostile_diff(app):
    detail = manifest_detail([{'reason': 'baseline_moved'}])
    detail['diffs'][0]['diff'] = '<b>bold</b>\x1b[31mred\n' * 50000
    pane = MultiRepoPane()
    pane.set_view(full_view(invalidations=[{'reason': 'baseline_moved'}], detail=detail))
    assert not pane.banner.isHidden() and 'cannot be approved or deployed' in pane.banner.text()
    assert 'INVALIDATED' in pane.manifest.text()
    text = pane.diff.toPlainText()
    assert len(text) <= m.MAX_DIFF_CHARS and '\x1b' not in text and '<b>bold</b>' in text
    assert 'TRUNCATED' in pane.diff_note.text()
    pane.set_view(None)
    assert pane.isHidden() and pane.table.rowCount() == 0 and pane.diff.toPlainText() == ''


# ------------------------------------------------------------------ poller + window wiring
def spin(app, predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return predicate()


def make_window(tmp_path, answers):
    calls = []

    def fetch(_state, endpoint):
        calls.append(endpoint)
        if 'health' in endpoint:
            return {'status': 'ok'}
        if 'events' in endpoint:
            return {'events': []}
        if 'approvals' in endpoint:
            return {'approvals': []}
        if '/threads' in endpoint:
            return {'threads': []}
        if endpoint == '/api/v1/profiles' or endpoint.endswith('/agent') or '/usage?' in endpoint:
            raise DaemonHTTPError(404)
        for path, answer in answers.items():
            if endpoint == path:
                if isinstance(answer, int):
                    raise DaemonHTTPError(answer)
                return copy.deepcopy(answer)
        if endpoint.startswith('/api/v1/runs/') and endpoint.count('/') == 4:
            return {'run': {'id': RUN, 'state': 'succeeded', 'state_version': 1}, 'tasks': [], 'dependencies': []}
        raise DaemonHTTPError(404)

    window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=fetch, interval=0.01))
    window.run_id.setText(RUN)
    window._select_run()
    return window, calls


def test_window_shows_multirepo_pane_from_proposed_routes(app, tmp_path):
    answers = {routes.run_repos(RUN): run_repos(), routes.run_manifests(RUN): manifest_list([{'reason': 'baseline_moved'}]),
               routes.run_manifest(RUN, MSHA): manifest_detail([{'reason': 'baseline_moved'}]), routes.registry(): registry()}
    window, calls = make_window(tmp_path, answers)
    try:
        pane = window.multirepo_pane
        assert spin(app, lambda: pane.table.rowCount() == 3)
        assert not pane.banner.isHidden()
        assert routes.registry() in calls
        assert not any(c for c in calls if 'deploy' in c or 'approve' in c)  # read only
    finally:
        window.close()


def test_window_hides_pane_on_old_single_repo_daemon(app, tmp_path):
    window, calls = make_window(tmp_path, {})
    try:
        assert spin(app, lambda: routes.run_manifests(RUN) in calls)
        app.processEvents()
        assert window.multirepo_pane.isHidden()
        count = calls.count(routes.run_repos(RUN))
        spin(app, lambda: False, seconds=0.2)
        assert calls.count(routes.run_repos(RUN)) <= count + 1  # backs off instead of probing every poll
    finally:
        window.close()


def test_window_tolerates_garbage_from_proposed_routes(app, tmp_path):
    answers = {routes.run_repos(RUN): {'repos': [{'repo_id': '<i>x</i>'}] * 5}, routes.run_manifests(RUN): {'manifests': 'nope'}}
    window, calls = make_window(tmp_path, answers)
    try:
        assert spin(app, lambda: routes.run_manifests(RUN) in calls)
        app.processEvents()
        assert window.multirepo_pane.isHidden()
    finally:
        window.close()
