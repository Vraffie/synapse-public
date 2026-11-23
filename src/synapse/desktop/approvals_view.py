"""Approvals tab: the operator's primary workflow.

Pending-approval badge, auto-selection of the newest requested approval, a notification for each newly
requested approval, paging, and the existing token/reason decision dialog (approval_decision).
"""
from __future__ import annotations

import json
from uuid import uuid4

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout, QWidget

from synapse.desktop import approval_decision, narrative

APPROVALS_TAB_TITLE = "Approvals"


def pending_rows(rows) -> list[dict]:
    return [row for row in rows if isinstance(row, dict) and row.get("state") == "requested"]


class ApprovalsMixin:
    def _build_approvals_tab(self) -> QWidget:
        self.approvals_status = QLabel("Loading approvals")
        self.approvals_list = QListWidget()
        approvals_layout = QVBoxLayout()
        approvals_layout.addWidget(self.approvals_status)
        approvals_layout.addWidget(self.approvals_list)
        self.approve_button = QPushButton("Approve selected…")
        self.reject_button = QPushButton("Reject selected…")
        self.approve_button.setEnabled(False)
        self.reject_button.setEnabled(False)
        decision_row = QHBoxLayout()
        decision_row.addWidget(self.approve_button)
        decision_row.addWidget(self.reject_button)
        approvals_layout.addLayout(decision_row)
        self.approval_decision_status = QLabel("Select a requested approval to approve or reject it.")
        self.approval_decision_status.setWordWrap(True)
        approvals_layout.addWidget(self.approval_decision_status)
        self.approvals_previous = QPushButton("Previous approvals")
        self.approvals_next = QPushButton("Next approvals")
        self.approvals_previous.setEnabled(False)
        self.approvals_next.setEnabled(False)
        approvals_nav = QHBoxLayout()
        approvals_nav.addWidget(self.approvals_previous)
        approvals_nav.addWidget(self.approvals_next)
        approvals_layout.addLayout(approvals_nav)
        widget = QWidget()
        widget.setLayout(approvals_layout)
        self.approve_button.clicked.connect(lambda: self._decide_selected("approved"))
        self.reject_button.clicked.connect(lambda: self._decide_selected("rejected"))
        self.approvals_list.currentItemChanged.connect(lambda *_: self._update_decision_controls())
        self.approvals_previous.clicked.connect(lambda: self._navigate_approvals(False))
        self.approvals_next.clicked.connect(lambda: self._navigate_approvals(True))
        return widget

    def _init_approvals_state(self):
        self._approval_pages = [""]
        self._approval_page_generation = 0
        self._approval_next_cursor = None
        self._approval_page_loaded = False
        self._approval_rows = []
        self._decision_intents = {}
        self._decision_pending = None
        self._dialog_open = False
        self._seen_approval_ids = set()
        self._approvals_primed = False  # the first successful page is the backlog: it is not announced
        self.pending_count = 0
        self.pending_more = False

    def _connect_approvals_poller(self):
        self.poller.approvals.connect(self._show_approvals)
        self.poller.cc_approvals.connect(self.control_center.set_approvals)
        self.poller.approval_decided.connect(self._decision_finished)
        self.poller.approvals_error.connect(self._approvals_unavailable)

    def _navigate_approvals(self, forward):
        if forward:
            if self._approval_next_cursor is None:
                return
            self._approval_pages.append(self._approval_next_cursor)
        elif len(self._approval_pages) > 1:
            self._approval_pages.pop()
        else:
            return
        self._approval_page_generation = self.poller.select_approval_page(self._approval_pages[-1])
        self._approval_next_cursor = None
        self._approval_page_loaded = False
        self.approvals_list.clear()
        self._approval_rows = []
        self._update_decision_controls()
        self.approvals_next.setEnabled(False)
        self.approvals_previous.setEnabled(False)
        self.approvals_status.setText("Loading approvals…")

    def _show_approvals(self, approvals, page_generation):
        if page_generation != self._approval_page_generation:
            return
        if (len(approvals) > 100 or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not item["id"].strip() or len(item["id"]) > 256 for item in approvals)):
            self.approvals_status.setText("Approvals invalid — showing last result" if self._approval_page_loaded else "Approvals invalid — no page loaded")
            self.approvals_previous.setEnabled(len(self._approval_pages) > 1)
            return
        ids = [item["id"] for item in approvals]
        if ids != sorted(set(ids)) or (ids and ids[0] <= self._approval_pages[-1]):
            self.approvals_status.setText("Approvals invalid — showing last result" if self._approval_page_loaded else "Approvals invalid — no page loaded")
            self.approvals_previous.setEnabled(len(self._approval_pages) > 1)
            return
        # A row with a mistyped run_id/state is kept but neutralised, so one bad row cannot abort this slot or the gate.
        approvals = [{**item, **{key: "" for key in ("run_id", "state") if key in item and not isinstance(item[key], str)}}
                     for item in approvals]
        selected = self._selected_approval_id()
        with QSignalBlocker(self.approvals_list):
            self.approvals_list.clear()
            for item in approvals:
                self.approvals_list.addItem("  ".join(str(item.get(key, ""))[:256] for key in ("id", "state", "operation", "target")))
            self._approval_rows = list(approvals)
            if selected in ids:
                self.approvals_list.setCurrentRow(ids.index(selected))
        fresh = [row for row in pending_rows(approvals) if row["id"] not in self._seen_approval_ids]
        first_page = len(self._approval_pages) == 1
        self._seen_approval_ids.update(ids)
        self._select_newest_pending(selected, fresh if self._approvals_primed else [])
        self._update_decision_controls()
        self._refresh_parked()
        self._approval_page_loaded = True
        self._approval_next_cursor = ids[-1] if len(ids) == 100 else None
        self.approvals_previous.setEnabled(len(self._approval_pages) > 1)
        self.approvals_next.setEnabled(self._approval_next_cursor is not None)
        self.approvals_status.setText(f"Approvals page {len(self._approval_pages)} — {len(approvals)} shown")
        self._set_pending(len(pending_rows(approvals)), self._approval_next_cursor is not None)
        if self._approvals_primed and first_page and fresh:
            self.notifier.notify(fresh)
        self._approvals_primed = True

    def _select_newest_pending(self, selected, fresh):
        """Point the list at the newest requested approval unless the operator is mid-decision.

        The current row is kept while it is still requested, except when a new request just arrived.
        """
        if self._dialog_open or self._decision_pending is not None:
            return
        pending = pending_rows(self._approval_rows)
        if not pending:
            return
        current = next((row for row in self._approval_rows if row["id"] == selected), None)
        if fresh:
            target = fresh[-1]["id"]
        elif current is not None and current.get("state") == "requested":
            return
        else:
            target = pending[-1]["id"]
        self.approvals_list.setCurrentRow([row["id"] for row in self._approval_rows].index(target))

    def _set_pending(self, count: int, more: bool):
        self.pending_count, self.pending_more = count, more
        self.pending_changed.emit(count)

    def pending_text(self) -> str:
        return f"{self.pending_count}{'+' if self.pending_more else ''}"

    def open_decision_dialog(self, decision: str):
        """Keyboard entry point: show the Approvals tab and open the same dialog the buttons use."""
        self.show_developer(self.approvals_tab)
        if self._selected_binding() is None:
            self.approval_decision_status.setText("Select a requested approval first.")
            return
        self._decide_selected(decision)

    def decide_from_control_center(self, decision: str):
        """Gate panel Approve/Reject: act on the exact approval the gate shows, via the same token dialog."""
        row = self.control_center.gate.row
        target = row.get("id") if isinstance(row, dict) and row.get("state") == "requested" else None
        if target is None:
            self.control_center.set_gate_note("That request is no longer waiting for you.")
            return
        binding = approval_decision.binding_from_row(row)
        if binding is None:
            self.control_center.set_gate_note("This request is incomplete, so it cannot be decided from here.")
            return
        if self._decision_pending is not None:
            self.control_center.set_gate_note("A decision is already being submitted.")
            return
        # Decide from the gate's own row (same endpoint and fields as the Approvals list), so the button never depends on
        # the Developer tab's separate poll. A listed row with the same id must carry the identical binding, else fail closed.
        listed = next((r for r in self._approval_rows if r.get("id") == target), None)
        if listed is not None and approval_decision.binding_from_row(listed) != binding:
            self.control_center.set_gate_note("This request changed; wait a moment and try again. Nothing was submitted.")
            return
        ids = [r.get("id") for r in self._approval_rows]
        if target in ids:
            self.approvals_list.setCurrentRow(ids.index(target))

        def current():
            now = self.control_center.gate.row
            return approval_decision.binding_from_row(now) if isinstance(now, dict) and now.get("id") == target else None
        self._decide_binding(decision, binding, current)

    def _selected_approval_id(self):
        row = self.approvals_list.currentRow()
        return self._approval_rows[row].get("id") if 0 <= row < len(self._approval_rows) else None

    def _selected_binding(self):
        row = self.approvals_list.currentRow()
        return approval_decision.binding_from_row(self._approval_rows[row]) if 0 <= row < len(self._approval_rows) else None

    def _update_decision_controls(self):
        enabled = self._selected_binding() is not None and self._decision_pending is None
        self.approve_button.setEnabled(enabled)
        self.reject_button.setEnabled(enabled)

    def _decide_selected(self, decision):
        self._decide_binding(decision, self._selected_binding(), self._selected_binding)

    def _decide_binding(self, decision, binding, current):
        """Run the token dialog for exactly `binding`; `current()` re-reads it after the modal, and any change aborts."""
        if binding is None or self._decision_pending is not None:
            return
        self._dialog_open = True
        try:
            context = self._agent_context_for(binding["approval_id"])
            entry = (approval_decision.prompt_decision(self, decision, binding, context) if context
                     else approval_decision.prompt_decision(self, decision, binding))
        finally:
            self._dialog_open = False
        if entry is None:
            return
        token, reason = entry
        entry = None
        try:
            # The modal runs an event loop; refuse to submit if the row changed underneath it.
            if current() != binding or self._decision_pending is not None:
                self.approval_decision_status.setText("The approval changed while the dialog was open; nothing was submitted.")
                return
            intent_id = (decision, reason, tuple(sorted(binding.items())))
            intent = self._decision_intents.get(intent_id)
            if intent is None:
                body = json.dumps({"decision": decision, "expected_version": binding["expected_version"],
                                   "operation": binding["operation"], "target": binding["target"],
                                   "candidate_sha256": binding["candidate_sha256"],
                                   "policy_version": binding["policy_version"], "reason": reason},
                                  ensure_ascii=True).encode("utf-8")
                intent = (str(uuid4()), body)
                self._decision_intents[intent_id] = intent
            key, body = intent
            if self.poller.request_approval_decision(binding["approval_id"], key, str(uuid4()), body, token):
                self._decision_pending = (binding["approval_id"], intent_id)
                self.approval_decision_status.setText("Submitting decision…")
                self._update_decision_controls()
            else:
                self.approval_decision_status.setText("Another command is in progress; try again shortly.")
        finally:
            token = None

    def _decision_finished(self, approval_id, outcome):
        pending = self._decision_pending
        if pending is None or pending[0] != approval_id:
            return
        self._decision_pending = None
        if outcome != "failed":  # only a lost/unknown response keeps its key for a replay-safe retry
            self._decision_intents.pop(pending[1], None)
        self.approval_decision_status.setText(approval_decision.message_for_outcome(outcome))
        self.control_center.set_gate_note(narrative.decision_outcome_text(outcome))
        if outcome == "accepted":
            self._approval_page_generation = self.poller.select_approval_page(self._approval_pages[-1])
        self._update_decision_controls()

    def _approvals_unavailable(self, page_generation):
        if page_generation != self._approval_page_generation:
            return
        self.approvals_status.setText("Approvals unavailable — showing last result" if self._approval_page_loaded else "Approvals unavailable — no page loaded")
        self.approvals_previous.setEnabled(len(self._approval_pages) > 1)
