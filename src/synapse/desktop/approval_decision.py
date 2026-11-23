"""Operator-facing approval decision helpers: binding validation, safe messages, token prompt.

The approver token is requested per decision, held only by the modal's line edit and the single
in-flight request, and is never stored, logged or rendered. Server bodies are never displayed.
"""
from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QPlainTextEdit, QVBoxLayout,
)

from synapse.infrastructure.passphrase_approver import read_mode_hint

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_REASON = 4096

MESSAGES = {
    "accepted": "Decision recorded; approvals refreshed.",
    "unauthorized": "Decision refused (HTTP 401): the daemon did not accept the operator or approver credentials.",
    "forbidden": "Decision refused (HTTP 403): the approver token was not accepted or you may not decide this request.",
    "conflict": "Decision rejected (HTTP 409): the request changed, expired or was already decided. Refresh and review it again.",
    "failed": "Decision not confirmed (network or daemon error). Retrying reuses the same idempotency key.",
    "invalid": "Decision response was not understood; refresh to see the current state.",
}


def outcome_for_status(status: int, locked_until: str | None = None) -> str:
    if status == 403 and isinstance(locked_until, str) and re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", locked_until):
        return "locked@" + locked_until
    return {401: "unauthorized", 403: "forbidden", 409: "conflict"}.get(status, "failed")


def message_for_outcome(outcome: object) -> str:
    if isinstance(outcome, str) and outcome.startswith("locked@"):
        return (f"Decision refused: approver locked until {outcome[7:]} UTC after failed attempts. "
                "The operator can reset it with agentctl approver-unlock.")
    return MESSAGES.get(outcome, MESSAGES["failed"]) if isinstance(outcome, str) else MESSAGES["failed"]


def binding_from_row(row: object) -> dict | None:
    """Return the exact binding being decided, or None unless the row is a well-formed REQUESTED approval."""
    if not isinstance(row, dict) or row.get("state") != "requested":
        return None
    approval_id, version = row.get("id"), row.get("state_version")
    if (not isinstance(approval_id, str) or not approval_id.strip() or len(approval_id) > 256
            or "/" in approval_id or approval_id in (".", "..")):
        return None
    if type(version) is not int or not 0 <= version <= 9223372036854775807:
        return None
    digest = row.get("candidate_manifest_sha256")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        return None
    result = {"approval_id": approval_id, "expected_version": version, "candidate_sha256": digest}
    for field in ("operation", "target", "policy_version"):
        value = row.get(field)
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            return None
        result[field] = value
    return result


def summary_text(decision: str, binding: dict) -> str:
    verb = "APPROVE" if decision == "approved" else "REJECT"
    return (f"{verb} approval {binding['approval_id']}\n"
            f"Operation: {binding['operation']}\nTarget: {binding['target']}\n"
            f"Candidate SHA-256: {binding['candidate_sha256']}\nPolicy version: {binding['policy_version']}\n"
            f"Request version: {binding['expected_version']}")


class DecisionDialog(QDialog):
    def __init__(self, parent, decision: str, binding: dict, context: str | None = None,
                 passphrase: bool = False):
        super().__init__(parent)
        self.passphrase = passphrase
        self.setWindowTitle("Approve request" if decision == "approved" else "Reject request")
        self.summary = QLabel(summary_text(decision, binding))
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        label = "Approver passphrase" if passphrase else "Approver token"
        self.token_edit.setPlaceholderText(label)
        self.token_edit.setMaxLength(256 if passphrase else 43)
        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("Optional reason")
        self.reason_edit.setMaxLength(MAX_REASON)
        self.problem = QLabel("")
        form = QFormLayout()
        form.addRow(label, self.token_edit)
        form.addRow("Reason", self.reason_edit)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        self.context_view = None
        if context:  # optional read-only extra evidence (e.g. an agent worktree diff); never editable or sent
            self.context_view = QPlainTextEdit(context)
            self.context_view.setReadOnly(True)
            self.context_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            self.context_view.setMaximumHeight(200)
            layout.addWidget(QLabel("Agent worktree context (read-only):"))
            layout.addWidget(self.context_view)
        layout.addLayout(form)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.token_edit.textChanged.connect(self._validate)
        self.reason_edit.textChanged.connect(self._validate)
        self._validate()

    def _validate(self):
        token, reason = self.token_edit.text(), self.reason_edit.text()
        if self.passphrase and not 16 <= len(token) <= 256:
            problem = "Enter the approver passphrase (16 to 256 characters)."
        elif not self.passphrase and TOKEN_PATTERN.fullmatch(token) is None:
            problem = "Enter the 43-character approver token."
        elif token in reason:
            problem = "The reason must not contain the approver credential."
        else:
            problem = ""
        self.problem.setText(problem)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not problem)

    def take(self) -> tuple[str, str | None]:
        """Return (token, reason) and clear the fields."""
        token, reason = self.token_edit.text(), self.reason_edit.text().strip()
        self.token_edit.clear()
        self.reason_edit.clear()
        return token, reason or None


def prompt_decision(parent, decision: str, binding: dict, context: str | None = None) -> tuple[str, str | None] | None:
    state_dir = getattr(getattr(parent, "poller", None), "state_dir", None)
    # UX hint only; the daemon decides which credential it accepts.
    passphrase = state_dir is not None and read_mode_hint(state_dir) == "passphrase"
    dialog = DecisionDialog(parent, decision, binding, context, passphrase)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.take()
    finally:
        dialog.token_edit.clear()
        dialog.deleteLater()
