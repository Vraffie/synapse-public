"""'Start agent run' form: validation, request building, idempotency and the form widget.

Contract (POST /api/v1/agent-runs): body {repo, base_ref, prompt, allowed_tools, permission_mode, timeout_seconds,
approval_step, profile?}. ``profile`` is the Claude account the operator chose for this run (ids come from
GET /api/v1/profiles). The picker has three states: loading (Start disabled), unavailable (an explicit 404/503: no
``profile`` is sent and the daemon's default account is used, which the confirm summary says) and loaded (the
operator must choose; nothing is preselected and there is never a fallback account). The form validates client-side what it can and never sends bypass/auto permission modes. The
Idempotency-Key is generated per submission and reused while the very same payload is retried after a lost
response; any edit that changes the payload is a new submission with a new key. Nothing here handles tokens
(the Poller's transport loads the operator token) and nothing formats exception text.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import uuid4

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

TOOLS = ("Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit", "Bash")
DEFAULT_TOOLS = ("Read", "Glob", "Grep")
PERMISSION_MODES = ("plan", "acceptEdits", "dontAsk", "manual")  # bypass/auto modes are deliberately absent
DEFAULT_MODE = "plan"
MAX_REPO = 1024
MAX_REF = 256
MAX_PROMPT = 20000
MAX_PROMPT_BYTES = 32768  # the daemon counts UTF-8 bytes as well as characters
MIN_TIMEOUT, MAX_TIMEOUT, DEFAULT_TIMEOUT = 1, 3600, 900
MIN_TTL, MAX_TTL, DEFAULT_TTL = 1, 86400, 3600
MAX_FIELD = 128  # operation / target (the approval binding limit)
RECENT_KEY = "agent/recent_repos"
MAX_RECENT = 8
UNAVAILABLE_TEXT = "Agent runs are not available or not configured on this daemon."
ACTIVE_TEXT = "The daemon refused the start: a run is already active. Try again when it has finished."
REJECTED_TEXT = ("The daemon rejected the request (422). Either a value exceeds the daemon's limit (for example the "
                 "timeout) or a tool is not allowed by the daemon's operator config; adjust the form and retry.")
PROFILE_PROMPT = "Choose a Claude account…"
PROFILE_REQUIRED = "Choose a Claude account for this run."
PROFILE_LOADING = "Loading accounts…"
PROFILE_UNAVAILABLE = "Not available on this daemon (the daemon's default account will be used)"
DEFAULT_ACCOUNT_TEXT = "Claude account: daemon default (no account was chosen)"
_PROFILE = re.compile(r"[A-Za-z0-9._-]{1,64}")  # the daemon's profile id pattern
_REPO_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")  # a registry repository id (ADR 026)
MAX_REPO_IDS = 16
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CREDENTIAL_URL = re.compile(r"://[^/\s]*@")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@+~^-]*")
_TOKEN = re.compile(r"[A-Za-z0-9._:/@+-]{1,128}")


def sanitize_profiles(raw) -> list[dict] | None:
    """Bounded rows {id, enabled, cooldown} from a GET /api/v1/profiles answer; None when it is not understood."""
    rows = raw.get("profiles") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows[:50]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or _PROFILE.fullmatch(row["id"]) is None:
            continue
        remaining = row.get("cooldown_remaining_seconds")
        remaining = float(remaining) if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and remaining > 0 else 0.0
        out.append({"id": row["id"], "enabled": row.get("enabled") is True, "cooldown": min(remaining, 86400.0)})
    return out


def profile_label(row: dict) -> str:
    if not row["enabled"]:
        return f"{row['id']} (disabled: not selectable)"
    if row["cooldown"] > 0:
        minutes = row["cooldown"] / 60
        return f"{row['id']} (cooling down, about {minutes:.0f} min left: not selectable)" if minutes >= 1 else \
            f"{row['id']} (cooling down, {row['cooldown']:.0f} s left: not selectable)"
    return row["id"]


def clean_reason(reason) -> str | None:
    return (_CONTROL.sub("", reason).strip()[:200] or None) if isinstance(reason, str) else None


def prompt_digest(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def default_approval_step(repo: str, prompt: str) -> dict:
    """Prefill: sanitised repo name plus a short prompt hash, so the approver can match what was requested.
    The daemon only accepts [A-Za-z0-9._:/@+-] (1-128 chars) here, so everything else becomes '-'."""
    base = re.sub(r"[^A-Za-z0-9._@+-]", "-", repo.strip().rstrip("/").rsplit("/", 1)[-1]).strip("-") or "repo"
    suffix = ":" + prompt_digest(prompt.strip())[:16]
    return {"operation": "agent_run.start", "target": (base[:MAX_FIELD - len(suffix)] + suffix)[:MAX_FIELD],
            "ttl_seconds": DEFAULT_TTL}


def utf8_len(text: str) -> int | None:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def validate(fields: dict) -> tuple[dict | None, list[str]]:
    """Return (payload, []) or (None, problems). `fields` holds the raw form values."""
    problems = []
    repo_ids = list(fields.get("repo_ids") or ())
    if repo_ids and (len(repo_ids) > MAX_REPO_IDS or len(set(repo_ids)) != len(repo_ids)
                     or any(not isinstance(i, str) or _REPO_ID.fullmatch(i) is None for i in repo_ids)):
        problems.append("Registered repositories must be 1-%d distinct ids." % MAX_REPO_IDS)
    repo = str(fields.get("repo", "")).strip()
    if repo_ids:
        repo = ""  # a run over registered repositories names them by id; the path and base ref fields are not used
    elif not repo:
        problems.append("Repository is required.")
    elif len(repo) > MAX_REPO or _CONTROL.search(repo):
        problems.append("Repository must be a single line of at most %d characters." % MAX_REPO)
    elif _CREDENTIAL_URL.search(repo):
        problems.append("Repository must not embed credentials (user:password@host).")
    ref = str(fields.get("base_ref", "")).strip()
    if repo_ids:
        ref = ""
    elif not ref:
        problems.append("Base ref is required.")
    elif len(ref) > MAX_REF or _REF.fullmatch(ref) is None or ref.startswith("-") or ".." in ref or "//" in ref or ref.endswith((".lock", "/", ".")):
        problems.append("Base ref must be a branch, tag or commit name starting with a letter or digit, without spaces, "
                        "'..', '//' or a trailing '/', '.' or '.lock'.")
    prompt = str(fields.get("prompt", "")).strip()
    if not prompt:
        problems.append("Prompt is required.")
    elif "\x00" in prompt:
        problems.append("Prompt must not contain NUL characters.")
    elif len(prompt) > MAX_PROMPT:
        problems.append("Prompt must be at most %d characters." % MAX_PROMPT)
    elif utf8_len(prompt) is None or utf8_len(prompt) > MAX_PROMPT_BYTES:
        problems.append("Prompt must be valid text of at most %d bytes (UTF-8)." % MAX_PROMPT_BYTES)
    tools = [t for t in TOOLS if t in set(fields.get("allowed_tools", ()))]
    if not tools:
        problems.append("Allow at least one tool.")
    profile = fields.get("profile")
    if profile is None:
        if fields.get("profile_state") == "loading":
            problems.append("The Claude accounts are not loaded yet; wait for the list (or the connection) and retry.")
        elif fields.get("profile_required"):
            problems.append(PROFILE_REQUIRED)
    elif not isinstance(profile, str) or _PROFILE.fullmatch(profile) is None:
        problems.append("Claude account must be 1-64 characters of letters, digits and . _ -")
    mode = fields.get("permission_mode")
    if mode not in PERMISSION_MODES:
        problems.append("Permission mode must be one of: " + ", ".join(PERMISSION_MODES) + ".")
    timeout = fields.get("timeout_seconds")
    if type(timeout) is not int or not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
        problems.append("Timeout must be between %d and %d seconds." % (MIN_TIMEOUT, MAX_TIMEOUT))
    step = fields.get("approval_step")
    if step is not None:
        operation, target, ttl = str(step.get("operation", "")).strip(), str(step.get("target", "")).strip(), step.get("ttl_seconds")
        if _TOKEN.fullmatch(operation) is None:
            problems.append("Approval operation must be 1-%d characters of letters, digits and . _ : / @ + - (no spaces)." % MAX_FIELD)
        if _TOKEN.fullmatch(target) is None:
            problems.append("Approval target must be 1-%d characters of letters, digits and . _ : / @ + - (no spaces)." % MAX_FIELD)
        if type(ttl) is not int or not MIN_TTL <= ttl <= MAX_TTL:
            problems.append("Approval lifetime must be between %d and %d seconds." % (MIN_TTL, MAX_TTL))
        step = {"operation": operation, "target": target, "ttl_seconds": ttl}
    if problems:
        return None, problems
    payload = {"repo": repo, "base_ref": ref, "prompt": prompt, "allowed_tools": tools, "permission_mode": mode,
               "timeout_seconds": timeout, "approval_step": step}
    if repo_ids:
        payload["repo_ids"] = repo_ids
    if profile is not None:
        payload["profile"] = profile
    return payload, []


def wire_payload(payload: dict) -> dict:
    """The POST body. A run over registered repositories (ADR 026) is one task that writes all of them."""
    ids = payload.get("repo_ids")
    if not ids:
        return payload
    task = {"id": "main", "prompt": payload["prompt"], "allowed_tools": payload["allowed_tools"],
            "repos": [{"repo_id": i, "access": "write"} for i in ids]}
    if payload["approval_step"]:
        task["approval_step"] = payload["approval_step"]
    body = {"repos": [{"repo_id": i} for i in ids], "tasks": [task], "permission_mode": payload["permission_mode"],
            "timeout_seconds": payload["timeout_seconds"]}
    if payload.get("profile") is not None:
        body["profile"] = payload["profile"]
    return body


def encode_payload(payload: dict) -> bytes:
    payload = wire_payload(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def summary_text(payload: dict) -> str:
    prompt = payload["prompt"]
    shown = prompt if len(prompt) <= 400 else prompt[:400] + "..."
    where = ([f"Repositories ({len(payload['repo_ids'])}, one task writes all of them): {', '.join(payload['repo_ids'])}",
              "Base ref: each repository's default baseline branch"] if payload.get("repo_ids")
             else [f"Repository: {payload['repo']}", f"Base ref: {payload['base_ref']}"])
    lines = [*where,
             f"Claude account: {payload['profile']} (what you chose)" if payload.get("profile") else DEFAULT_ACCOUNT_TEXT,
             f"Permission mode: {payload['permission_mode']}", f"Allowed tools: {', '.join(payload['allowed_tools'])}",
             f"Timeout: {payload['timeout_seconds']} s", f"Prompt ({len(prompt)} chars, sha256 {prompt_digest(prompt)[:16]}):", shown]
    risky = [t for t in payload["allowed_tools"] if t in ("Edit", "Write", "NotebookEdit", "Bash")]
    if risky:
        lines.append("WARNING: this agent may change files or run commands (" + ", ".join(risky) + ") in its own worktree.")
    step = payload["approval_step"]
    if step:
        lines.append(f"Approval required before start: {step['operation']} / {step['target']} (valid {step['ttl_seconds']} s)")
    else:
        lines.append("No approval step: the agent starts immediately.")
    return "\n".join(lines)


def ask_confirm(parent, title: str, text: str) -> bool:
    """Modal yes/no confirmation. Module-level so tests can replace it."""
    box = QMessageBox(QMessageBox.Icon.Question, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    try:
        return box.exec() == QMessageBox.StandardButton.Yes
    finally:
        box.deleteLater()


def load_recent(settings: QSettings) -> list[str]:
    value = settings.value(RECENT_KEY, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [v for v in value if isinstance(v, str) and 0 < len(v) <= MAX_REPO and not _CONTROL.search(v)
            and not _CREDENTIAL_URL.search(v)][:MAX_RECENT]


def remember_repo(settings: QSettings, repo: str) -> list[str]:
    """Most-recent-first, de-duplicated; only the repo string is stored (never prompts or tokens)."""
    recent = [repo] + [r for r in load_recent(settings) if r != repo]
    recent = recent[:MAX_RECENT]
    settings.setValue(RECENT_KEY, recent)
    settings.sync()
    return recent


@dataclass(frozen=True)
class AgentSubmission:
    payload: dict
    body: bytes
    key: str
    correlation: str


class AgentRunForm(QWidget):
    submit_requested = Signal(object)  # AgentSubmission

    def __init__(self, settings: QSettings | None = None, parent=None):
        super().__init__(parent)
        self.settings = settings if settings is not None else QSettings("Synapse", "Desktop")
        self.repo = QComboBox()
        self.repo.setEditable(True)
        self.repo.lineEdit().setPlaceholderText("Repository path or URL")
        self.repo.addItems(load_recent(self.settings))
        self.repo.setCurrentText("")
        self.base_ref = QLineEdit("main")
        self.repos_list = QListWidget()  # the operator's registered repositories (GET /api/v1/repos); hidden until known
        self.repos_list.setMaximumHeight(96)
        self.repos_list.setToolTip("Tick one or more registered repositories to run one agent over all of them. "
                                   "The Repository and Base ref fields are then not used.")
        self.repos_list.setVisible(False)
        self.profile = QComboBox()
        self._profile_state = "loading"  # loading | unavailable | loaded
        self._default_profile = None  # the operator's default account (default-profile file), preselected once
        self._profile_chosen_by_user = False
        self._profile_rows = []
        self._profile_notice = ""
        self._set_profile_items()
        self.prompt = self._make_prompt()
        self.tool_boxes = {}
        tools_row = QHBoxLayout()
        for tool in TOOLS:
            box = QCheckBox(tool)
            box.setChecked(tool in DEFAULT_TOOLS)
            self.tool_boxes[tool] = box
            tools_row.addWidget(box)
        self.mode = QComboBox()
        self.mode.addItems(PERMISSION_MODES)
        self.mode.setCurrentText(DEFAULT_MODE)
        self.timeout = QSpinBox()
        self.timeout.setRange(MIN_TIMEOUT, MAX_TIMEOUT)
        self.timeout.setValue(DEFAULT_TIMEOUT)
        self.timeout.setSuffix(" s")
        self.require_approval = QCheckBox("Require my approval before the agent starts")
        self.approval_operation = QLineEdit()
        self.approval_target = QLineEdit()
        self.approval_ttl = QSpinBox()
        self.approval_ttl.setRange(MIN_TTL, MAX_TTL)
        self.approval_ttl.setValue(DEFAULT_TTL)
        self.approval_ttl.setSuffix(" s")
        self._approval_edited = False
        for widget in (self.approval_operation, self.approval_target, self.approval_ttl):
            widget.setEnabled(False)
        self.submit_button = QPushButton("Start agent run…")
        self.problems = QLabel("")
        self.problems.setWordWrap(True)
        self.problems.setTextFormat(Qt.TextFormat.PlainText)
        self.status = QLabel("Fill in the form to start an agent run.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.tools_row = tools_row
        self._build_layout()
        self._last = None  # (body, key, correlation) of the submission awaiting its outcome
        self._pending = False
        self._connect_signals()
        self.repos_list.itemChanged.connect(self._repos_changed)
        self._update_submit()

    def _make_prompt(self) -> QPlainTextEdit:
        """Hook: subclasses supply a richer prompt editor."""
        prompt = QPlainTextEdit()
        prompt.setPlaceholderText("What should the agent do?")
        prompt.setMaximumHeight(90)
        return prompt

    def _build_layout(self):
        tools_row = self.tools_row
        form = QFormLayout()
        form.addRow("Repository", self.repo)
        form.addRow("Registered repositories", self.repos_list)
        form.addRow("Claude account", self.profile)
        form.addRow("Base ref", self.base_ref)
        form.addRow("Prompt", self.prompt)
        form.addRow("Allowed tools", tools_row)
        form.addRow("Permission mode", self.mode)
        form.addRow("Timeout", self.timeout)
        form.addRow(self.require_approval)
        form.addRow("Approval operation", self.approval_operation)
        form.addRow("Approval target", self.approval_target)
        form.addRow("Approval valid for", self.approval_ttl)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.submit_button)
        layout.addWidget(self.problems)
        layout.addWidget(self.status)

    def set_repos(self, raw):
        """The daemon's registered repositories ({"repos": [{id, default_baseline_ref, ...}]}); None or junk hides the list."""
        rows = raw.get("repos") if isinstance(raw, dict) else None
        keep = set(self.selected_repo_ids())
        self.repos_list.blockSignals(True)
        self.repos_list.clear()
        for row in rows if isinstance(rows, list) else ():
            rid = row.get("id") if isinstance(row, dict) else None
            if not isinstance(rid, str) or _REPO_ID.fullmatch(rid) is None or self.repos_list.count() >= MAX_REPO_IDS:
                continue
            base = row.get("default_baseline_ref")
            base = base[len("refs/heads/"):] if isinstance(base, str) and base.startswith("refs/heads/") else ""
            item = QListWidgetItem(f"{rid}  ({base})" if base and len(base) <= 80 and base.isprintable() else rid)
            item.setData(Qt.ItemDataRole.UserRole, rid)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if rid in keep else Qt.CheckState.Unchecked)
            self.repos_list.addItem(item)
        self.repos_list.blockSignals(False)
        self.repos_list.setVisible(self.repos_list.count() > 0)
        self._repos_changed()

    def selected_repo_ids(self) -> list[str]:
        return [self.repos_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.repos_list.count())
                if self.repos_list.item(i).checkState() == Qt.CheckState.Checked]

    def _repos_changed(self, *_):
        multi = bool(self.selected_repo_ids())
        self.repo.setEnabled(not multi)
        self.base_ref.setEnabled(not multi)
        self._update_submit()

    def _profile_activated(self, _index: int):
        self._profile_chosen_by_user = True  # the operator picked an entry: the default no longer overrides it

    def set_default_profile(self, profile_id: str | None):
        """The operator's default account: preselected when the list loads and the operator has not chosen another."""
        self._default_profile = profile_id if isinstance(profile_id, str) and _PROFILE.fullmatch(profile_id) else None
        self._set_profile_items()
        self._update_submit()

    def _set_profile_items(self):
        """Rebuild the picker: index 0 is the empty 'choose' entry; unusable profiles are listed but not selectable."""
        chosen = self.profile.currentData() if self.profile.count() else None
        if chosen is None and not self._profile_chosen_by_user:
            chosen = self._default_profile
        self.profile.blockSignals(True)
        self.profile.clear()
        rows = self._profile_rows
        if self._profile_state != "loaded":
            self.profile.addItem(PROFILE_LOADING if self._profile_state == "loading" else PROFILE_UNAVAILABLE, None)
            self.profile.setEnabled(False)
        else:
            self.profile.setEnabled(True)
            self.profile.addItem(PROFILE_PROMPT, None)
            for row in rows:
                self.profile.addItem(profile_label(row), row["id"])
                if not row["enabled"] or row["cooldown"] > 0:
                    item = self.profile.model().item(self.profile.count() - 1)
                    item.setEnabled(False)
                    item.setToolTip("Disabled by the operator" if not row["enabled"] else "In a cooldown; try again later")
                elif row["id"] == chosen:
                    self.profile.setCurrentIndex(self.profile.count() - 1)  # keep an earlier choice that is still usable
        self.profile.blockSignals(False)

    def set_profiles(self, raw):
        """Feed the picker from GET /api/v1/profiles (raw answer). None, or an answer with no usable rows, is the
        explicit 'unavailable' state (the daemon default account); anything not understood is a failed load."""
        rows = None if raw is None else sanitize_profiles(raw)
        if raw is not None and rows is None:
            self.set_profiles_failed("the daemon's accounts answer was not understood")
            return
        chosen = self.profile.currentData()
        self._clear_profile_notice()
        if rows:
            self._profile_state, self._profile_rows = "loaded", rows
        else:
            self._profile_state, self._profile_rows = "unavailable", []
            if chosen is not None:
                self._profile_notice = (f"The daemon no longer lists accounts: your choice '{chosen}' was cleared and the "
                                        "daemon's default account would be used. Check before starting.")
        self._set_profile_items()
        self._update_submit()

    def _clear_profile_notice(self):
        if self._profile_notice and self.status.text() == self._profile_notice:
            self.status.setText("Fill in the form to start an agent run.")
        self._profile_notice = ""

    def set_profiles_failed(self, reason: str = "the daemon could not be reached"):
        """The accounts could not be loaded (not an explicit 404/503): back to loading, Start disabled, reason visible."""
        self._clear_profile_notice()
        self._profile_state, self._profile_rows = "loading", []
        self._profile_notice = f"Claude accounts could not be loaded ({reason}); retrying. Start is disabled until they load."
        self._set_profile_items()
        self._update_submit()

    def _profile_required(self) -> bool:
        return self._profile_state == "loaded"

    def _update_submit(self, *_):
        ready = self._profile_state == "unavailable" or (self._profile_state == "loaded" and self.profile.currentData() is not None)
        self.submit_button.setEnabled(not self._pending and ready)
        if not self._pending and self._profile_notice:
            self.status.setText(self._profile_notice)

    def _connect_signals(self):
        self.profile.activated.connect(self._profile_activated)
        self.profile.currentIndexChanged.connect(self._update_submit)
        self.submit_button.clicked.connect(self.submit)
        self.require_approval.toggled.connect(self._approval_toggled)
        self.repo.currentTextChanged.connect(self._autofill_approval)
        self.prompt.textChanged.connect(self._autofill_approval)
        self.approval_operation.textEdited.connect(self._mark_edited)
        self.approval_target.textEdited.connect(self._mark_edited)

    def _mark_edited(self, _text=""):
        self._approval_edited = True

    def _approval_toggled(self, on):
        for widget in (self.approval_operation, self.approval_target, self.approval_ttl):
            widget.setEnabled(on)
        if on and not self._approval_edited:
            self._autofill_approval()

    def _autofill_approval(self, *_):
        if self._approval_edited or not self.require_approval.isChecked():
            return
        step = default_approval_step(self.repo.currentText(), self.prompt.toPlainText())
        self.approval_operation.setText(step["operation"])
        self.approval_target.setText(step["target"])

    def fields(self) -> dict:
        step = None
        if self.require_approval.isChecked():
            step = {"operation": self.approval_operation.text(), "target": self.approval_target.text(),
                    "ttl_seconds": self.approval_ttl.value()}
        return {"repo": self.repo.currentText(), "base_ref": self.base_ref.text(), "prompt": self.prompt.toPlainText(),
                "allowed_tools": [t for t, box in self.tool_boxes.items() if box.isChecked()],
                "permission_mode": self.mode.currentText(), "timeout_seconds": self.timeout.value(), "approval_step": step,
                "repo_ids": self.selected_repo_ids(),
                "profile": self.profile.currentData(), "profile_required": self._profile_required(),
                "profile_state": self._profile_state}

    def build_submission(self) -> AgentSubmission | None:
        """Validate and return a submission, reusing the key only for a byte-identical retry; shows problems."""
        payload, problems = validate(self.fields())
        self.problems.setText("\n".join(problems))
        if payload is None:
            return None
        body = encode_payload(payload)
        if self._last is None or self._last[0] != body:
            self._last = (body, str(uuid4()), str(uuid4()))
        return AgentSubmission(payload, body, self._last[1], self._last[2])

    def submit(self):
        if self._pending:
            return
        submission = self.build_submission()
        if submission is None:
            self.status.setText("Fix the problems above before starting.")
            return
        if not ask_confirm(self, "Start agent run", summary_text(submission.payload)):
            self.status.setText("Cancelled; nothing was sent.")
            return
        self.submit_requested.emit(submission)

    def set_pending(self, pending: bool):
        self._pending = pending
        self._update_submit()
        if pending:
            self.status.setText("Starting agent run…")

    def finished(self, outcome: str, result: dict | None = None, repo: str = ""):
        """Show the outcome; only an accepted start (or a definitive refusal) ends the key's life."""
        self.set_pending(False)
        if outcome == "accepted":
            self._last = None
            if repo:
                self.repo.clear()
                self.repo.addItems(remember_repo(self.settings, repo))
                self.repo.setCurrentText(repo)
            profile = (result or {}).get("profile")
            self.status.setText(f"Agent run {str((result or {}).get('run_id', ''))[:128]} created "
                                f"(state {str((result or {}).get('state', ''))[:40]}"
                                + (f", account {profile}" if isinstance(profile, str) and _PROFILE.fullmatch(profile) else "") + ").")
        elif outcome == "unavailable":
            self.status.setText(UNAVAILABLE_TEXT)
        elif outcome == "active":
            self.status.setText(ACTIVE_TEXT)
        elif outcome == "rejected":
            reason = clean_reason((result or {}).get("reason"))
            self.status.setText(f"The daemon refused the start (422): {reason}. Nothing was started; pick another account "
                                "or adjust the form." if reason else REJECTED_TEXT)
        elif outcome == "invalid":
            self.status.setText("The daemon's response was not understood; retrying reuses the same request key.")
        else:
            self.status.setText("Start failed or the response was lost; retrying reuses the same request key.")


