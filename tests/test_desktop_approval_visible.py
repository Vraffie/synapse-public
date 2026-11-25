"""Layout tests: with the sample fixture, the approval banner is pinned under the telemetry strip with its decision buttons visible,
and nothing overflows horizontally at common sizes and larger fonts."""
import os
import re
import sys
from pathlib import Path

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['SYNAPSE_REDUCED_MOTION'] = '1'
pytest.importorskip('PySide6')
from PySide6.QtCore import QPoint, QRect, QSettings
from PySide6.QtWidgets import QApplication, QScrollArea

from synapse.desktop import theme

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import render_control_center as fixture  # noqa: E402

SIZES = [(1100, 800), (1400, 900), (900, 700)]


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def shown(app, width, height):
    center = fixture.build(QSettings(os.devnull, QSettings.Format.IniFormat))
    center.resize(width, height)
    center.show()
    app.processEvents()
    return center


def scaled(center, factor):
    """Re-style with every point size multiplied: stands in for a wider default font or higher DPI, as on CI."""
    center.setStyleSheet(re.sub(r'(\d+(?:\.\d+)?)pt', lambda m: '%.1fpt' % (float(m[1]) * factor), theme.stylesheet(center.p)))
    center.resize(center.width(), center.height())
    QApplication.processEvents()


def inside(center, widget):
    box = QRect(widget.mapTo(center, QPoint(0, 0)), widget.size())
    return center.rect().contains(box), box


@pytest.mark.parametrize('width,height', SIZES)
def test_banner_and_buttons_inside_window(app, width, height):
    center = shown(app, width, height)
    try:
        assert center.gate.decidable and center.banner.isVisible()
        for widget in (center.gate_frame, center.approve_button, center.reject_button, center.gate_title):
            ok, box = inside(center, widget)
            assert ok, (widget.objectName(), box, center.rect())
        assert center.approve_button.isEnabled() and center.reject_button.isEnabled()
        # pinned: directly under the telemetry strip, above the split panes
        assert center.banner.mapTo(center, QPoint(0, 0)).y() >= center.telemetry.mapTo(center, QPoint(0, 0)).y() + center.telemetry.height()
        assert center.banner.mapTo(center, QPoint(0, 0)).y() < center.main_split.mapTo(center, QPoint(0, 0)).y()
    finally:
        center.close()


@pytest.mark.parametrize('width,height', SIZES)
def test_no_horizontal_overflow_and_graph_fits_or_scrolls(app, width, height):
    center = shown(app, width, height)
    try:
        assert center.minimumSizeHint().width() <= width
        for scroll in center.findChildren(QScrollArea):
            assert scroll.horizontalScrollBar().maximum() == 0, scroll.objectName()
        topo = center.topology
        viewport = topo.viewport().rect()
        if topo.horizontalScrollBar().maximum() <= 0 and topo.verticalScrollBar().maximum() <= 0:
            for item in topo.node_items.values():
                assert viewport.contains(topo.mapFromScene(item.sceneBoundingRect()).boundingRect())
        # panes split about 55 / 45
        sizes = center.main_split.sizes()
        assert 0.45 < sizes[0] / sum(sizes) < 0.65
    finally:
        center.close()


def test_decision_buttons_still_fail_closed_without_approval(app):
    center = shown(app, 1100, 800)
    try:
        center.set_approvals([])
        assert not center.approve_button.isEnabled() and not center.reject_button.isEnabled()
        assert not center.banner.isVisible()  # no pending gate: the banner takes no space
        assert center.banner.height() == 0 or not center.banner.isVisible()
    finally:
        center.close()


@pytest.mark.parametrize('factor', [1.2, 1.5, 1.8, 2.0])
def test_wider_font_keeps_banner_buttons_and_strip_usable_at_900(app, factor):
    center = shown(app, 900, 700)
    try:
        scaled(center, factor)
        assert center.gate.decidable and center.approve_button.isVisible()
        for scroll in center.findChildren(QScrollArea):
            assert scroll.horizontalScrollBar().maximum() == 0
        ok, box = inside(center, center.approve_button)
        assert ok, box
        assert center.telemetry.width() <= center.width()
    finally:
        center.close()
