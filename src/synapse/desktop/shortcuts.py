"""Keyboard shortcuts: one table drives the actions and the Help > Shortcuts dialog."""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTableWidget, QTableWidgetItem, QVBoxLayout

# (action id, key sequence, description)
SHORTCUTS = (
    ("view_control", "Ctrl+1", "Switch to Control Center"),
    ("view_developer", "Ctrl+2", "Switch to Developer"),
    ("tab_events", "Alt+1", "Developer: Events"),
    ("tab_runs", "Alt+2", "Developer: Runs"),
    ("tab_messages", "Alt+3", "Developer: Messages"),
    ("tab_approvals", "Alt+4", "Developer: Approvals"),
    ("approval_gate", "Ctrl+Shift+A", "Jump to the active approval gate"),
    ("new_run", "Ctrl+N", "Configure a new run"),
    ("find_events", "Ctrl+F", "Search or filter the current view (Events or Activity)"),
    ("send", "Ctrl+Enter", "Send the prompt from the launch bar"),
    ("zoom_reset", "Ctrl+0", "Reset the topology zoom to 100%"),
    ("refresh", "F5", "Refresh now (poll the daemon immediately and reload the approvals page)"),
    ("shortcuts", "F1", "Show this list"),
    ("quit", "Ctrl+Q", "Quit"),
)
# Menu-only actions: deciding is never one keystroke (each opens the decision dialog).
MENU_ONLY = (("approve", "&Approve selected…"), ("reject", "Re&ject selected…"))


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
