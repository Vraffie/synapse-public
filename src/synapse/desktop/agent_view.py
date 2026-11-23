"""'Agent' detail pane for the Runs tab (GET /api/v1/runs/{id}/agent) and its cancel control.

All daemon text is untrusted: it is shown as plain text, bounded, and stripped of control characters. The diff
viewer is read-only and monospace. No tokens and no exception text ever reach this pane.
"""
from __future__ import annotations

import re

from PySide6.QtCore import Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout

from synapse.desktop.agent_form import UNAVAILABLE_TEXT

MAX_SUMMARY = 8000
MAX_DIFF = 60000
MAX_DIFF_STAT = 4000
MAX_TOOLS = 50
UNAVAILABLE_STATUSES = (404, 409, 503)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # keeps \t and \n


def clean(text, limit: int) -> str:
    return _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))[:limit]


def sanitize_detail(raw) -> dict | None:
    """Validate and bound an agent detail response; None means 'not understood'."""
    if not isinstance(raw, dict):
        return None
    outcome, summary, tools = raw.get("outcome"), raw.get("summary_text"), raw.get("tool_names", [])
    if (outcome is not None and not isinstance(outcome, str)) or (summary is not None and not isinstance(summary, str)):
        return None
    if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
        return None
    worktree = raw.get("worktree")
    if worktree is not None:
        if not isinstance(worktree, dict) or not isinstance(worktree.get("truncated", False), bool):
            return None
        if any(worktree.get(k) is not None and not isinstance(worktree.get(k), str) for k in ("status", "head", "diff_stat", "diff")):
            return None
        diff = clean(worktree.get("diff") or "", MAX_DIFF + 1)
        worktree = {"status": clean(worktree.get("status") or "", 200), "head": clean(worktree.get("head") or "", 80),
                    "diff_stat": clean(worktree.get("diff_stat") or "", MAX_DIFF_STAT),
                    "diff": diff[:MAX_DIFF], "truncated": bool(worktree.get("truncated", False)) or len(diff) > MAX_DIFF}
    profile = raw.get("profile")  # top-level account id; absent on older daemons
    profile = profile if isinstance(profile, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", profile) else None
    return {"profile": profile, "outcome": None if outcome is None else clean(outcome, 80),
            "summary_text": None if summary is None else clean(summary, MAX_SUMMARY),
            "tool_names": [clean(t, 64) for t in tools[:MAX_TOOLS]], "tools_total": len(tools), "worktree": worktree}


def decision_context(detail: dict | None) -> str | None:
    """Read-only text for the approval decision dialog: worktree status plus the bounded diff."""
    worktree = (detail or {}).get("worktree")
    if not worktree:
        return None
    text = f"Worktree {worktree['status']} head {worktree['head']}\n{worktree['diff_stat']}\n\n{worktree['diff']}"
    return text + ("\n[diff truncated]" if worktree["truncated"] else "")


class AgentDetailPane(QGroupBox):
    cancel_clicked = Signal()
    approve_clicked = Signal()
    reject_clicked = Signal()
    abandon_clicked = Signal()
    remove_clicked = Signal()
    commit_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__("Agent", parent)
        self.status = QLabel("Select a run to see agent details.")
        self.status.setWordWrap(True)
        self.banner = QLabel("This agent run is awaiting your approval before it starts.")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("font-weight: bold;")
        self.approve_button = QPushButton("Approve…")
        self.reject_button = QPushButton("Reject…")
        self.profile = QLabel("")
        self.outcome = QLabel("")
        self.tools = QLabel("")
        self.tools.setWordWrap(True)
        self.worktree = QLabel("")
        self.worktree.setWordWrap(True)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setMaximumHeight(80)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.diff.setMaximumHeight(160)
        self.diff_note = QLabel("")
        self.cancel_button = QPushButton("Cancel agent run…")
        self.cancel_button.setEnabled(False)
        self.abandon_button = QPushButton("Abandon recovering run…")
        self.remove_button = QPushButton("Remove worktree…")
        self.commit_button = QPushButton("Commit changes…")
        self.op_status = QLabel("")
        self.op_status.setWordWrap(True)
        for button in (self.abandon_button, self.remove_button, self.commit_button):
            button.setEnabled(False)
        ops_row = QHBoxLayout()
        for button in (self.abandon_button, self.commit_button, self.remove_button):
            ops_row.addWidget(button)
        ops_row.addStretch(1)
        banner_row = QHBoxLayout()
        banner_row.addWidget(self.banner, 1)
        banner_row.addWidget(self.approve_button)
        banner_row.addWidget(self.reject_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addLayout(banner_row)
        for widget in (self.profile, self.outcome, self.tools, self.worktree, self.summary, self.diff_note, self.diff, self.cancel_button):
            layout.addWidget(widget)
        layout.addLayout(ops_row)
        layout.addWidget(self.op_status)
        self.cancel_button.clicked.connect(self.cancel_clicked)
        self.approve_button.clicked.connect(self.approve_clicked)
        self.reject_button.clicked.connect(self.reject_clicked)
        self.abandon_button.clicked.connect(self.abandon_clicked)
        self.remove_button.clicked.connect(self.remove_clicked)
        self.commit_button.clicked.connect(self.commit_clicked)
        self.set_awaiting_approval(False)
        self.clear()

    def set_awaiting_approval(self, on: bool):
        for widget in (self.banner, self.approve_button, self.reject_button):
            widget.setVisible(on)

    def clear(self, status: str = "Select a run to see agent details."):
        self.status.setText(status)
        self.profile.setText("")
        self.outcome.setText("")
        self.tools.setText("")
        self.worktree.setText("")
        self.summary.setPlainText("")
        self.diff.setPlainText("")
        self.diff_note.setText("")
        self.cancel_button.setEnabled(False)
        for button in (self.abandon_button, self.remove_button, self.commit_button):
            button.setEnabled(False)
        self.op_status.setText("")

    def set_unavailable(self):
        self.clear(UNAVAILABLE_TEXT)

    def set_detail(self, detail: dict):
        self.status.setText("Agent run details")
        self.profile.setText("Claude account: " + detail["profile"] if detail.get("profile") else "")
        self.outcome.setText("Outcome: " + (detail["outcome"] or "not finished yet"))
        names = ", ".join(detail["tool_names"])
        extra = detail["tools_total"] - len(detail["tool_names"])
        self.tools.setText("Tools used: " + (names or "none") + (f" (+{extra} more)" if extra > 0 else ""))
        self.summary.setPlainText(detail["summary_text"] or "")
        worktree = detail["worktree"]
        if worktree is None:
            self.worktree.setText("Worktree: not available")
            self.diff.setPlainText("")
            self.diff_note.setText("")
            return
        self.worktree.setText(f"Worktree: {worktree['status'] or 'unknown'}  head {worktree['head'] or '-'}\n{worktree['diff_stat']}")
        if self.diff.toPlainText() != worktree["diff"]:
            self.diff.setPlainText(worktree["diff"])
        self.diff_note.setText(f"Diff shown: {len(worktree['diff'])} characters"
                               + (" — TRUNCATED (the full diff is not shown)" if worktree["truncated"] else ""))
