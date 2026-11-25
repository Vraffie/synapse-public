"""Single home of the desktop look: design tokens, the QSS, app-wide application and the reduced-motion switch.

Neutral graphite surfaces (dark) or paper (light), hairline 1px borders, 0px corners on panes and 4px on controls.
Colour carries state only (running, complete, waiting, failed) and every state also has an icon and a text label
(see ``STATES``); it is never the only signal. Fonts are sized in points relative to the platform's own UI font, so
they follow the display's font scale and DPI. The theme follows the system colour scheme (dark when the platform
does not report one); SYNAPSE_THEME=light|dark overrides.

The older keys (bg, panel, cyan, ...) stay as aliases of the tokens so views not yet restyled keep working.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette

_DARK_TOKENS = {
    "canvas": "#111214", "surface": "#17181b", "elevated": "#1e2025",
    "border_subtle": "#24262c", "border_strong": "#343740",
    "text_primary": "#e6e8ec", "text_secondary": "#9499a6", "text_muted": "#848996",
    "accent_primary": "#e6e8ec", "on_primary": "#111214", "focus": "#5b8def", "selection": "#252a36",
    "running": "#60a5fa", "running_tint": "#172538", "done": "#4ade80", "done_tint": "#142a1e",
    "waiting": "#fbbf24", "waiting_tint": "#2e2312", "failed": "#f87171", "failed_tint": "#2e1618",
}
_LIGHT_TOKENS = {
    "canvas": "#f6f7f8", "surface": "#ffffff", "elevated": "#f0f2f5",
    "border_subtle": "#e4e6eb", "border_strong": "#cdd1d9",
    "text_primary": "#16181d", "text_secondary": "#555b68", "text_muted": "#666d7a",
    "accent_primary": "#16181d", "on_primary": "#ffffff", "focus": "#2563eb", "selection": "#e8f0fe",
    "running": "#1d4ed8", "running_tint": "#eff6ff", "done": "#15803d", "done_tint": "#f0fdf4",
    "waiting": "#b45309", "waiting_tint": "#fffbeb", "failed": "#b91c1c", "failed_tint": "#fef2f2",
}


def _with_aliases(t: dict, violet: str, edge: str) -> dict:
    return {**t, "bg": t["canvas"], "panel": t["surface"], "panel2": t["elevated"], "line": t["border_subtle"],
            "muted": t["text_secondary"], "text": t["text_primary"], "cyan": t["running"], "purple": violet,
            "green": t["done"], "amber": t["waiting"], "red": t["failed"], "node": t["elevated"],
            "node_line": t["border_strong"], "edge": edge, "button": t["elevated"], "button_line": t["border_strong"],
            "gate_bg": t["waiting_tint"], "gate_line": t["waiting"], "gate_text": t["text_primary"]}


DARK = _with_aliases(_DARK_TOKENS, "#a78bfa", "#6b7080")
LIGHT = _with_aliases(_LIGHT_TOKENS, "#6d28d9", "#8b92a0")

# state -> (palette key of the colour, icon name, text label). Colour is never used alone.
STATES = {
    "idle": ("text_secondary", "circle", "Idle"),
    "running": ("running", "loader", "Running"),
    "done": ("done", "check", "Complete"),
    "waiting": ("waiting", "shield-alert", "Waiting for approval"),
    "failed": ("failed", "x-circle", "Failed"),
}
TONES = {"idle": "muted", "running": "cyan", "done": "green", "failed": "red", "waiting": "amber"}  # legacy

MONO_FAMILIES = ("JetBrains Mono", "SF Mono", "Cascadia Code", "DejaVu Sans Mono", "Liberation Mono", "Menlo", "Consolas")


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


def base_point_size() -> float:
    """The platform UI font's size (never below 9 pt): every size below is a multiple of it, so it follows the user's
    font scale and DPI instead of a fixed 10 pt."""
    try:
        size = QGuiApplication.font().pointSizeF()
    except Exception:
        size = 0.0
    return max(size if size > 0 else 10.0, 9.0)


def mono_family() -> str:
    """The operational monospace face: the first installed of MONO_FAMILIES, else the system fixed font."""
    try:
        installed = set(QFontDatabase.families())
    except Exception:
        installed = set()
    for family in MONO_FAMILIES:
        if family in installed:
            return family
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


def mono_font(size_factor: float = 0.92) -> QFont:
    font = QFont(mono_family())
    font.setPointSizeF(base_point_size() * size_factor)
    font.setStyleHint(QFont.StyleHint.Monospace)
    _tabular(font)
    return font


def _tabular(font: QFont) -> None:
    """Equal-width digits, so 1 s polling updates never make a counter or timer jitter."""
    try:
        font.setFeature(QFont.Tag("tnum"), 1)
    except Exception:
        pass


def stylesheet(p: dict | None = None) -> str:
    p = p or palette()
    base = base_point_size()
    dense, title, h1 = base * 0.92, base * 1.15, base * 1.3
    mono = mono_family()
    return f"""
