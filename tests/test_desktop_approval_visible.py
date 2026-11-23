"""Layout tests: with the sample fixture, the pending approval card and its decision buttons are inside the visible viewport."""
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


def visible_rect(center):
    scroll = center.findChild(QScrollArea)
    return QRect(scroll.viewport().mapTo(center, QPoint(0, 0)), scroll.viewport().size())


@pytest.mark.parametrize('width,height', SIZES)
def test_gate_card_and_buttons_inside_viewport(app, width, height):
    center = shown(app, width, height)
    try:
        assert center.gate.decidable
        view = visible_rect(center)
        for widget in (center.gate_frame, center.approve_button, center.reject_button, center.gate_status):
            box = QRect(widget.mapTo(center, QPoint(0, 0)), widget.size())
            assert view.contains(box), (widget.objectName(), box, view)
        assert center.approve_button.isEnabled() and center.reject_button.isEnabled()
    finally:
        center.close()


@pytest.mark.parametrize('width,height', SIZES)
def test_no_horizontal_scroll_and_topology_fits(app, width, height):
    center = shown(app, width, height)
    try:
        scroll = center.findChild(QScrollArea)
        assert scroll.horizontalScrollBar().maximum() == 0
        topo = center.topology
        assert topo.verticalScrollBar().maximum() <= 0
        viewport = topo.viewport().rect()
        if topo.horizontalScrollBar().maximum() <= 0:  # otherwise the legibility floor kicks in and the graph scrolls sideways
            for item in topo.node_items.values():
                assert viewport.contains(topo.mapFromScene(item.sceneBoundingRect()).boundingRect())
        else:
            assert width < 1000
        # the page scrolls to the end, so the topology is reachable
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        app.processEvents()
    finally:
        center.close()


def test_decision_buttons_still_fail_closed_without_approval(app):
    center = shown(app, 1100, 800)
    try:
        center.set_approvals([])
        assert not center.approve_button.isEnabled() and not center.reject_button.isEnabled()
    finally:
        center.close()


@pytest.mark.parametrize('factor', [1.2, 1.5, 1.8, 2.0])
def test_no_horizontal_scroll_with_wider_font_at_900(app, factor):
    center = shown(app, 900, 700)
    try:
        scaled(center, factor)
        scroll = center.findChild(QScrollArea)
        assert scroll.horizontalScrollBar().maximum() == 0
        assert center.gate.decidable and center.approve_button.isVisible()
    finally:
        center.close()
