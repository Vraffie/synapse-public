"""Single home of the desktop look: palettes, the QSS, app-wide application and the reduced-motion switch.

Fonts are sized in points (never pixels) so they scale with the display. The theme is dark by default and follows a
light system colour scheme when the platform reports one; SYNAPSE_THEME=light|dark overrides.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette

DARK = {"bg": "#090e19", "panel": "#111a2b", "panel2": "#152138", "line": "#27354b", "muted": "#8ea0b9", "text": "#e9f0fb",
        "cyan": "#4bd8e6", "purple": "#aa91ff", "green": "#4cdb9a", "amber": "#ffc36b", "red": "#ff7c8b",
        "node": "#17253b", "node_line": "#45607c", "edge": "#6386a8", "button": "#243451", "button_line": "#405372",
        "gate_bg": "#342a24", "gate_line": "#936c3b", "gate_text": "#e1c8a7"}
LIGHT = {"bg": "#f3f6fb", "panel": "#ffffff", "panel2": "#eef3fa", "line": "#cfd8e6", "muted": "#58677d", "text": "#14202f",
         "cyan": "#0a7f91", "purple": "#6b4fd6", "green": "#1d8a5a", "amber": "#a86a00", "red": "#c23346",
         "node": "#f7faff", "node_line": "#9db0c8", "edge": "#7b93ad", "button": "#e3ebf6", "button_line": "#b5c3d8",
         "gate_bg": "#fff4e3", "gate_line": "#d9a455", "gate_text": "#6a4b16"}
TONES = {"idle": "muted", "running": "cyan", "done": "green", "failed": "red", "waiting": "amber"}


def is_dark(app=None) -> bool:
    forced = os.environ.get("SYNAPSE_THEME", "").lower()
    if forced in ("light", "dark"):
        return forced == "dark"
    try:
        hints = (app or QGuiApplication.instance()).styleHints()
        return hints.colorScheme() != Qt.ColorScheme.Light
    except Exception:
        return True


def palette(dark: bool | None = None) -> dict:
    return DARK if (is_dark() if dark is None else dark) else LIGHT


def reduced_motion() -> bool:
    """True when animation should be off: env SYNAPSE_REDUCED_MOTION / QT_REDUCED_MOTION, or the ui/reduced_motion setting."""
    for name in ("SYNAPSE_REDUCED_MOTION", "QT_REDUCED_MOTION"):
        if os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    try:
        return str(QSettings("synapse", "desktop").value("ui/reduced_motion", "false")).lower() in ("1", "true", "yes", "on")
    except Exception:
        return False


def stylesheet(p: dict | None = None) -> str:
    p = p or palette()
    return f"""
QWidget {{ color: {p['text']}; font-size: 10pt; }}
QMainWindow, QDialog, QWidget#controlCenter, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {p['bg']}; }}
QLabel {{ background: transparent; }}
QFrame#panel, QFrame#metric {{ background: {p['panel']}; border: 1px solid {p['line']}; border-radius: 12px; }}
QFrame#gate {{ background: {p['gate_bg']}; border: 1px solid {p['gate_line']}; border-radius: 10px; }}
QFrame#gate QLabel {{ color: {p['gate_text']}; }}
QFrame#gate QLabel#gateTitle {{ color: {p['amber']}; font-weight: 700; font-size: 11pt; }}
QFrame#agentCard {{ background: {p['panel2']}; border: 1px solid {p['line']}; border-radius: 9px; }}
QFrame#agentCard[selected="true"] {{ border: 2px solid {p['cyan']}; }}
QFrame#message {{ background: {p['panel2']}; border: 1px solid {p['line']}; border-radius: 8px; }}
QLabel#h1 {{ font-size: 15pt; font-weight: 700; }}
QLabel#h2 {{ font-size: 11pt; font-weight: 700; }}
QLabel#eyebrow {{ color: {p['cyan']}; font-size: 8pt; font-weight: 800; letter-spacing: 2px; }}
QLabel#muted, QLabel#caption {{ color: {p['muted']}; }}
QLabel#caption {{ font-size: 8.5pt; }}
QLabel#metricValue {{ font-size: 17pt; font-weight: 700; }}
QLabel#pill {{ border: 1px solid {p['line']}; border-radius: 10px; padding: 2px 9px; color: {p['muted']}; font-size: 8.5pt; }}
QPushButton {{ background: {p['button']}; border: 1px solid {p['button_line']}; border-radius: 8px; padding: 6px 12px; }}
QPushButton:hover {{ border-color: {p['cyan']}; }}
QPushButton:disabled {{ color: {p['muted']}; }}
QPushButton#primary {{ background: #316f8b; border-color: {p['cyan']}; color: #ffffff; }}
QPushButton#danger {{ background: #693445; border-color: #a95668; color: #ffffff; }}
QPushButton#link {{ background: transparent; border: none; color: {p['cyan']}; text-align: left; padding: 2px; }}
QComboBox, QLineEdit, QPlainTextEdit, QListWidget, QTextEdit {{ background: {p['panel2']}; border: 1px solid {p['line']}; border-radius: 6px; padding: 3px; }}
QTabWidget::pane {{ border: 1px solid {p['line']}; border-radius: 6px; }}
QTabBar::tab {{ background: {p['panel']}; padding: 6px 14px; border: 1px solid {p['line']}; border-bottom: none; }}
QTabBar::tab:selected {{ color: {p['cyan']}; }}
QProgressBar {{ background: {p['line']}; border: none; border-radius: 3px; max-height: 6px; }}
QProgressBar::chunk {{ background: {p['cyan']}; border-radius: 3px; }}
QStatusBar, QMenuBar {{ background: {p['panel']}; }}
"""


def apply_theme(app) -> None:
    """Fusion style + palette + QSS for the whole app (the Control Center also carries its own sheet)."""
    dark = is_dark(app)
    p = palette(dark)
    app.setStyle("Fusion")
    pal = QPalette()
    for role, key in ((QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.Base, "panel2"), (QPalette.ColorRole.AlternateBase, "panel"),
                      (QPalette.ColorRole.Button, "button"), (QPalette.ColorRole.WindowText, "text"), (QPalette.ColorRole.Text, "text"),
                      (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.ToolTipBase, "panel"), (QPalette.ColorRole.ToolTipText, "text"),
                      (QPalette.ColorRole.Highlight, "cyan"), (QPalette.ColorRole.PlaceholderText, "muted")):
        pal.setColor(role, QColor(p[key]))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#06101a" if dark else "#ffffff"))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(p))
