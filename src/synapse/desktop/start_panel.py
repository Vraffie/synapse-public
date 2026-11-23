"""Isolated 'Start run' panel with two modes.

Mock mode drives the existing mock-start endpoint (POST /api/v1/runs/{id}/mock-start) for a run that is already
queued. Agent mode embeds the 'Start agent run' form (agent_form.AgentRunForm) which emits an AgentSubmission for
POST /api/v1/agent-runs. The panel itself is a dumb view: it emits requests and shows the status/enabled state its
owner computes; the owner maps each request to a poller command.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QComboBox, QGroupBox, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from synapse.desktop.agent_form import AgentRunForm

MOCK_FIXTURE = "mock-fixture"


@dataclass(frozen=True)
class StartRequest:
    run_id: str
    kind: str = MOCK_FIXTURE  # the only kind the daemon can start today


class StartRunPanel(QGroupBox):
    start_requested = Signal(object)  # StartRequest (mock mode)
    agent_start_requested = Signal(object)  # agent_form.AgentSubmission (agent mode)

    def __init__(self, parent=None, settings: QSettings | None = None):
        super().__init__("Start run", parent)
        self.target_run = ""
        self.button = QPushButton("Start fixed mock fixture")
        self.button.setEnabled(False)
        self.button.clicked.connect(self._clicked)
        self.status = QLabel("Select a queued run to start the fixed mock fixture.")
        self.status.setWordWrap(True)
        self.kind_label = QLabel("Kind: fixed mock fixture")
        self.mode = QComboBox()
        self.mode.addItems(["Mock fixture (queued run)", "Agent run"])
        self.agent_form = AgentRunForm(settings)
        self.agent_form.submit_requested.connect(self.agent_start_requested)
        mock_page = QWidget()
        mock_layout = QVBoxLayout(mock_page)
        mock_layout.setContentsMargins(0, 0, 0, 0)
        mock_layout.addWidget(self.kind_label)
        mock_layout.addLayout(self._form_row())
        mock_layout.addWidget(self.status)
        self.stack = QStackedWidget()
        self.stack.addWidget(mock_page)
        self.stack.addWidget(self.agent_form)
        self.mode.currentIndexChanged.connect(self.stack.setCurrentIndex)
        layout = QVBoxLayout(self)
        layout.addWidget(self.mode)
        layout.addWidget(self.stack)

    def _form_row(self):
        """Extension point: a richer start form adds its input widgets here, before the button."""
        row = QHBoxLayout()
        row.addWidget(self.button)
        return row

    def set_target(self, run_id: str):
        self.target_run = run_id

    def build_request(self) -> StartRequest | None:
        return StartRequest(self.target_run) if self.target_run else None

    def set_state(self, enabled: bool, status: str):
        self.button.setEnabled(enabled)
        self.status.setText(status)

    def _clicked(self):
        request = self.build_request()
        if request is not None and self.button.isEnabled():
            self.start_requested.emit(request)
