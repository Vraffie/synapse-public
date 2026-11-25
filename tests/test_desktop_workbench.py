"""Workstation layout: toolbar switcher, telemetry strip, approval banner, split panes, drawer, activity filter, no overflow."""
import os
import re
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["SYNAPSE_REDUCED_MOTION"] = "1"
pytest.importorskip("PySide6")
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from synapse.desktop import theme
from synapse.desktop.connection import Poller
from synapse.desktop.control_center import feed_kind
from synapse.desktop.main_window import MainWindow
from synapse.desktop.workbench import DRAWER_COLLAPSED, DRAWER_OPEN

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import render_control_center as fixture  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def sample(app):
    center = fixture.build(QSettings(os.devnull, QSettings.Format.IniFormat))
    center.resize(1280, 812)
    center.show()
    app.processEvents()
    return center


def window(app, tmp_path):
    def fetch(_dir, _endpoint):
        raise OSError("down")
    poller = Poller(tmp_path, fetch=fetch, interval=0.05)
    win = MainWindow(tmp_path, poller=poller)
    win.resize(1280, 860)
    win.show()
    app.processEvents()
    return win


def test_telemetry_strip_is_one_monospace_line_with_the_five_signals(app):
    center = sample(app)
    try:
        text = center.telemetry.text()
        assert re.fullmatch(r"Status: .+  ·  Agents: \d+/\d+  ·  (Checks|Steps): \d+/\d+  ·  Messages: \d+  ·  Approvals: \d+", text), text
        assert center.telemetry.height() == 34
        assert center.telemetry.segments["Status"].font().styleHint().name in ("Monospace", "TypeWriter") or \
            center.telemetry.segments["Status"].font().fixedPitch()
        assert center.telemetry.gates.text() == "Approval needed"  # the sample run has a pending approval
    finally:
        center.close()


def test_banner_is_compact_with_agent_payload_and_actions(app):
    center = sample(app)
    try:
        assert center.banner.isVisible() and center.banner.height() <= 64
        assert center.gate_payload.text()  # monospace payload preview (operation and target)
        assert center.banner.inspect.isVisible() and center.banner.inspect.text() == "Inspect full diff"
        assert center.banner.approve.accessibleName() and center.banner.reject.accessibleName()
        center.set_approvals([])
        app.processEvents()
        assert not center.banner.isVisible()
        assert center.telemetry.gates.text() == "Gates clear"
    finally:
        center.close()


def test_state_is_never_colour_alone(app):
    center = sample(app)
    try:
        assert center.state_badge.text() and not center.state_icon.pixmap().isNull()
        assert center.telemetry.segments["Status"].text() and not center.telemetry.status_icon.pixmap().isNull()
        assert center.telemetry.gates.text() and not center.telemetry.gates_icon.pixmap().isNull()
        # phases carry their status in words
        assert re.search(r"Phase 1 \((complete|active|blocked|pending)\)", center.phases.text())
    finally:
        center.close()


def test_no_emoji_or_pictographs_in_visible_text(app):
    center = sample(app)
    try:
        center.bottom_drawer.set_open(True)
        pictographs = re.compile("[←-⇿⌀-⏿■-➿⬀-⯿\U0001f000-\U0001faff]")
        for label in center.findChildren(QLabel) + center.findChildren(QPushButton):
            assert not pictographs.search(label.text()), (label.objectName(), label.text())
    finally:
        center.close()


def test_drawer_toggles_between_32_and_168(app):
    center = sample(app)
    try:
        drawer = center.bottom_drawer
        assert drawer.height() == DRAWER_COLLAPSED == 32
        drawer.toggle.click()
        assert drawer.height() == DRAWER_OPEN == 168 and drawer.stack.isVisible()
        drawer.tabs.setCurrentIndex(1)
        assert drawer.stack.currentIndex() == 1 and center.roster_cards
        drawer.toggle.click()
        assert drawer.height() == 32 and not drawer.stack.isVisible()
    finally:
        center.close()


def test_activity_filter_selects_decisions_tools_and_errors(app):
    assert feed_kind({"type": "approval.requested"}) == "decision"
    assert feed_kind({"type": "task.approval_gated"}) == "decision"
    assert feed_kind({"type": "agent.usage_observed"}) == "tool"
    assert feed_kind({"type": "task.state_changed", "payload": {"after": "failed"}}) == "error"
    assert feed_kind({"type": "run.created"}) == "other"
    center = sample(app)
    try:
        def shown():
            return [label.text() for i in range(center.feed_box.count()) for label in center.feed_box.itemAt(i).widget().findChildren(QLabel)]
        everything = shown()
        center.filter_buttons["errors"].click()
        errors = shown()
        center.filter_buttons["decisions"].click()
        decisions = shown()
        center.filter_buttons["all"].click()
        assert shown() == everything
        assert len(errors) < len(everything) and len(decisions) < len(everything)
        assert center.filter_buttons["decisions"].text() == "Decisions && Tools"  # '&&' renders one ampersand, not a mnemonic
    finally:
        center.close()


def test_zoom_controls_and_fit(app):
    center = sample(app)
    try:
        topo = center.topology
        base = topo.transform().m11()
        center.zoom_plus.click()
        assert topo.transform().m11() > base
        center.zoom_minus.click()
        center.zoom_minus.click()
        assert topo.transform().m11() < base
        center.zoom_fit.click()
        assert abs(topo.transform().m11() - base) < 1e-6
        for button in (center.zoom_plus, center.zoom_minus, center.zoom_fit):
            assert button.accessibleName() and button.toolTip()
    finally:
        center.close()


def test_window_fits_1280x860_and_switcher_drives_the_views(app, tmp_path):
    win = window(app, tmp_path)
    try:
        assert win.height() == 860 and win.minimumSizeHint().height() <= 860 and win.minimumSizeHint().width() <= 1280
        assert not win.views.tabBar().isVisible()  # the segmented switcher replaces the tab bar
        assert win.views.currentWidget() is win.control_center
        win.control_center.switcher.buttons[1].click()
        assert win.views.currentWidget() is win.tabs and win.dev_switcher.buttons[1].isChecked()
        win.dev_switcher.buttons[0].click()
        assert win.views.currentWidget() is win.control_center and win.control_center.switcher.buttons[0].isChecked()
        win.pending_changed.emit(2)
        assert "2" in win.control_center.switcher.buttons[1].text() and "2 approvals" in win.control_center.switcher.buttons[1].accessibleName()
    finally:
        win.close()


@pytest.mark.parametrize("dark", [True, False])
def test_both_themes_style_the_workbench(app, dark):
    p = theme.palette(dark)
    sheet = theme.stylesheet(p)
    for name in ("QFrame#toolbar", "QFrame#telemetry", "QFrame#pane", "QSplitter::handle", "QPushButton#segment:checked"):
        assert name in sheet
    assert p["canvas"] in sheet and p["border_subtle"] in sheet