MAX_COMMIT_MESSAGE = 2000


def valid_commit_message(text: str) -> str | None:
    """The message to send (surrounding whitespace trimmed), or None when it is empty or longer than the daemon accepts."""
    text = text.strip()
    return text if text and len(text) <= MAX_COMMIT_MESSAGE else None


def ask_commit_message(parent) -> str | None:
    """Modal multi-line commit message prompt; None when cancelled. Module-level so tests can replace it.
    The text is returned to the caller and never logged or stored here."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("Commit changes")
    note = QLabel("Commit message for the uncommitted changes in each task worktree of this run "
                  f"(required, at most {MAX_COMMIT_MESSAGE} characters). Your own checkout is not touched and nothing is pushed.")
    note.setWordWrap(True)
    edit = QPlainTextEdit()
    count = QLabel("")
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    ok = buttons.button(QDialogButtonBox.StandardButton.Ok)

    def changed():
        text = edit.toPlainText().strip()
        count.setText(f"{len(text)} / {MAX_COMMIT_MESSAGE}")
        ok.setEnabled(valid_commit_message(text) is not None)
    edit.textChanged.connect(changed)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout = QVBoxLayout(dialog)
    for widget in (note, edit, count, buttons):
        layout.addWidget(widget)
    changed()
    try:
        return valid_commit_message(edit.toPlainText()) if dialog.exec() == QDialog.DialogCode.Accepted else None
    finally:
        dialog.deleteLater()
