"""Events tab: live timeline with filters (run, type, text) and a redacted JSON detail pane.

Filters only affect what is shown; the bounded buffer (latest MAX_EVENT_ROWS events) keeps receiving
everything. Text search covers the safe top-level fields only, never the payload.
"""
from __future__ import annotations

from collections import deque

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
    QSplitter, QVBoxLayout, QWidget,
)

from synapse.desktop.redaction import redacted_json

MAX_EVENT_ROWS = 1000
ALL_RUNS = "All runs"
ALL_TYPES = "All types"
SEARCH_FIELDS = ("sequence", "type", "aggregate_type", "aggregate_id", "run_id", "occurred_at", "correlation_id", "causation_id")
_SEQ = Qt.ItemDataRole.UserRole


def event_row_text(event: dict) -> str:
    return f"{event['sequence']}  {str(event.get('type', ''))[:128]}  {str(event.get('aggregate_id', ''))[:256]}"


def search_text(event: dict) -> str:
    return " ".join(str(event.get(field, "")) for field in SEARCH_FIELDS if event.get(field) is not None).lower()


class EventsMixin:
    def _build_events_tab(self) -> QWidget:
        self.events = QListWidget()
        self.events.setUniformItemSizes(True)
        self.events_status = QLabel("No events yet.")
        self.event_run_filter = QComboBox()
        self.event_run_filter.addItem(ALL_RUNS)
        self.event_type_filter = QComboBox()
        self.event_type_filter.addItem(ALL_TYPES)
        self.event_search = QLineEdit()
        self.event_search.setPlaceholderText("Search type, aggregate, run, time (not payload)")
        self.event_search.setClearButtonEnabled(True)
        self.event_inspected_run = QPushButton("Inspected run")
        self.event_inspected_run.setToolTip("Show only events of the run inspected in the Runs tab")
        self.event_detail = QPlainTextEdit()
        self.event_detail.setReadOnly(True)
        self.event_detail.setPlaceholderText("Select an event to see its JSON (credential-like fields are redacted).")
        filters = QHBoxLayout()
        filters.addWidget(self.event_run_filter, 2)
        filters.addWidget(self.event_inspected_run)
        filters.addWidget(self.event_type_filter, 2)
        filters.addWidget(self.event_search, 3)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.events)
        splitter.addWidget(self.event_detail)
        layout = QVBoxLayout()
        layout.addWidget(self.events_status)
        layout.addLayout(filters)
        layout.addWidget(splitter)
        widget = QWidget()
        widget.setLayout(layout)
        self.event_run_filter.currentIndexChanged.connect(self._events_filter_changed)
        self.event_type_filter.currentIndexChanged.connect(self._events_filter_changed)
        self.event_search.textChanged.connect(self._events_filter_changed)
        self.event_inspected_run.clicked.connect(self._filter_events_to_inspected_run)
        self.events.currentItemChanged.connect(self._show_event_detail)
        return widget

    def _init_events_state(self):
        self._events_seen = 0
        self._event_buffer = deque()
        self._event_types = set()
        self._event_runs = set()

    def _event_matches(self, event: dict) -> bool:
        run = self.event_run_filter.currentText()
        if run != ALL_RUNS and event.get("run_id") != run:
            return False
        kind = self.event_type_filter.currentText()
        if kind != ALL_TYPES and event.get("type") != kind:
            return False
        needle = self.event_search.text().strip().lower()
        return not needle or needle in search_text(event)

    def _add_event(self, event):
        self._event_buffer.append(event)
        self._events_seen += 1
        self._track_filter_value(self.event_type_filter, self._event_types, event.get("type"))
        self._track_filter_value(self.event_run_filter, self._event_runs, event.get("run_id"))
        if self._event_matches(event):
            self._append_event_item(event)
        while len(self._event_buffer) > MAX_EVENT_ROWS:
            old = self._event_buffer.popleft()
            if self.events.count() and self.events.item(0).data(_SEQ) == old["sequence"]:
                self.events.takeItem(0)
        self._update_events_status()
        self.events.scrollToBottom()

    def _append_event_item(self, event):
        item = QListWidgetItem(event_row_text(event))
        item.setData(_SEQ, event["sequence"])
        if event.get("occurred_at"):
            item.setToolTip(str(event["occurred_at"])[:64])
        self.events.addItem(item)

    def _track_filter_value(self, combo, seen, value):
        if isinstance(value, str) and value and value not in seen and len(seen) < 200:
            seen.add(value)
            with QSignalBlocker(combo):
                combo.addItem(value[:256])

    def _update_events_status(self):
        shown = self.events.count()
        buffered = len(self._event_buffer)
        dropped = self._events_seen - buffered
        filtered = buffered - shown
        self.events_status.setText(f"{self._events_seen} events received — showing latest {shown}"
                                   + (f" ({dropped} older dropped from view)" if dropped else "")
                                   + (f" ({filtered} hidden by filters)" if filtered else ""))

    def _events_filter_changed(self, *_):
        selected = self._selected_event_sequence()
        with QSignalBlocker(self.events):
            self.events.clear()
            for event in self._event_buffer:
                if self._event_matches(event):
                    self._append_event_item(event)
                    if event["sequence"] == selected:
                        self.events.setCurrentRow(self.events.count() - 1)
        if self.events.currentRow() < 0:
            self.event_detail.clear()
        self._update_events_status()
        self.events.scrollToBottom()

    def _filter_events_to_inspected_run(self):
        run = self._active_run
        if not run:
            return
        self._track_filter_value(self.event_run_filter, self._event_runs, run)
        index = self.event_run_filter.findText(run[:256])
        self.event_run_filter.setCurrentIndex(index if index >= 0 else 0)

    def _selected_event_sequence(self):
        item = self.events.currentItem()
        return item.data(_SEQ) if item is not None else None

    def _show_event_detail(self, item, _previous=None):
        sequence = item.data(_SEQ) if item is not None else None
        event = next((e for e in self._event_buffer if e["sequence"] == sequence), None)
        self.event_detail.setPlainText(redacted_json(event) if event is not None else "")
