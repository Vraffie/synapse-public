"""The activity feed reports how many distinct rows it is not showing, once each, across repeated delivery and reruns."""
import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['SYNAPSE_REDUCED_MOTION'] = '1'
pytest.importorskip('PySide6')
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QLabel

from synapse.desktop.control_center import MAX_FEED, MAX_FEED_BUFFER, ControlCenter


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def rows(n, start=0):
    return [{'id': f'm{i}', 'type': 'ack', 'created_at': f'2026-01-01T{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z'} for i in range(start, start + n)]


def make():
    center = ControlCenter(settings=QSettings(os.devnull, QSettings.Format.IniFormat))
    center.set_run_id('r1')
    return center


def marker(center):
    return [w.text() for w in center.findChildren(QLabel) if 'not shown' in w.text()]


def test_no_marker_when_nothing_dropped(app):
    center = make()
    center.set_messages('r1', rows(MAX_FEED))
    assert center.dropped_count() == 0 and marker(center) == []


def test_count_shown_beyond_window_and_not_double_counted_on_redelivery(app):
    center = make()
    center.set_messages('r1', rows(MAX_FEED + 15))
    assert center.dropped_count() == 15 and marker(center) == ['15 older updates not shown.']
    center.set_messages('r1', rows(MAX_FEED + 15))  # repeated poll / reconnect re-delivers the same rows
    assert center.dropped_count() == 15


def test_count_includes_buffer_trimmed_rows_and_survives_redelivery(app):
    center = make()
    total = MAX_FEED_BUFFER + 100
    center.set_messages('r1', rows(total))
    assert center.dropped_count() == total - MAX_FEED
    center.set_messages('r1', rows(total))  # full replay after a reconnect
    assert center.dropped_count() == total - MAX_FEED
    center.set_messages('r1', rows(10, total))
    assert center.dropped_count() == total + 10 - MAX_FEED


def test_events_count_and_count_resets_on_run_change(app):
    center = make()
    center.set_messages('r1', rows(MAX_FEED))
    center.add_event({'sequence': 1, 'type': 'task.state_changed', 'run_id': 'r1', 'occurred_at': '2027-01-01T00:00:00+00:00', 'payload': {}})
    assert center.dropped_count() == 1 and marker(center) == ['1 older update not shown.']
    center.set_run_id('r2')
    assert center.dropped_count() == 0 and marker(center) == []
