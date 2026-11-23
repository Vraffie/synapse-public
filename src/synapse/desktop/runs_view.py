"""Runs tab: run inspection, task list, dependency graph, parked approvals, mock cancel/resume.

RunsMixin is mixed into MainWindow; it owns the run selection state and the widgets built here.
"""
from __future__ import annotations

import hashlib
import json
import math
from uuid import uuid4

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QDialog, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

from synapse.desktop import agent_form
from synapse.desktop.agent_usage import UsagePane
from synapse.desktop.agent_view import AgentDetailPane, decision_context
from synapse.desktop.mock_resume import PROFILE, confirm_evidence
from synapse.desktop.start_panel import StartRunPanel

MAX_TASK_ROWS = 200
ABANDON_TEXT = (
    "Abandon recovering agent run {run}?\n\n"
    "This ends the run: it is marked failed and its unfinished tasks are cancelled. Recovery stops and the work is NOT continued or resumed.\n"
    "If a task is parked on an approval, that still-requested approval is revoked and can no longer be decided.\n"
    "Attempts stay 'unknown' and the worktree is kept for inspection. This cannot be undone.\n\n"
    "By confirming you assert that you have checked that no agent process for this run is still running or editing its worktree. "
    "The daemon cannot verify this.")
REMOVE_TEXT = (
    "Remove the worktree of finished run {run}?\n\n"
    "Removed: each task's git worktree directory and its branch.\n"
    "Not removed: the run, its events and its artifacts.\n"
    "If any task's worktree has uncommitted changes (including untracked files) the daemon refuses. Forced removal is not offered here.")
COMMIT_OK = ("Committed on each task's own branch (your checkout was not touched, nothing was pushed):\n{rows}")
_COMMON = {'unavailable': "Not available: the daemon does not know this agent run or agent runs are not configured (404/503). Nothing changed.",
           'rejected': "Refused by the daemon (422). Nothing changed.",
           'invalid': "The daemon sent an unexpected response; check the run's audit trail and refresh before retrying."}
OP_MESSAGES = {
    'abandon': dict(_COMMON, accepted="Run abandoned: it is now failed and its tasks are cancelled. The worktree is kept.",
                    failed="The request failed or the response was lost. Retrying uses the same request key.",
                    conflict="Refused (409): the run is no longer recovering or changed since it was loaded. Nothing changed; check the run and try again."),
    'remove': dict(_COMMON, accepted="Worktree removed (or was already gone).",
                   failed="The request failed or the response was lost. It can be retried.",
                   conflict="Removal refused or incomplete (409): the run is not finished, or a worktree has uncommitted changes. "
                            "Some task worktrees may already have been removed; check the run's audit trail and refresh."),
    'commit': dict(_COMMON, accepted="Committed (your checkout was not touched, nothing was pushed).",
                   failed="The request failed or the response was lost. It can be retried.",
                   conflict="Nothing was committed (409): there are no changes to commit, the run is not finished, or the daemon refused.",
                   rejected="The commit message was refused (422). Nothing was committed."),
}
BUSY_TEXT = "Another command is in progress, try again."
CHANGED_TEXT = "The run changed while you were confirming; nothing was sent."


def _task_signature(tasks):
    fields = ('id', 'state', 'state_version', 'attempt_count', 'assigned_profile_id')
    return tuple(sorted(tuple(task.get(field) for field in fields) for task in tasks))


