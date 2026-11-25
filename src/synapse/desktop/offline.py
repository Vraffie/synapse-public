"""Offline handling: a slim top alert with 'Retry now', and the set of mutating controls that go dark while the daemon is away.

Controls are disabled with a tooltip that says why; on reconnect exactly the controls this module disabled are restored
(their own enable logic keeps running underneath, so a control that was already disabled stays disabled).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAccessible, QAccessibleEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from synapse.desktop import icons, narrative

OFFLINE_TIP = "Unavailable: the daemon is offline. This works again when the connection returns."


class OfflineBar(QFrame):
    retry_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("offlineBar")
        self.setAccessibleName("Daemon offline")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 4, 12, 4)
        row.setSpacing(10)
        self.icon = QLabel()
        self.icon.setPixmap(icons.state_icon("failed").pixmap(icons.SIZE, icons.SIZE))
        self.icon.setAccessibleName("")
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.retry = QPushButton("Retry now")
        self.retry.setAccessibleName("Retry the daemon connection now")
        self.retry.clicked.connect(self.retry_requested)
        row.addWidget(self.icon)
        row.addWidget(self.label, 1)
        row.addWidget(self.retry)
        self.setVisible(False)

    def show_offline(self, failures: int, delay: float):
        text = narrative.clean(f"Daemon offline. Showing the last data; next attempt in {delay:.0f}s (attempt {failures} failed).", 200)
        first = not self.isVisible()
        self.label.setText(text)
        self.setAccessibleDescription(text)
        self.setVisible(True)
        if first:  # assertive announcement once, without taking keyboard focus
            QAccessible.updateAccessibility(QAccessibleEvent(self, QAccessible.Event.Alert))

    def clear(self):
        self.setVisible(False)
        self.label.setText("")


class MutatingControls:
    """Disables and restores a set of widgets as the connection drops and returns."""

    def __init__(self, widgets):
        self._widgets = list(widgets)
        self._held: list[tuple[object, str]] = []

    def set_offline(self, offline: bool):
        if offline:
            if self._held:
                return
            for widget in self._widgets:
                if widget.isEnabled():
                    self._held.append((widget, widget.toolTip()))
                    widget.setEnabled(False)
                    widget.setToolTip(OFFLINE_TIP)
        else:
            for widget, tip in self._held:
                widget.setEnabled(True)
                widget.setToolTip(tip)
            self._held = []
