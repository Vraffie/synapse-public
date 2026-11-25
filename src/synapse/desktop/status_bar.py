"""Status bar: only live signals (connection, health, pending approvals).

Each signal is an icon plus a text label (state is never colour alone). Static facts, the daemon socket path (never
the token file) and the API / daemon version, are in ``diagnostics_text()`` (Help -> Diagnostics) and in the
connection indicator's tooltip.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QLabel, QStatusBar, QToolButton

from synapse.desktop import icons, theme
from synapse.desktop.connection import API_VERSION

OFFLINE_AFTER_FAILURES = 3  # reconnecting for the first attempts, then "Daemon offline"


class ConsoleStatusBar(QObject):
    retry_requested = Signal()
    approvals_requested = Signal()

    def __init__(self, bar: QStatusBar, state_dir: Path):
        super().__init__(bar)
        self._socket = Path(state_dir) / "agentd.sock"
        self._version = ""
        self._state = "connecting"
        self.connection_icon = QLabel()
        self.connection = QLabel("Connecting…")
        self.retry = QToolButton()
        self.retry.setObjectName("link")
        self.retry.setText("Retry")
        self.retry.setIcon(icons.icon("refresh"))
        self.retry.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.retry.setVisible(False)
        self.retry.setAccessibleName("Retry connection now")
        self.retry.setToolTip("Poll the daemon again now instead of waiting out the backoff")
        self.retry.clicked.connect(self.retry_requested)
        self.health_icon = QLabel()
        self.health = QLabel("Health: unknown")
        self.pending_button = QToolButton()
        self.pending_button.setObjectName("link")
        self.pending_button.setText("Approvals: 0")
        self.pending_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.pending_button.setIcon(icons.icon("shield-alert"))
        self.pending_button.setAccessibleName("Pending approvals")
        self.pending_button.setToolTip("Open the approvals list")
        self.pending_button.clicked.connect(self.approvals_requested)
        for widget in (self.connection_icon, self.connection, self.retry, self.health_icon, self.health, self.pending_button):
            bar.addPermanentWidget(widget)
        self.connection.setAccessibleName("Connection state")
        self.health.setAccessibleName("Daemon health")
        self._paint_connection("idle")
        self._paint_health("idle")
        self._refresh_tooltip()

    # --- text and icons ---------------------------------------------------------------------------------
    def diagnostics_text(self) -> str:
        return (f"Socket: {self._socket}\nAPI: {API_VERSION}" + (f"\nDaemon: {self._version}" if self._version else "")
                + f"\nConnection: {self.connection.text()}\n{self.health.text()}")

    def _refresh_tooltip(self):
        tip = f"Socket: {self._socket}\nAPI: {API_VERSION}" + (f" (daemon {self._version})" if self._version else "")
        for widget in (self.connection, self.connection_icon):
            widget.setToolTip(tip)

    def _paint(self, label: QLabel, state: str):
        label.setPixmap(icons.state_icon(state).pixmap(icons.SIZE, icons.SIZE))

    def _paint_connection(self, state: str):
        self._paint(self.connection_icon, state)

    def _paint_health(self, state: str):
        self._paint(self.health_icon, state)

    def repaint_icons(self):
        """Re-colour the state icons after a theme change."""
        icons._cache.clear()
        self._paint_connection(self._connection_tone)
        self._paint_health(self._health_tone)
        self.retry.setIcon(icons.icon("refresh"))

    _connection_tone = "idle"
    _health_tone = "idle"

    # --- updates ----------------------------------------------------------------------------------------
    def set_health(self, info):
        status = info.get("status") if isinstance(info, dict) else None
        version = info.get("version") if isinstance(info, dict) else None
        self._version = version if isinstance(version, str) and version else ""
        if isinstance(status, str) and status:
            self.health.setText("Health: OK" if status.lower() == "ok" else f"Health: {status.capitalize()}")
            self._health_tone = "done" if status.lower() == "ok" else "waiting"
        else:
            self.health.setText("Health: unknown")
            self._health_tone = "idle"
        self._paint_health(self._health_tone)
        self._refresh_tooltip()

    def set_connection(self, state: str, failures: int, delay: float):
        self._state = state
        if state == "connected":
            self.connection.setText("Connected")
            self._connection_tone = "done"
            self.retry.setVisible(False)
        else:
            offline = failures >= OFFLINE_AFTER_FAILURES
            self.connection.setText("Daemon offline" if offline else f"Reconnecting ({delay:.0f}s)…")
            self._connection_tone = "failed" if offline else "running"
            self.retry.setVisible(True)
            self.health.setText("Health: unreachable")
            self._health_tone = "failed"
            self._paint_health("failed")
        self._paint_connection(self._connection_tone)
        self._refresh_tooltip()

    def set_pending(self, text: str):
        self.pending_button.setText(f"Approvals: {text}")
        waiting = text.strip() not in ("", "0")
        self.pending_button.setIcon(icons.state_icon("waiting") if waiting else icons.icon("shield-alert"))