QWidget {{ color: {p['text_primary']}; font-size: {base:.1f}pt; }}
QMainWindow, QDialog, QWidget#controlCenter, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {p['canvas']}; }}
QLabel {{ background: transparent; }}
QFrame#panel, QFrame#metric {{ background: {p['surface']}; border: 1px solid {p['border_subtle']}; border-radius: 0px; }}
QFrame#gate {{ background: {p['waiting_tint']}; border: 1px solid {p['waiting']}; border-radius: 0px; }}
QFrame#gate QLabel {{ color: {p['text_primary']}; }}
QFrame#gate QLabel#gateTitle {{ color: {p['waiting']}; font-weight: 700; font-size: {title:.1f}pt; }}
QFrame#agentCard {{ background: {p['elevated']}; border: 1px solid {p['border_strong']}; border-radius: 4px; }}
QFrame#agentCard[selected="true"] {{ border: 2px solid {p['focus']}; }}
QFrame#message {{ background: {p['elevated']}; border: 1px solid {p['border_subtle']}; border-radius: 4px; }}
QLabel#h1 {{ font-size: {h1:.1f}pt; font-weight: 600; }}
QLabel#h2 {{ font-size: {title:.1f}pt; font-weight: 600; }}
QLabel#eyebrow {{ color: {p['text_secondary']}; font-size: {dense:.1f}pt; font-weight: 600; }}
QLabel#muted, QLabel#caption {{ color: {p['text_secondary']}; }}
QLabel#caption {{ font-size: {dense:.1f}pt; }}
QLabel#mono, QPlainTextEdit#mono, QTextEdit#mono {{ font-family: "{mono}"; font-size: {dense:.1f}pt; }}
QLabel#metricValue {{ font-size: {title:.1f}pt; font-weight: 600; }}
QLabel#pill {{ border: 1px solid {p['border_strong']}; border-radius: 4px; padding: 1px 6px; color: {p['text_secondary']}; font-size: {dense:.1f}pt; }}
QPushButton {{ background: {p['elevated']}; border: 1px solid {p['border_strong']}; border-radius: 4px; padding: 5px 11px; }}
QPushButton:hover {{ background: {p['selection']}; }}
QPushButton:focus {{ border: 2px solid {p['focus']}; padding: 4px 10px; }}
QPushButton:disabled {{ color: {p['text_muted']}; }}
QPushButton#primary {{ background: {p['accent_primary']}; border-color: {p['accent_primary']}; color: {p['on_primary']}; }}
QPushButton#primary:disabled {{ background: {p['elevated']}; border-color: {p['border_subtle']}; color: {p['text_muted']}; }}
QPushButton#danger {{ background: transparent; border-color: {p['failed']}; color: {p['failed']}; }}
QPushButton#danger:disabled {{ border-color: {p['border_subtle']}; color: {p['text_muted']}; }}
QPushButton#link, QToolButton#link {{ background: transparent; border: none; color: {p['text_primary']}; text-align: left; padding: 2px; text-decoration: underline; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 4px; padding: 3px; }}
QToolButton:hover {{ background: {p['selection']}; }}
QToolButton:focus {{ border: 2px solid {p['focus']}; padding: 2px; }}
QComboBox, QLineEdit, QPlainTextEdit, QListWidget, QTextEdit {{ background: {p['elevated']}; border: 1px solid {p['border_strong']}; border-radius: 4px; padding: 3px; selection-background-color: {p['selection']}; selection-color: {p['text_primary']}; }}
QComboBox:focus, QLineEdit:focus, QPlainTextEdit:focus, QListWidget:focus, QTextEdit:focus {{ border: 2px solid {p['focus']}; padding: 2px; }}
QListWidget::item:selected {{ background: {p['selection']}; color: {p['text_primary']}; }}
QTabWidget::pane {{ border: 1px solid {p['border_subtle']}; border-radius: 0px; }}
QTabBar::tab {{ background: {p['surface']}; padding: 6px 14px; border: 1px solid {p['border_subtle']}; border-bottom: none; border-radius: 0px; color: {p['text_secondary']}; }}
QTabBar::tab:selected {{ color: {p['text_primary']}; background: {p['canvas']}; border-top: 2px solid {p['text_primary']}; }}
QProgressBar {{ background: {p['border_subtle']}; border: none; border-radius: 0px; max-height: 4px; }}
QProgressBar::chunk {{ background: {p['running']}; border-radius: 0px; }}
QStatusBar {{ background: {p['surface']}; border-top: 1px solid {p['border_subtle']}; }}
QStatusBar QLabel, QStatusBar QToolButton, QStatusBar QPushButton {{ font-size: {dense:.1f}pt; }}
QMenuBar {{ background: {p['surface']}; border-bottom: 1px solid {p['border_subtle']}; }}
QMenu {{ background: {p['elevated']}; border: 1px solid {p['border_strong']}; }}
QMenu::item:selected, QMenuBar::item:selected {{ background: {p['selection']}; }}
QToolTip {{ background: {p['elevated']}; color: {p['text_primary']}; border: 1px solid {p['border_strong']}; }}
"""


def apply_theme(app) -> None:
    """Fusion style + palette + QSS for the whole app (the Control Center also carries its own sheet)."""
    dark = is_dark(app)
    p = palette(dark)
    app.setStyle("Fusion")
    font = app.font()
    _tabular(font)
    app.setFont(font)
    pal = QPalette()
    for role, key in ((QPalette.ColorRole.Window, "canvas"), (QPalette.ColorRole.Base, "elevated"), (QPalette.ColorRole.AlternateBase, "surface"),
                      (QPalette.ColorRole.Button, "elevated"), (QPalette.ColorRole.WindowText, "text_primary"), (QPalette.ColorRole.Text, "text_primary"),
                      (QPalette.ColorRole.ButtonText, "text_primary"), (QPalette.ColorRole.ToolTipBase, "elevated"),
                      (QPalette.ColorRole.ToolTipText, "text_primary"), (QPalette.ColorRole.Highlight, "focus"),
                      (QPalette.ColorRole.PlaceholderText, "text_muted")):
        pal.setColor(role, QColor(p[key]))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(p))