class RunsMixin:
    def _build_runs_tab(self) -> QWidget:
        self.run_id = QLineEdit()
        self.run_id.setPlaceholderText("Run ID")
        self.run_status = QLabel("Enter a run ID to inspect its graph.")
        self.graph_scene = QGraphicsScene(self)
        graph = QGraphicsView(self.graph_scene)
        load = QPushButton("Inspect run")
        controls = QHBoxLayout()
        controls.addWidget(self.run_id)
        controls.addWidget(load)
        self.start_panel = StartRunPanel()
        self.start_panel.start_requested.connect(self._start_requested)
        self.start_panel.agent_start_requested.connect(self._agent_start_requested)
        self.agent_pane = AgentDetailPane()
        self.agent_pane.cancel_clicked.connect(self._cancel_agent)
        self.agent_pane.abandon_clicked.connect(self._abandon_agent)
        self.agent_pane.remove_clicked.connect(self._remove_worktree)
        self.agent_pane.commit_clicked.connect(self._commit_changes)
        self.usage_pane = UsagePane()
        self.agent_pane.approve_clicked.connect(lambda: self._agent_decide("approved"))
        self.agent_pane.reject_clicked.connect(lambda: self._agent_decide("rejected"))
        self.mock_button = self.start_panel.button
        self.mock_status = self.start_panel.status
        self.cancel_mock_button = QPushButton("Cancel fixed mock run")
        self.cancel_mock_button.setEnabled(False)
        controls.addWidget(self.cancel_mock_button)
        self.resume_mock_button = QPushButton("Inspect fixed mock recovery")
        self.resume_mock_button.setEnabled(False)
        controls.addWidget(self.resume_mock_button)
        self.discard_resume_button = QPushButton("Discard rejected recovery and reinspect")
        self.discard_resume_button.setEnabled(False)
        controls.addWidget(self.discard_resume_button)
        run_layout = QVBoxLayout()
        run_layout.addLayout(controls)
        run_layout.addWidget(self.run_status)
        self.start_dialog = QDialog(self)
        self.start_dialog.setWindowTitle("Start run")
        QVBoxLayout(self.start_dialog).addWidget(self.start_panel)
        self.start_dialog_button = QPushButton("Start run\u2026")
        self.start_dialog_button.clicked.connect(self.show_start_dialog)
        run_layout.addWidget(self.start_dialog_button)
        self.cancel_mock_status = QLabel("Select a running fixed mock run to request cancellation.")
        run_layout.addWidget(self.cancel_mock_status)
        self.resume_mock_status = QLabel("Select a recovering fixed mock run to inspect evidence.")
        run_layout.addWidget(self.resume_mock_status)
        self.recent_runs = QListWidget()
        self.recent_runs.setMaximumHeight(70)
        self.recent_runs.currentItemChanged.connect(self._pick_recent_run)
        self._recent = {}
        run_layout.addWidget(QLabel("Runs inspected this session (the daemon API has no run-list endpoint):"))
        run_layout.addWidget(self.recent_runs)
        self.task_status = QLabel("")
        self.task_list = QListWidget()
        self.task_list.setUniformItemSizes(True)
        self.parked_status = QLabel("")
        self.parked_status.setWordWrap(True)
        self.parked_list = QListWidget()
        self.parked_list.setMaximumHeight(80)
        self.review_approval_button = QPushButton("Review selected approval…")
        self.review_approval_button.setEnabled(False)
        self.review_approval_button.clicked.connect(self._review_parked_approval)
        self.parked_list.currentItemChanged.connect(lambda *_: self.review_approval_button.setEnabled(self.parked_list.currentRow() >= 0))
        run_layout.addWidget(self.task_status)
        run_layout.addWidget(self.task_list)
        run_layout.addWidget(self.parked_status)
        run_layout.addWidget(self.parked_list)
        run_layout.addWidget(self.review_approval_button)
        run_layout.addWidget(self.agent_pane)
        run_layout.addWidget(self.usage_pane)
        run_layout.addWidget(graph)
        widget = QWidget()
        widget.setLayout(run_layout)
        load.clicked.connect(self._select_run)
        self.run_id.textChanged.connect(self._run_input_changed)
        self.run_id.returnPressed.connect(self._select_run)
        self.resume_mock_button.clicked.connect(self._resume_mock)
        self.discard_resume_button.clicked.connect(self._discard_resume)
        self.cancel_mock_button.clicked.connect(self._cancel_mock)
        return widget

    def _init_runs_state(self):
        self._active_run = ""
        self._active_generation = 0
        self._mock_keys = {}
        self._mock_results = {}
        self._mock_pending = None
        self._cancel_keys = {}
        self._cancel_results = {}
        self._cancel_pending = None
        self._resume_pending = None
        self._resume_intents = {}
        self._resume_results = {}
        self._known_run_state = None
        self._known_run_version = None
        self._known_mock_config = None
        self._known_task_signature = None
        self._known_tasks = []
        self._graph_signature = None
        self._parked_ids = []
        self._agent_details = {}
        self._agent_start_repo = None
        self._agent_start_form = None
        self._agent_cancel_keys = {}
        self._agent_cancel_pending = None
        self._agent_cancel_results = {}
        self._agent_unavailable = False
        self._agent_op_keys = {}
        self._agent_op_last = {}  # run id -> (action, outcome) of the latest finished request
        self._agent_op_pending = None  # (action, run id)
        self._commit_rows = {}  # run id -> [(branch, short head)] of the latest commit
        self._agent_op_results = {}  # (action, run id) -> outcome

    def _connect_runs_poller(self):
        self.poller.mock_started.connect(self._mock_finished)
        self.poller.mock_cancel_requested.connect(self._cancel_mock_finished)
        self.poller.mock_resume_prepared.connect(self._resume_prepared)
        self.poller.mock_resume_finished.connect(self._resume_finished)
        self.poller.run_snapshot.connect(self._show_run)
        self.poller.run_error.connect(self._run_unavailable)
        self.poller.agent_started.connect(self._agent_started)
        self.poller.profiles_list.connect(self.start_panel.agent_form.set_profiles)
        self.poller.profiles_list.connect(self.control_center.composer.set_profiles)
        self.poller.agent_cancel_finished.connect(self._agent_cancel_finished)
        self.poller.agent_op_finished.connect(self._agent_op_finished)
        self.poller.agent_commit_done.connect(self._agent_commit_done)
        self.poller.agent_usage.connect(self._agent_usage_shown)
        self.poller.agent_usage_error.connect(self._agent_usage_error)
        self.poller.agent_detail.connect(self._agent_detail_shown)
        self.poller.agent_detail_error.connect(self._agent_detail_error)

    def _start_requested(self, request):
        if request.run_id == self._active_run:
            self._start_mock()

    def show_start_dialog(self):
        """The Start run forms (mock fixture / agent run) live in this dialog; the Control Center button opens it."""
        self.start_dialog.show()
        self.start_dialog.raise_()
        self.start_dialog.activateWindow()

    def _composer_submit(self, submission):
        self._agent_start_requested(submission, self.control_center.composer)

    def _agent_start_requested(self, submission, form=None):
        form = form or self.start_panel.agent_form
        if self.poller.request_agent_start(submission.key, submission.correlation, submission.body):
            self._agent_start_repo = submission.payload["repo"]
            self._agent_start_form = form
            form.set_pending(True)
        else:
            form.status.setText("Another command is in progress; try again shortly.")

    def _agent_started(self, outcome, result, _key):
        if self._agent_start_repo is None:
            return
        repo, self._agent_start_repo = self._agent_start_repo, None
        form = self._agent_start_form or self.start_panel.agent_form
        self._agent_start_form = None
        form.finished(outcome, result, repo if outcome == "accepted" else "")
        if outcome == "accepted" and result:
            self.run_id.setText(result["run_id"])
            self._select_run()
            self.start_dialog.hide()
            self.views.setCurrentWidget(self.control_center)

    def _agent_context_for(self, approval_id):
        """Worktree diff for the run owning this approval, when that run's agent detail is loaded."""
        row = next((r for r in self._approval_rows if r.get("id") == approval_id), None)
        run_id = row.get("run_id") if row else None
        return decision_context(self._agent_details.get(run_id)) if isinstance(run_id, str) else None

    def _agent_decide(self, decision):
        if not self._parked_ids:
            return
        if self.parked_list.currentRow() < 0:
            self.parked_list.setCurrentRow(0)
        self._review_parked_approval()
        self.open_decision_dialog(decision)

    def _update_agent_pane(self):
        run_id = self._active_run
        detail = self._agent_details.get(run_id)
        self.agent_pane.set_awaiting_approval(bool(run_id and self._parked_ids and (detail is not None or self._known_run_state == 'awaiting_approval')))
        result = self._agent_cancel_results.get(run_id)
        if self._agent_cancel_pending == run_id:
            self.agent_pane.status.setText("Requesting agent cancellation…")
        elif result == 'accepted':
            self.agent_pane.status.setText("Cancellation requested; waiting for the run to finish.")
        elif result == 'unavailable':
            self.agent_pane.status.setText(agent_form.UNAVAILABLE_TEXT)
        elif result == 'failed':
            self.agent_pane.status.setText("Cancellation failed or response lost; retry uses the same request key.")
        self.agent_pane.cancel_button.setEnabled(bool(
            detail is not None and detail["outcome"] is None and run_id and self.run_id.text().strip() == run_id
            and self._known_run_state not in ('cancelled', 'succeeded', 'failed')
            and self._agent_cancel_pending is None and result != 'accepted'))
        self.control_center.set_can_cancel(self.agent_pane.cancel_button.isEnabled())
        self._update_agent_ops(run_id, detail)

    def _update_agent_ops(self, run_id, detail):
        """Abandon (recovering runs only) and worktree removal (finished runs only); both need a loaded agent detail."""
        pane = self.agent_pane
        known = bool(detail is not None and run_id and self.run_id.text().strip() == run_id)
        idle = self._agent_op_pending is None
        recovering = known and self._known_run_state == 'recovering' and type(self._known_run_version) is int
        finished = known and self._known_run_state in ('succeeded', 'failed', 'cancelled')
        abandoned = self._agent_op_results.get(('abandon', run_id)) == 'accepted'
        removed = self._agent_op_results.get(('remove', run_id)) == 'accepted'
        pane.abandon_button.setEnabled(bool(recovering and idle and not abandoned))
        pane.remove_button.setEnabled(bool(finished and idle and not removed))
        dirty = bool(detail is not None and detail.get('worktree') and detail['worktree'].get('status') == 'dirty')
        pane.commit_button.setEnabled(bool(finished and idle and not removed and dirty))
        if self._agent_op_pending and self._agent_op_pending[1] == run_id:
            pane.op_status.setText("Sending request to the daemon…")
            return
        last = self._agent_op_last.get(run_id)
        text = OP_MESSAGES[last[0]][last[1]] if last else ""
        if last == ('commit', 'accepted') and self._commit_rows.get(run_id):
            text = COMMIT_OK.format(rows="\n".join(f"{branch}  {head}" for branch, head in self._commit_rows[run_id]))
        pane.op_status.setText(text)

    def _agent_op_key(self, action, run_id, version=None):
        return self._agent_op_keys.setdefault((action, run_id, version), str(uuid4()))

    def _abandon_agent(self):
        if not self.agent_pane.abandon_button.isEnabled():
            return
        run_id, version = self._active_run, self._known_run_version
        if not agent_form.ask_confirm(self, "Abandon recovering agent run", ABANDON_TEXT.format(run=run_id[:128])):
            return
        if run_id != self._active_run or version != self._known_run_version or self._known_run_state != 'recovering':
            self.agent_pane.op_status.setText(CHANGED_TEXT)
            return
        key = self._agent_op_key('abandon', run_id, version)
        if self.poller.request_agent_op('abandon', run_id, key, version):
            self._agent_op_pending = ('abandon', run_id)
            self._update_agent_pane()
        else:
            self.agent_pane.op_status.setText(BUSY_TEXT)

    def _remove_worktree(self):
        if not self.agent_pane.remove_button.isEnabled():
            return
        run_id = self._active_run
        if not agent_form.ask_confirm(self, "Remove worktree", REMOVE_TEXT.format(run=run_id[:128])):
            return
        if run_id != self._active_run or self._known_run_state not in ('succeeded', 'failed', 'cancelled'):
            self.agent_pane.op_status.setText(CHANGED_TEXT)
            return
        if self.poller.request_agent_op('remove', run_id, self._agent_op_key('remove', run_id)):
            self._agent_op_pending = ('remove', run_id)
            self._update_agent_pane()
        else:
            self.agent_pane.op_status.setText(BUSY_TEXT)

    def _commit_changes(self):
        if not self.agent_pane.commit_button.isEnabled():
            return
        run_id = self._active_run
        message = agent_form.ask_commit_message(self)
        if message is None:
            return
        if run_id != self._active_run or self._known_run_state not in ('succeeded', 'failed', 'cancelled'):
            self.agent_pane.op_status.setText(CHANGED_TEXT)
            return
        # the key depends on a digest of the message so a lost-response retry of the same text is idempotent; the text is not kept
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        if self.poller.request_agent_commit(run_id, self._agent_op_key('commit', run_id, digest), message):
            self._agent_op_pending = ('commit', run_id)
            self._update_agent_pane()
        else:
            self.agent_pane.op_status.setText(BUSY_TEXT)

    def _agent_commit_done(self, run_id, rows):
        self._commit_rows[run_id] = list(rows or [])

    def _agent_op_finished(self, action, run_id, outcome):
        if self._agent_op_pending != (action, run_id):
            return
        self._agent_op_pending = None
        self._agent_op_results[(action, run_id)] = outcome if outcome in OP_MESSAGES[action] else 'failed'
        self._agent_op_last[run_id] = (action, self._agent_op_results[(action, run_id)])
        if outcome == 'conflict' or outcome == 'rejected':
            # a refused request is final for that key; a later attempt (different state) gets a fresh one
            self._agent_op_keys = {k: v for k, v in self._agent_op_keys.items() if k[:2] != (action, run_id)}
        self._update_agent_pane()

    def _agent_usage_shown(self, run_id, generation, usage):
        if run_id == self._active_run and generation == self._active_generation:
            self.usage_pane.set_usage(usage)

    def _agent_usage_error(self, run_id, generation, kind):
        if run_id == self._active_run and generation == self._active_generation:
            (self.usage_pane.set_unavailable if kind == 'unavailable' else self.usage_pane.set_failed)()

    def _agent_detail_shown(self, run_id, generation, detail):
        if run_id != self._active_run or generation != self._active_generation:
            return
        self._agent_details[run_id] = detail
        self.control_center.set_agent_detail(detail)
        self.agent_pane.set_detail(detail)
        self._update_agent_pane()

    def _agent_detail_error(self, run_id, generation, kind):
        if run_id != self._active_run or generation != self._active_generation:
            return
        if kind == "unavailable":
            self._agent_details.pop(run_id, None)
            self.control_center.set_agent_detail(None)
            self.agent_pane.set_unavailable()
            self.usage_pane.clear()
            self._update_agent_pane()
        elif run_id not in self._agent_details:
            self.agent_pane.clear("Agent details unavailable right now.")

    def _cancel_agent(self):
        if not self.agent_pane.cancel_button.isEnabled():
            return
        run_id = self._active_run
        if not agent_form.ask_confirm(self, "Cancel agent run",
                                      f"Request cancellation of agent run {run_id[:128]}?\nThe agent is stopped and its worktree is kept for inspection."):
            return
        if run_id != self._active_run:
            return
        key = self._agent_cancel_keys.setdefault(run_id, str(uuid4()))
        if self.poller.request_agent_cancel(run_id, key):
            self._agent_cancel_pending = run_id
            self._update_agent_pane()

    def _agent_cancel_finished(self, run_id, outcome):
        if self._agent_cancel_pending != run_id:
            return
        self._agent_cancel_pending = None
        self._agent_cancel_results[run_id] = outcome if outcome in ('accepted', 'unavailable') else 'failed'
        self._update_agent_pane()

    def _select_run(self):
        run_id = self.run_id.text().strip()
        if not run_id or len(run_id) > 256 or run_id in (".", "..") or "/" in run_id or any(ord(c) < 32 for c in run_id):
            # Never leave actions pointed at a previous run after invalid input.
            self._active_run = ""
            self._active_thread = ""
            self._known_run_state = None
            self._known_run_version = None
            self._known_mock_config = None
            self._known_task_signature = None
            self._active_generation = self.poller.select_run("")
            self._clear_run_panes()
            self._reset_messages(loading=False)
            self._update_mock_controls()
            self.run_status.setText("Enter a valid run ID.")
            return
        self._active_run = run_id
        self._known_run_state = None
        self._known_run_version = None
        self._known_mock_config = None
        self._known_task_signature = None
        self._update_mock_controls()
        self._clear_run_panes()
        self._active_thread = ""
        self._reset_messages(loading=True)
        self.run_status.setText("Loading run…")
        self._active_generation = self.poller.select_run(run_id)
        self._reset_message_paging()

    def _clear_run_panes(self):
        self.control_center.set_run_id(self._active_run)
        self.agent_pane.clear()
        self.usage_pane.clear()
        self.agent_pane.set_awaiting_approval(False)
        self.graph_scene.clear()
        self._graph_signature = None
        self._known_tasks = []
        self.task_list.clear()
        self.task_status.setText("")
        self._refresh_parked()

    def _run_input_changed(self):
        self._update_mock_controls()

    def _update_mock_controls(self):
        selection_matches_input = bool(self._active_run and self.run_id.text().strip() == self._active_run)
        self.start_panel.set_target(self._active_run)
        result = self._mock_results.get(self._active_run)
        if self._mock_pending == self._active_run:
            self.mock_status.setText("Starting fixed mock fixture…")
        elif result == 'accepted':
            self.mock_status.setText("Mock start accepted; inspect run state for the outcome.")
        elif result == 'failed':
            self.mock_status.setText("Start failed or response lost; retry uses the same request key.")
        else:
            self.mock_status.setText("Select a queued run to start the fixed mock fixture.")
        self.mock_button.setEnabled(bool(selection_matches_input and self._mock_pending is None and self._cancel_pending is None and self._resume_pending is None
                                         and result != 'accepted'
                                         and (self._known_run_state == 'queued' or (result == 'failed' and self._known_run_state == 'running'))))
        cancel = self._cancel_results.get(self._active_run)
        if self._known_run_state in ('cancelled', 'succeeded', 'failed'):
            self.cancel_mock_status.setText("Run is " + self._known_run_state + "; cancellation is not pending.")
        elif self._cancel_pending == self._active_run:
            self.cancel_mock_status.setText("Requesting fixed mock cancellation…")
        elif cancel == 'accepted':
            self.cancel_mock_status.setText("Cancellation requested; waiting for terminal run state.")
        elif cancel == 'failed':
            self.cancel_mock_status.setText("Cancellation failed or response lost; retry uses the same request key.")
        else:
            self.cancel_mock_status.setText("Select a running fixed mock run to request cancellation.")
        self.cancel_mock_button.setEnabled(bool(selection_matches_input and self._known_run_state == 'running'
                                               and self._mock_pending is None and self._cancel_pending is None and self._resume_pending is None
                                               and cancel != 'accepted'))
        resume = self._resume_results.get(self._active_run)
        if self._resume_pending == self._active_run:
            self.resume_mock_status.setText("Preparing or submitting fixed mock recovery…")
        elif resume == 'accepted':
            self.resume_mock_status.setText("Resume accepted; inspect the current run state for its outcome.")
        elif resume == 'failed':
            self.resume_mock_status.setText("Resume failed or response lost; retry uses the same evidence and key.")
        elif resume == 'rejected':
            self.resume_mock_status.setText("Recovery rejected (HTTP 409); discard this intent to reinspect current evidence.")
        elif resume == 'invalid':
            self.resume_mock_status.setText("Recovery evidence unavailable or changed; inspect and try again.")
        else:
            self.resume_mock_status.setText("Select a recovering fixed mock run to inspect evidence.")
        fixed = self._known_mock_config == {'fixture': 'fixed-v1', 'profile_id': PROFILE, 'provider': 'mock'}
        retry = resume == 'failed' and self._active_run in self._resume_intents
        allowed_state = self._known_run_state == 'recovering' or (retry and self._known_run_state == 'running')
        self.resume_mock_button.setEnabled(bool(selection_matches_input and fixed and allowed_state
                                               and resume not in ('accepted', 'rejected') and self._mock_pending is None
                                               and self._cancel_pending is None and self._resume_pending is None))
        self.discard_resume_button.setEnabled(bool(selection_matches_input and resume == 'rejected'
                                                  and self._active_run in self._resume_intents
                                                  and self._mock_pending is None and self._cancel_pending is None
                                                  and self._resume_pending is None))
        if self._active_run and not selection_matches_input:
            self.mock_status.setText("Inspect the entered run to enable actions.")
            self.cancel_mock_status.setText("Inspect the entered run to enable actions.")
            self.resume_mock_status.setText("Inspect the entered run to enable actions.")

    def _discard_resume(self):
        if not self.discard_resume_button.isEnabled():
            return
        self._resume_intents.pop(self._active_run, None)
        self._resume_results.pop(self._active_run, None)
        self._update_mock_controls()

    def _resume_mock(self):
        if not self.resume_mock_button.isEnabled():
            return
        run_id, generation = self._active_run, self._active_generation
        self._resume_pending = run_id
        self._update_mock_controls()
        intent = self._resume_intents.get(run_id)
        if intent is not None:
            # The same previously confirmed request is the only safe retry.
            key, body, _preview = intent
            if not self.poller.request_mock_resume(run_id, key, body):
                self._resume_pending = None
                self._update_mock_controls()
        elif not self.poller.prepare_mock_resume(run_id, generation):
            self._resume_pending = None
            self._update_mock_controls()

    def _resume_prepared(self, run_id, generation, prepared):
        if self._resume_pending != run_id:
            return
        if (run_id != self._active_run or generation != self._active_generation
                or self.run_id.text().strip() != run_id or self._known_run_state != 'recovering'):
            self._resume_pending = None
            self._update_mock_controls()
            return
        if prepared is None:
            self._resume_pending = None
            self._resume_results[run_id] = 'invalid'
            self._update_mock_controls()
            return
        body, preview = prepared
        if (type(self._known_run_version) is not int or self._known_run_version != body['expected_run_version']
                or self._known_task_signature != _task_signature(preview['tasks'])
                or not confirm_evidence(self, preview)):
            self._resume_pending = None
            self._update_mock_controls()
            return
        # The modal dialog may process selection/snapshot updates while open.
        if (run_id != self._active_run or generation != self._active_generation
                or self.run_id.text().strip() != run_id or self._known_run_state != 'recovering'
                or type(self._known_run_version) is not int or self._known_run_version != body['expected_run_version']
                or self._known_task_signature != _task_signature(preview['tasks'])
                or self._known_mock_config != {'fixture': 'fixed-v1', 'profile_id': PROFILE, 'provider': 'mock'}):
            self._resume_pending = None
            self._update_mock_controls()
            return
        key = str(uuid4())
        encoded = json.dumps(body, sort_keys=True, separators=(',', ':')).encode()
        self._resume_intents[run_id] = (key, encoded, preview)
        if not self.poller.request_mock_resume(run_id, key, encoded):
            self._resume_pending = None
            self._resume_results[run_id] = 'failed'
            self._update_mock_controls()

    def _resume_finished(self, run_id, outcome):
        if self._resume_pending != run_id:
            return
        self._resume_pending = None
        self._resume_results[run_id] = outcome if outcome in ('accepted', 'rejected') else 'failed'
        self._update_mock_controls()

    def _cancel_mock(self):
        if not self.cancel_mock_button.isEnabled():
            return
        run_id = self._active_run
        key = self._cancel_keys.setdefault(run_id, str(uuid4()))
        if self.poller.request_mock_cancel(run_id, key):
            self._cancel_pending = run_id
            self._update_mock_controls()

    def _cancel_mock_finished(self, run_id, success):
        if self._cancel_pending != run_id:
            return
        self._cancel_pending = None
        self._cancel_results[run_id] = 'accepted' if success else 'failed'
        self._update_mock_controls()

    def _start_mock(self):
        if not self.mock_button.isEnabled():
            return
        run_id = self._active_run
        key = self._mock_keys.setdefault(run_id, str(uuid4()))
        if self.poller.request_mock_start(run_id, key):
            self._mock_pending = run_id
            self._update_mock_controls()

    def _mock_finished(self, run_id, success):
        if self._mock_pending != run_id:
            return
        self._mock_pending = None
        self._mock_results[run_id] = 'accepted' if success else 'failed'
        self._update_mock_controls()

    def _show_run(self, run_id, generation, snapshot):
        if run_id != self._active_run or generation != self._active_generation:
            return
        tasks = snapshot["tasks"]
        dependencies = snapshot["dependencies"]
        if (len(tasks) > 1000 or len(dependencies) > 5000
                or any(not isinstance(t, dict) or not isinstance(t.get("id"), str) for t in tasks)
                or any(not isinstance(d, dict) or not isinstance(d.get("task_id"), str)
                       or not isinstance(d.get("depends_on_id"), str) for d in dependencies)):
            self._known_run_state = None
            self._known_run_version = None
            self._known_mock_config = None
            self._known_task_signature = None
            self._update_mock_controls()
            self.run_status.setText("Run graph is invalid or too large — showing last result")
            return
        ids = [task["id"] for task in tasks]
        if len(set(ids)) != len(ids) or any(edge["task_id"] not in ids or edge["depends_on_id"] not in ids for edge in dependencies):
            self._known_run_state = None
            self._known_run_version = None
            self._known_mock_config = None
            self._known_task_signature = None
            self._update_mock_controls()
            self.run_status.setText("Run graph is invalid — showing last result")
            return
        self._known_run_state = snapshot["run"].get("state")
        self._known_run_version = snapshot["run"].get("state_version")
        self._known_mock_config = snapshot.get("mock_config")
        signature = (_task_signature(tasks), tuple(sorted((d["task_id"], d["depends_on_id"]) for d in dependencies)))
        unchanged = signature == self._graph_signature and bool(self.graph_scene.items())
        self._known_task_signature = _task_signature(tasks)
        self._update_mock_controls()
        self._render_run_tasks(run_id, tasks)
        self.control_center.set_snapshot(run_id, snapshot)
        self._remember_run(run_id, snapshot["run"].get("state"))
        if unchanged:  # avoid rebuilding up to 1000 scene items every poll
            self.run_status.setText(f"Run {run_id}: {snapshot['run'].get('state', '')} — {len(tasks)} tasks")
            return
        self._graph_signature = signature
        self.graph_scene.clear()
        positions = {}
        for index, task in enumerate(tasks):
            x, y = (index % 4) * 220, (index // 4) * 90
            positions[task["id"]] = (x, y)
            self.graph_scene.addRect(x, y, 190, 55)
            label = str(task.get("node_id", task["id"]))[:80]
            state = str(task.get("state", ""))[:40]
            self.graph_scene.addText(f"{label}\n{state}").setPos(x + 5, y + 4)
        for edge in dependencies:
            before, after = positions.get(edge["depends_on_id"]), positions.get(edge["task_id"])
            if before and after:
                x1, y1, x2, y2 = before[0] + 95, before[1] + 55, after[0] + 95, after[1]
                self.graph_scene.addLine(x1, y1, x2, y2)
                angle = math.atan2(y2 - y1, x2 - x1)
                for offset in (-0.5, 0.5):
                    self.graph_scene.addLine(x2, y2, x2 - 10 * math.cos(angle + offset),
                                             y2 - 10 * math.sin(angle + offset))
        self.run_status.setText(f"Run {run_id}: {snapshot['run'].get('state', '')} — {len(tasks)} tasks")

    def _remember_run(self, run_id, state):
        label = f"{run_id[:80]}  {str(state or '')[:40]}"
        if run_id in self._recent:
            self._recent[run_id].setText(label)
            return
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, run_id)
        self._recent[run_id] = item
        with QSignalBlocker(self.recent_runs):
            self.recent_runs.insertItem(0, item)
            while self.recent_runs.count() > 20:
                gone = self.recent_runs.takeItem(self.recent_runs.count() - 1)
                self._recent.pop(gone.data(Qt.ItemDataRole.UserRole), None)

    def _run_unavailable(self, run_id, generation):
        if run_id and run_id == self._active_run and generation == self._active_generation:
            self._known_run_state = None
            self._known_run_version = None
            self._known_mock_config = None
            self._known_task_signature = None
            self._update_mock_controls()
            self.run_status.setText("Run unavailable — showing last result")

    def _pick_recent_run(self, item, _previous=None):
        if item is not None:
            self.run_id.setText(item.data(Qt.ItemDataRole.UserRole))
            self._select_run()

    def _review_parked_approval(self):
        row = self.parked_list.currentRow()
        approval_id = self._parked_ids[row] if 0 <= row < len(self._parked_ids) else None
        ids = [item.get("id") for item in self._approval_rows]
        self.show_developer(self.approvals_tab)
        if approval_id in ids:
            self.approvals_list.setCurrentRow(ids.index(approval_id))
            self.approval_decision_status.setText("Approval " + str(approval_id)[:256] + " selected; use Approve or Reject to review and decide.")
        else:
            self.approval_decision_status.setText("Approval " + str(approval_id)[:256] + " is not on this page; use Next approvals to find it.")

    def _render_run_tasks(self, run_id, tasks):
        shown = tasks[:MAX_TASK_ROWS]
        self.task_list.clear()
        for task in shown:
            self.task_list.addItem(f"{str(task.get('node_id', task['id']))[:80]}  {str(task.get('state', ''))[:40]}"
                                   f"  attempts={task.get('attempt_count', 0)}")
        extra = len(tasks) - len(shown)
        self.task_status.setText(f"Tasks: {len(tasks)}" + (f" — showing first {len(shown)}" if extra else ""))
        self._refresh_parked(run_id, tasks)

    def _refresh_parked(self, run_id=None, tasks=None):
        run_id = self._active_run if run_id is None else run_id
        tasks = self._known_tasks if tasks is None else tasks
        self._known_tasks = tasks
        parked = [t for t in tasks if t.get("state") == "awaiting_approval"]
        pending = [a for a in self._approval_rows if a.get("run_id") == run_id and a.get("state") == "requested"]
        current = self.parked_list.currentRow()
        selected = self._parked_ids[current] if 0 <= current < len(self._parked_ids) else None
        with QSignalBlocker(self.parked_list):
            self.parked_list.clear()
            self._parked_ids = []
            for approval in pending:
                self._parked_ids.append(approval["id"])
                self.parked_list.addItem("  ".join(str(approval.get(k, ""))[:256] for k in ("id", "operation", "target")))
            if selected in self._parked_ids:
                self.parked_list.setCurrentRow(self._parked_ids.index(selected))
        self.review_approval_button.setEnabled(self.parked_list.currentRow() >= 0)
        self._update_agent_pane()
        if parked:
            names = ", ".join(str(t.get("node_id", t["id"]))[:40] for t in parked[:5])
            self.parked_status.setText(f"{len(parked)} task(s) awaiting approval ({names}). Approval requests on the loaded "
                                       f"approvals page for this run: {len(pending)}. Select one and review it. The API does not link "
                                       f"a task to its approval, so match by operation/target.")
        else:
            self.parked_status.setText("No tasks awaiting approval.")
