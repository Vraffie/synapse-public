"""Keyboard shortcuts: one table drives the actions and the Help > Shortcuts dialog."""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTableWidget, QTableWidgetItem, QVBoxLayout

# (action id, key sequence, description)
SHORTCUTS = (
    ("refresh", "F5", "Refresh now (poll the daemon immediately and reload the approvals page)"),
    ("tab_approvals", "Ctrl+1", "Go to Approvals"),
    ("tab_runs", "Ctrl+2", "Go to Runs"),
    ("tab_events", "Ctrl+3", "Go to Events"),
    ("tab_messages", "Ctrl+4", "Go to Messages"),
    ("approve", "Ctrl+Shift+A", "Approve the selected approval (opens the decision dialog)"),
    ("reject", "Ctrl+Shift+R", "Reject the selected approval (opens the decision dialog)"),
    ("find_events", "Ctrl+F", "Search events"),
    ("shortcuts", "F1", "Show this list"),
    ("quit", "Ctrl+Q", "Quit"),
)


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.table = QTableWidget(len(SHORTCUTS), 2)
        self.table.setHorizontalHeaderLabels(["Shortcut", "Action"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, (_id, key, description) in enumerate(SHORTCUTS):
            self.table.setItem(row, 0, QTableWidgetItem(key))
            self.table.setItem(row, 1, QTableWidgetItem(description))
        self.table.resizeColumnsToContents()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addWidget(buttons)
        self.resize(640, 360)
