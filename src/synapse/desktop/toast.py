"""A slim notification strip above the status bar: one line of text, an optional action (Cancel), no modal.

Used for the 5-second cancel window of a run start. The text is display-safe plain text; the strip takes no room while hidden
and announces itself to assistive technology when it appears.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAccessible, QAccessibleEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from synapse.desktop import icons, narrative


class ToastBar(QFrame):
    action_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("toast")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(10)
        self.icon = QLabel()
        self.icon.setPixmap(icons.state_icon("running").pixmap(icons.SIZE, icons.SIZE))
        self.icon.setAccessibleName("")
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(False)
        self.action = QPushButton("Cancel")
        self.action.setAccessibleName("Cancel the run that is about to start")
        self.action.clicked.connect(self.action_clicked)
        row.addWidget(self.icon)
        row.addWidget(self.label, 1)
        row.addWidget(self.action)
        self.setVisible(False)
        self.setAccessibleName("Notification")

    def show_text(self, text: str, tooltip: str = "", action: str = "Cancel"):
        text = narrative.clean(text, 300)
        first = not self.isVisible()
        self.label.setText(text)
        self.label.setToolTip(narrative.safe_tip(tooltip) if tooltip else "")
        self.action.setText(action)
        self.action.setVisible(bool(action))
        self.setAccessibleDescription(text)
        self.setVisible(True)
        if first:  # announce once when it appears, not on every tick of the countdown
            QAccessible.updateAccessibility(QAccessibleEvent(self, QAccessible.Event.Alert))

    def clear(self):
        self.setVisible(False)
        self.label.setText("")
