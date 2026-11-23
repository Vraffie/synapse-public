"""Validate and present operator evidence for fixed synthetic mock recovery."""
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout
import json

ASSERTION = "fixed-v1-no-external-effects"
PROFILE = "synapse.builtin.mock.v1"


def collect_evidence(run_id: str, before: dict, details: dict, after: dict) -> tuple[dict, dict]:
    """Fail closed on inconsistent snapshots, attempts, or non-fixed work."""
    if not isinstance(before, dict) or not isinstance(after, dict) or not isinstance(details, dict):
        raise ValueError("Invalid snapshot")
    if before.get('mock_config') != {'fixture': 'fixed-v1', 'profile_id': PROFILE, 'provider': 'mock'} or before.get('mock_config') != after.get('mock_config'):
        raise ValueError("Fixed mock configuration unavailable")
    first, last = before.get('run'), after.get('run')
    if not isinstance(first, dict) or not isinstance(last, dict) or first.get('id') != run_id or last.get('id') != run_id:
        raise ValueError("Run identity differs")
    version = first.get('state_version')
    if first.get('state') != 'recovering' or last.get('state') != 'recovering' or type(version) is not int or version < 0 or version != last.get('state_version'):
        raise ValueError("Run state or version changed")
    first_tasks, last_tasks, records = before.get('tasks'), after.get('tasks'), details.get('tasks')
    if not isinstance(first_tasks, list) or not isinstance(last_tasks, list) or not isinstance(records, list) or not 1 <= len(records) <= 1000:
        raise ValueError("Invalid task snapshots")
    def fingerprint(rows):
        result = {}
        for item in rows:
            if not isinstance(item, dict):
                raise ValueError("Invalid task")
            ident = item.get('id')
            if not isinstance(ident, str) or not ident.strip() or len(ident) > 256 or ident in result:
                raise ValueError("Invalid task identity")
            state, task_version = item.get('state'), item.get('state_version')
            count, profile = item.get('attempt_count'), item.get('assigned_profile_id')
            if not isinstance(state, str) or type(task_version) is not int or task_version < 0 or type(count) is not int or count < 0 or (profile is not None and not isinstance(profile, str)):
                raise ValueError("Invalid task state")
            result[ident] = (state, task_version, count, profile)
        return result
    expected = fingerprint(first_tasks)
    if expected != fingerprint(last_tasks) or expected != fingerprint(records):
        raise ValueError("Task state changed while loading evidence")
    evidence = []
    safe_tasks = []
    seen_attempts = set()
    for task in records:
        ident = task['id']
        state, task_version, count, profile = expected[ident]
        attempts = task.get('attempts')
        if not isinstance(attempts, list) or len(attempts) != count or len(attempts) > 1000:
            raise ValueError("Attempt history is incomplete")
        unknown = []
        safe_attempts = []
        for index, attempt in enumerate(attempts, 1):
            if (not isinstance(attempt, dict) or type(attempt.get('attempt_no')) is not int or attempt['attempt_no'] != index
                    or attempt.get('profile_id') != PROFILE or not isinstance(attempt.get('id'), str)
                    or not attempt['id'].strip() or len(attempt['id']) > 256):
                raise ValueError("Attempt identity or profile differs")
            if attempt['id'] in seen_attempts:
                raise ValueError("Duplicate attempt identity")
            seen_attempts.add(attempt['id'])
            if len(seen_attempts) > 10000:
                raise ValueError("Too many attempts to inspect")
            status = attempt.get('state')
            safe_attempts.append({'id': attempt['id'], 'attempt_no': index, 'state': status, 'profile_id': PROFILE})
            if status == 'unknown':
                unknown.append(attempt['id'])
            elif status not in ('completed', 'failed', 'cancelled'):
                raise ValueError("Attempt remains unresolved")
        if len(unknown) > 1 or (unknown and (state != 'blocked' or attempts[-1]['id'] != unknown[0])):
            raise ValueError("Unknown attempt cannot be reconciled")
        if profile is not None and profile != PROFILE:
            raise ValueError("Task has non-fixture profile")
        if state == 'blocked':
            if profile != PROFILE or count >= 2:
                raise ValueError("Blocked task profile or retry limit differs")
            evidence.append({'task_id': ident, 'expected_version': task_version, 'unknown_attempt_id': unknown[0] if unknown else None})
        elif state in ('assigned', 'running', 'verifying'):
            raise ValueError("A task remains active")
        safe_tasks.append({'id': ident, 'state': state, 'state_version': task_version, 'attempt_count': count,
                           'assigned_profile_id': profile, 'attempts': safe_attempts})
    evidence.sort(key=lambda row: row['task_id'])
    body = {'expected_run_version': version, 'tasks': evidence, 'assertion': ASSERTION}
    preview = {'run_id': run_id, 'run_state': 'recovering', 'run_version': version,
               'mock_config': before['mock_config'], 'tasks': safe_tasks, 'request': body}
    if len(json.dumps(preview, ensure_ascii=True).encode('utf-8')) > 1024 * 1024:
        raise ValueError("Evidence is too large to inspect")
    return body, preview


def confirm_evidence(parent, preview: dict) -> bool:
    """Explicit opt-in; presentation is not human authentication or approval."""
    dialog = QDialog(parent)
    dialog.setWindowTitle('Inspect fixed mock recovery evidence')
    dialog.resize(750, 550)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel('Inspect the run, tasks, attempts and exact request below. This applies only to the built-in synthetic fixture.'))
    text = QPlainTextEdit()
    text.setReadOnly(True)
    text.setPlainText(json.dumps(preview, indent=2, ensure_ascii=True))
    layout.addWidget(text)
    acknowledgement = QCheckBox('I inspected this evidence and assert fixed-v1 has no external effects.')
    layout.addWidget(acknowledgement)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
    ok.setText('Reconcile and resume fixed mock')
    ok.setEnabled(False)
    acknowledgement.toggled.connect(ok.setEnabled)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return dialog.exec() == QDialog.DialogCode.Accepted
