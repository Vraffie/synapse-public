"""Design tokens: WCAG AA contrast in both themes, every state has an icon and a label, icons render."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")  # the sqlite-schema CI job has no Qt
from PySide6.QtWidgets import QApplication

from synapse.desktop import icons, theme


def luminance(hex_colour):
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("name", ["dark", "light"])
def test_text_and_state_colours_meet_aa(name):
    p = theme.palette(name == "dark")
    for text in ("text_primary", "text_secondary", "text_muted"):
        for ground in ("canvas", "surface", "elevated"):
            assert contrast(p[text], p[ground]) >= 4.5, (name, text, ground)
    for state in ("running", "done", "waiting", "failed"):
        assert contrast(p[state], p[f"{state}_tint"]) >= 4.5, (name, state, "tint")
        assert contrast(p[state], p["surface"]) >= 4.5, (name, state, "surface")
    assert contrast(p["on_primary"], p["accent_primary"]) >= 4.5
    assert contrast(p["focus"], p["canvas"]) >= 3.0  # non-text indicator


def test_every_state_has_icon_and_text_label():
    assert set(theme.STATES) >= {"idle", "running", "done", "waiting", "failed"}
    for key, icon_name, label in theme.STATES.values():
        assert key in theme.DARK and icon_name in icons.NAMES and label


def test_icons_render_in_both_themes(app):
    for name in icons.NAMES:
        for colour in (theme.DARK["text_secondary"], theme.LIGHT["text_secondary"]):
            pm = icons.pixmap(name, colour)
            assert not pm.isNull() and pm.width() == icons.SIZE * 2
            assert pm.toImage().hasAlphaChannel()


def test_theme_env_override_and_corner_radii(monkeypatch):
    monkeypatch.setenv("SYNAPSE_THEME", "light")
    assert not theme.is_dark()
    monkeypatch.setenv("SYNAPSE_THEME", "dark")
    assert theme.is_dark()
    sheet = theme.stylesheet(theme.DARK)
    assert "border-radius: 12px" not in sheet and "#4bd8e6" not in sheet and "#090e19" not in sheet
    assert "QFrame#panel" in sheet and "border-radius: 0px" in sheet
