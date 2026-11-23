"""Messages tab: threads of the inspected run and the messages of the selected thread (paged, read-only)."""
from __future__ import annotations

import json

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout, QWidget


class MessagesMixin:
    def _build_messages_tab(self) -> QWidget:
        self.threads_status = QLabel("Inspect a run to view its threads.")
        self.threads_list = QListWidget()
        self.messages_status = QLabel("Select a thread to view messages.")
        self.messages_list = QListWidget()
        messages_layout = QVBoxLayout()
        messages_layout.addWidget(self.threads_status)
        messages_layout.addWidget(self.threads_list)
        self.threads_previous = QPushButton("Previous threads")
        self.threads_next = QPushButton("Next threads")
        self.threads_previous.setEnabled(False)
        self.threads_next.setEnabled(False)
        thread_nav = QHBoxLayout()
        thread_nav.addWidget(self.threads_previous)
        thread_nav.addWidget(self.threads_next)
        messages_layout.addLayout(thread_nav)
        messages_layout.addWidget(self.messages_status)
        messages_layout.addWidget(self.messages_list)
        self.messages_previous = QPushButton("Previous messages")
        self.messages_next = QPushButton("Next messages")
        self.messages_previous.setEnabled(False)
        self.messages_next.setEnabled(False)
        message_nav = QHBoxLayout()
        message_nav.addWidget(self.messages_previous)
        message_nav.addWidget(self.messages_next)
        messages_layout.addLayout(message_nav)
        widget = QWidget()
        widget.setLayout(messages_layout)
        self.threads_previous.clicked.connect(lambda: self._navigate_threads(False))
        self.threads_next.clicked.connect(lambda: self._navigate_threads(True))
        self.messages_previous.clicked.connect(lambda: self._navigate_messages(False))
        self.messages_next.clicked.connect(lambda: self._navigate_messages(True))
        self.threads_list.currentItemChanged.connect(self._select_thread)
        return widget

    def _init_messages_state(self):
        self._active_thread = ""
        self._thread_generation = 0
        self._thread_pages = [0]
        self._message_pages = [0]
        self._thread_page_generation = 0
        self._message_page_generation = 0
        self._thread_next_cursor = None
        self._message_next_cursor = None
        self._thread_page_loaded = False
        self._message_page_loaded = False

    def _connect_messages_poller(self):
        self.poller.threads.connect(self._show_threads)
        self.poller.threads_error.connect(self._threads_unavailable)
        self.poller.messages.connect(self._show_messages)
        self.poller.messages_error.connect(self._messages_unavailable)

    def _reset_messages(self, *, loading: bool):
        """Called when the inspected run changes: drop thread/message panes."""
        self._active_thread = ""
        with QSignalBlocker(self.threads_list):
            self.threads_list.clear()
        self.messages_list.clear()
        self.threads_status.setText("Loading threads…" if loading else "Inspect a run to view its threads.")
        self.messages_status.setText("Select a thread to view messages.")

    def _reset_message_paging(self):
        self._thread_pages = [0]
        self._message_pages = [0]
        self._thread_page_generation = self.poller.select_threads_page(0)
        self._message_page_generation = self.poller.select_messages_page(0)
        self._thread_next_cursor = self._message_next_cursor = None
        self._thread_page_loaded = self._message_page_loaded = False
        self.threads_previous.setEnabled(False)
        self.threads_next.setEnabled(False)
        self.messages_previous.setEnabled(False)
        self.messages_next.setEnabled(False)

    def _select_thread(self, item, _previous=None):
        thread_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else ""
        if thread_id == self._active_thread:
            return
        self._active_thread = thread_id
        self.messages_list.clear()
        self.messages_status.setText("Loading messages…" if thread_id else "Select a thread to view messages.")
        self._thread_generation = self.poller.select_thread(thread_id)
        self._message_pages = [0]
        self._message_page_generation = self.poller.select_messages_page(0)
        self._message_next_cursor = None
        self._message_page_loaded = False
        self.messages_previous.setEnabled(False)
        self.messages_next.setEnabled(False)

    def _navigate_threads(self, forward):
        if forward:
            if self._thread_next_cursor is None:
                return
            self._thread_pages.append(self._thread_next_cursor)
        elif len(self._thread_pages) > 1:
            self._thread_pages.pop()
        else:
            return
        self._thread_page_generation = self.poller.select_threads_page(self._thread_pages[-1])
        self._thread_next_cursor = None
        self._thread_page_loaded = False
        self.threads_next.setEnabled(False)
        self.threads_previous.setEnabled(False)
        self.threads_status.setText("Loading threads…")
        with QSignalBlocker(self.threads_list):
            self.threads_list.clear()
        self._select_thread(None)

    def _navigate_messages(self, forward):
        if forward:
            if self._message_next_cursor is None:
                return
            self._message_pages.append(self._message_next_cursor)
        elif len(self._message_pages) > 1:
            self._message_pages.pop()
        else:
            return
        self._message_page_generation = self.poller.select_messages_page(self._message_pages[-1])
        self._message_next_cursor = None
        self._message_page_loaded = False
        self.messages_next.setEnabled(False)
        self.messages_previous.setEnabled(False)
        self.messages_list.clear()
        self.messages_status.setText("Loading messages…")

    def _show_threads(self, run_id, generation, threads, page_generation=None):
        if run_id != self._active_run or generation != self._active_generation or (page_generation is not None and page_generation != self._thread_page_generation):
            return
        if len(threads) > 100 or any(not isinstance(t, dict) or not isinstance(t.get("id"), str) or not t["id"].strip() or len(t["id"]) > 256 or "/" in t["id"] or t["id"] in (".", "..") for t in threads):
            self.threads_status.setText("Threads invalid — showing last result" if self._thread_page_loaded else "Threads invalid — no page loaded")
            self.threads_previous.setEnabled(len(self._thread_pages) > 1)
            return
        with QSignalBlocker(self.threads_list):
            self.threads_list.clear()
            for thread in threads:
                self.threads_list.addItem(f"{thread['id'][:256]}  {str(thread.get('subject', ''))[:256]}")
                item = self.threads_list.item(self.threads_list.count() - 1)
                item.setData(Qt.ItemDataRole.UserRole, thread["id"])
                if thread["id"] == self._active_thread:
                    self.threads_list.setCurrentItem(item)
        if self._active_thread and not any(thread["id"] == self._active_thread for thread in threads):
            self._select_thread(None)
        self._thread_page_loaded = True
        self._thread_next_cursor = threads[-1].get("cursor") if len(threads) == 100 else None
        if type(self._thread_next_cursor) is not int or self._thread_next_cursor <= self._thread_pages[-1]:
            self._thread_next_cursor = None
        self.threads_previous.setEnabled(len(self._thread_pages) > 1)
        self.threads_next.setEnabled(self._thread_next_cursor is not None)
        self.threads_status.setText(f"Threads page {len(self._thread_pages)} — {len(threads)} shown")

    def _threads_unavailable(self, run_id, generation, page_generation=None):
        if run_id and run_id == self._active_run and generation == self._active_generation and (page_generation is None or page_generation == self._thread_page_generation):
            self.threads_status.setText("Threads unavailable — showing last result" if self._thread_page_loaded else "Threads unavailable — no page loaded")
            self.threads_previous.setEnabled(len(self._thread_pages) > 1)

    def _show_messages(self, run_id, generation, thread_id, thread_generation, messages, page_generation=None):
        if (run_id, generation, thread_id, thread_generation) != (self._active_run, self._active_generation, self._active_thread, self._thread_generation) or (page_generation is not None and page_generation != self._message_page_generation):
            return
        if len(messages) > 100 or any(not isinstance(m, dict) or not isinstance(m.get("id"), str) for m in messages):
            self.messages_status.setText("Messages invalid — showing last result" if self._message_page_loaded else "Messages invalid — no page loaded")
            self.messages_previous.setEnabled(len(self._message_pages) > 1)
            return
        self.messages_list.clear()
        for message in messages:
            payload = json.dumps(message.get("payload"), ensure_ascii=True)[:1024]
            self.messages_list.addItem(f"{message['id'][:256]}  {str(message.get('type', ''))[:128]}  {payload}")
        self._message_page_loaded = True
        self._message_next_cursor = messages[-1].get("cursor") if len(messages) == 100 else None
        if type(self._message_next_cursor) is not int or self._message_next_cursor <= self._message_pages[-1]:
            self._message_next_cursor = None
        self.messages_previous.setEnabled(len(self._message_pages) > 1)
        self.messages_next.setEnabled(self._message_next_cursor is not None)
        self.messages_status.setText(f"Messages page {len(self._message_pages)} — {len(messages)} shown")

    def _messages_unavailable(self, run_id, generation, thread_id, thread_generation, page_generation=None):
        if thread_id and (run_id, generation, thread_id, thread_generation) == (self._active_run, self._active_generation, self._active_thread, self._thread_generation) and (page_generation is None or page_generation == self._message_page_generation):
            self.messages_status.setText("Messages unavailable — showing last result" if self._message_page_loaded else "Messages unavailable — no page loaded")
            self.messages_previous.setEnabled(len(self._message_pages) > 1)
