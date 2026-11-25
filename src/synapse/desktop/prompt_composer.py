"""Prompt composer: a docked, always-reachable prompt box for the Control Center.

It is an AgentRunForm (same validation, payload building, confirmation summary and Idempotency-Key reuse) with a
plain-text editor, live character/byte counters and the repo/options controls arranged compactly. Prompt text is never
logged, never put in a status line or notification, and never stored (only the repo name is remembered, as before).
"""
from __future__ import annotations

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QGuiApplication, QTextDocumentFragment
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from synapse.desktop import icons

from synapse.desktop.agent_form import MAX_PROMPT, MAX_PROMPT_BYTES, AgentRunForm, utf8_len, validate

PASTE_CAP = 100 * 1024  # characters kept from one paste or file; anything beyond is dropped with a message
FILE_CAP = 256 * 1024  # bytes read from a dropped file
PLACEHOLDER = "Describe the task. Enter sends, Shift+Enter adds a line."
HINT = "Enter sends. Shift+Enter adds a new line. A start can be cancelled for 5 seconds."
BAR_HEIGHT = 52


class AutoLabel(QLabel):
    """A caption that takes no room while it is empty."""

    def setText(self, text: str):
        super().setText(text)
        self.setVisible(bool(text))


def normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_text_file(path: str) -> tuple[str | None, str]:
    """Read a dropped file as UTF-8 text: (text, '') or (None, reason). Binary, oversized-beyond-cap and non-UTF-8 files are refused."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(FILE_CAP + 1)
    except OSError:
        return None, "That file could not be read."
    if b"\x00" in data[:8192] or b"\x00" in data:
        return None, "That looks like a binary file, so it was not added."
    truncated = len(data) > FILE_CAP
    data = data[:FILE_CAP]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if not truncated or exc.start < len(data) - 4:
            return None, "That file is not UTF-8 text, so it was not added."
        text = data[:exc.start].decode("utf-8")  # cut mid-character at the cap
    return text, "The file was larger than 256 KiB; only the start was read." if truncated else ""


class PromptEdit(QPlainTextEdit):
    send_requested = Signal()
    notice = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText(PLACEHOLDER)
        self.setTabChangesFocus(True)
        self.setAcceptDrops(True)
        self.setAccessibleName("Prompt")
        self.setUndoRedoEnabled(True)

    def canInsertFromMimeData(self, source: QMimeData) -> bool:
        return source.hasText() or source.hasHtml() or source.hasUrls()

    def insertFromMimeData(self, source: QMimeData):
        """Single entry for paste, drop and tests: plain text only, normalised newlines, NUL rejected, large input capped."""
        text, note = None, ""
        local = [u.toLocalFile() for u in source.urls() if u.isLocalFile()] if source.hasUrls() else []
        if local:
            text, note = read_text_file(local[0])
            if text is None:
                self.notice.emit(note)
                return
        elif source.hasText():
            text = source.text()
        elif source.hasHtml():
            text = QTextDocumentFragment.fromHtml(source.html()).toPlainText()
        if not text:
            return
        if "\x00" in text:
            self.notice.emit("That text contains a NUL character, so it was not added.")
            return
        text = normalize_text(text)
        if len(text) > PASTE_CAP:
            text = text[:PASTE_CAP]
            note = "That was very large: only the first 100 KiB was added. It is still over the limit, so shorten it before sending."
        self.notice.emit(note)
        self.textCursor().insertText(text)
        self.ensureCursorVisible()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)  # Shift+Enter: a new line
                return
            self.send_requested.emit()  # Enter and Ctrl+Enter send
            event.accept()
            return
        super().keyPressEvent(event)


class PromptComposer(AgentRunForm):
    def _make_prompt(self) -> QPlainTextEdit:
        edit = PromptEdit()
        edit.setMinimumHeight(36)
        edit.setMaximumHeight(72)
        return edit

    def _build_layout(self):
        """The pinned launch bar: prompt | account | repository | Configure… | Send (52 px), with one caption line that only
        takes room when it has something to say. The advanced options live in the Configure dialog."""
        self.setObjectName("composer")
        self.submit_button.setText("Send")
        self.submit_button.setObjectName("primary")
        self.submit_button.setIcon(icons.icon("send", "#ffffff", "#ffffff"))
        self.submit_button.setAccessibleName("Send the prompt and start a run")
        self.paste_button = QPushButton("Paste from clipboard")
        self.counter = AutoLabel("")
        self.counter.setObjectName("caption")
        self.hint = QLabel(HINT)
        self.hint.setObjectName("caption")
        self.hint.setVisible(False)
        self.notice_label = AutoLabel("")
        self.notice_label.setObjectName("caption")
        self.notice_label.setWordWrap(True)
        for name in ("problems", "status"):
            old = getattr(self, name)
            new = AutoLabel()
            new.setObjectName("caption")
            new.setWordWrap(True)
            new.setTextFormat(Qt.TextFormat.PlainText)
            setattr(self, name, new)
            old.deleteLater()
        self.status.setText("")
        self.options_toggle = QToolButton()  # kept for the Configure toggle API; the options live in the dialog
        self.options_toggle.setCheckable(True)
        self.options = QWidget()
        form = QFormLayout(self.options)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Registered repositories", self.repos_list)
        form.addRow("Base ref", self.base_ref)
        form.addRow("Allowed tools", self.tools_row)
        form.addRow("Permission mode", self.mode)
        form.addRow("Timeout", self.timeout)
        form.addRow(self.require_approval)
        form.addRow("Approval operation", self.approval_operation)
        form.addRow("Approval target", self.approval_target)
        form.addRow("Approval valid for", self.approval_ttl)
        self.options.setVisible(False)
        self.options_toggle.toggled.connect(self._options_toggled)
        self.configure_button = QPushButton("Configure…")
        self.configure_button.setIcon(icons.icon("sliders"))
        self.configure_button.setToolTip("Base ref, tools, permission mode, timeout, approval step, registered repositories (Ctrl+N)")
        self.configure_button.setAccessibleName("Configure the run")
        self.configure_dialog = None
        self.profile.setMinimumWidth(150)
        self.profile.setAccessibleName("Claude account")
        self.repo.setMinimumWidth(180)
        self.repo.setAccessibleName("Repository")
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        bar.addWidget(self.prompt, 1)
        bar.addWidget(self.profile)
        bar.addWidget(self.repo)
        bar.addWidget(self.configure_button)
        bar.addWidget(self.submit_button)
        bar.addWidget(self.cancel_start_button)
        self.bar_widget = QWidget()
        self.bar_widget.setLayout(bar)
        self.bar_widget.setMinimumHeight(BAR_HEIGHT - 12)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.bar_widget)
        for label in (self.counter, self.notice_label, self.problems, self.status):
            layout.addWidget(label)
        self.configure_button.clicked.connect(self.configure)

    def configure(self):
        """Open the full Configure dialog: the options form plus a summary of what the bar will send."""
        if self.configure_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("Configure run")
            dialog.setModal(False)
            column = QVBoxLayout(dialog)
            self.configure_summary = QLabel("")
            self.configure_summary.setTextFormat(Qt.TextFormat.PlainText)
            self.configure_summary.setWordWrap(True)
            column.addWidget(self.configure_summary)
            column.addWidget(self.options)
            row = QHBoxLayout()
            row.addWidget(self.paste_button)
            row.addStretch(1)
            close = QPushButton("Close")
            close.clicked.connect(dialog.close)
            row.addWidget(close)
            self.configure_start = QPushButton("Start")
            self.configure_start.setObjectName("primary")
            self.configure_start.clicked.connect(self._configure_start)
            row.addWidget(self.configure_start)
            column.addLayout(row)
            self.configure_dialog = dialog
        self.options.setVisible(True)
        chars, size = self.counts()
        self.configure_summary.setText(f"The prompt in the bar ({chars:,} characters) is sent with these settings. "
                                       f"Account: {self.profile.currentText() or 'none chosen'}. Repository: {self.repo.currentText() or 'none chosen'}.")
        self.configure_start.setEnabled(self.can_send())
        self.configure_dialog.show()
        self.configure_dialog.raise_()
        self.configure_dialog.activateWindow()

    def _configure_start(self):
        self.configure_dialog.close()
        self.submit_if_ready()

    def _connect_signals(self):
        super()._connect_signals()
        self.paste_button.clicked.connect(self.paste_clipboard)
        self.prompt.send_requested.connect(self.submit_if_ready)
        self.prompt.notice.connect(self.notice_label.setText)
        self.prompt.textChanged.connect(self.update_state)
        self.repo.currentTextChanged.connect(self.update_state)
        self.repos_list.itemChanged.connect(self.update_state)
        for box in self.tool_boxes.values():
            box.toggled.connect(self.update_state)
        for widget in (self.base_ref, self.approval_operation, self.approval_target):
            widget.textChanged.connect(self.update_state)
        self.mode.currentTextChanged.connect(self.update_state)
        self.profile.currentIndexChanged.connect(self.update_state)
        self.require_approval.toggled.connect(self.update_state)
        self.update_state()

    def _options_toggled(self, on):
        self.options.setVisible(on)
        self.options_toggle.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)

    # ---- state -------------------------------------------------------------------------------------
    def counts(self) -> tuple[int, int]:
        text = self.prompt.toPlainText()
        return len(text), utf8_len(text) or 0

    def over_limit(self) -> bool:
        chars, size = self.counts()
        return chars > MAX_PROMPT or size > MAX_PROMPT_BYTES

    def can_send(self) -> bool:
        payload, _problems = validate(self.fields())
        return payload is not None and not self._pending and not self._counting

    def update_state(self, *_):
        chars, size = self.counts()
        over = self.over_limit()
        near = over or chars > MAX_PROMPT * 0.8 or (size or 0) > MAX_PROMPT_BYTES * 0.8
        self.counter.setText((f"{chars:,} / {MAX_PROMPT:,} characters · {size:,} / {MAX_PROMPT_BYTES:,} bytes"
                              + ("  — too long: shorten it to send" if over else "")) if near else "")
        self.counter.setProperty("over", over)
        self.counter.style().unpolish(self.counter)
        self.counter.style().polish(self.counter)
        self.submit_button.setEnabled(self.can_send())

    def set_profiles(self, raw):
        super().set_profiles(raw)
        self.update_state()

    def set_pending(self, pending: bool):
        super().set_pending(pending)
        self.update_state()

    def paste_clipboard(self):
        mime = QGuiApplication.clipboard().mimeData()
        if mime is None or not (mime.hasText() or mime.hasHtml()):
            self.notice_label.setText("The clipboard has no text to paste.")
            return
        self.prompt.setFocus()
        self.prompt.insertFromMimeData(mime)

    def submit(self):
        if not self._pending:
            self._submitted_text = self.prompt.toPlainText()
        super().submit()

    def submit_if_ready(self):
        if self.submit_button.isEnabled():
            self.submit()

    def finished(self, outcome: str, result: dict | None = None, repo: str = ""):
        super().finished(outcome, result, repo)
        if outcome == "accepted":
            if self.prompt.toPlainText() == getattr(self, "_submitted_text", None):
                self.prompt.clear()
                self.status.setText("Sent. Following the new run.")
            else:  # the operator kept typing while the send was pending: never discard that newer text
                self.status.setText("Sent. Your newer text was kept.")
            self._submitted_text = None
            self.notice_label.setText("")
        else:
            self.status.setText(self.status.text().replace("Agent run ", "Run "))
        self.update_state()
