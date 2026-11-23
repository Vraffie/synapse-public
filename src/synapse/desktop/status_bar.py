"""Permanent status-bar fields: daemon socket, health, API version, connection state, pending approvals.

Only display-safe values go in: the socket path (never the token file), the health status word, the API prefix
version and an optional short daemon version string.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QLabel, QStatusBar

from synapse.desktop.connection import API_VERSION


class ConsoleStatusBar:
    def __init__(self, bar: QStatusBar, state_dir: Path):
        self.socket = QLabel(f"Socket: {Path(state_dir) / 'agentd.sock'}")
        self.health = QLabel("Health: unknown")
        self.api = QLabel(f"API: {API_VERSION}")
        self.connection = QLabel("Connection: connecting")
        self.pending = QLabel("Pending approvals: 0")
        for label in (self.socket, self.health, self.api, self.connection, self.pending):
            bar.addPermanentWidget(label)
        self.socket.setToolTip("Private Unix socket the console talks to")

    def set_health(self, info):
        status = info.get("status") if isinstance(info, dict) else None
        self.health.setText(f"Health: {status}" if isinstance(status, str) else "Health: unknown")
        version = info.get("version") if isinstance(info, dict) else None
        self.api.setText(f"API: {API_VERSION}" + (f" (daemon {version})" if isinstance(version, str) and version else ""))

    def set_connection(self, state: str, failures: int, delay: float):
        if state == "connected":
            self.connection.setText("Connection: connected")
        else:
            self.connection.setText(f"Connection: reconnecting (attempt {failures}, retry in {delay:.0f}s)")
            self.health.setText("Health: unreachable")

    def set_pending(self, text: str):
        self.pending.setText(f"Pending approvals: {text}")
