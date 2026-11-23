"""Desktop notification for a newly requested approval.

Uses a system-tray message when a tray exists, otherwise flashes the status bar. The text only ever carries
the operation and target (already shown in the approvals list): no ids, digests, reasons or payload.
"""
from __future__ import annotations

from PySide6.QtWidgets import QStyle, QSystemTrayIcon

FLASH_MS = 10000
MAX_FIELD = 80


def _clean(value) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return text[:MAX_FIELD]


def approval_message(requests: list[dict]) -> tuple[str, str]:
    """(title, body) for one or more newly requested approvals."""
    latest = requests[-1]
    detail = f"{_clean(latest.get('operation', ''))} on {_clean(latest.get('target', ''))}"
    if len(requests) == 1:
        return "Approval requested", detail
    return f"{len(requests)} approvals requested", f"Latest: {detail}"


class ApprovalNotifier:
    """tray=None selects automatically; pass False to force the status-bar fallback (used in headless tests)."""

    def __init__(self, window, tray=None):
        self.window = window
        self.messages: list[tuple[str, str, str]] = []  # (channel, title, body), the most recent notifications
        self.tray = None
        if tray is None:
            tray = QSystemTrayIcon.isSystemTrayAvailable()
        if tray:
            self.tray = QSystemTrayIcon(window.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation), window)
            self.tray.setToolTip("Synapse")
            self.tray.show()

    def notify(self, requests: list[dict]):
        title, body = approval_message(requests)
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, FLASH_MS)
            self.messages.append(("tray", title, body))
        else:
            self.window.statusBar().showMessage(f"{title}: {body}", FLASH_MS)
            self.messages.append(("statusbar", title, body))
        del self.messages[:-20]
