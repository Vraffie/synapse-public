"""Main window: composes the tab views, status bar, menus and shortcuts around one Poller."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QLabel, QMainWindow, QTabWidget, QVBoxLayout, QWidget

from synapse.desktop.control_center import ControlCenter
from synapse.desktop.approvals_view import APPROVALS_TAB_TITLE, ApprovalsMixin
from synapse.desktop.connection import Poller
from synapse.desktop.events_view import EventsMixin
from synapse.desktop.messages_view import MessagesMixin
from synapse.desktop.notifications import ApprovalNotifier
from synapse.desktop.runs_view import RunsMixin
from synapse.desktop.shortcuts import SHORTCUTS, ShortcutsDialog
from synapse.desktop.status_bar import ConsoleStatusBar


class MainWindow(RunsMixin, MessagesMixin, ApprovalsMixin, EventsMixin, QMainWindow):
    pending_changed = Signal(int)

    def __init__(self, state_dir: Path, *, poller: Poller | None = None, notifier: ApprovalNotifier | None = None):
        super().__init__()
        self.setWindowTitle("Synapse")
        self.resize(1280, 860)
        self.health_label = QLabel("Connecting")
        self.connection_detail = QLabel("")
        self.connection_state = "connecting"
        self._last_failures = 0
        self._init_runs_state()
        self._init_messages_state()
        self._init_approvals_state()
        self._init_events_state()
        self.events_tab = self._build_events_tab()
        self.runs_tab = self._build_runs_tab()
        self.messages_tab = self._build_messages_tab()
        self.approvals_tab = self._build_approvals_tab()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.events_tab, "Events")
        self.tabs.addTab(self.runs_tab, "Runs")
        self.tabs.addTab(self.messages_tab, "Messages")
        self.tabs.addTab(self.approvals_tab, APPROVALS_TAB_TITLE)
        self.tabs.setCurrentWidget(self.approvals_tab)  # approvals first: it is what needs the operator
        self.control_center = ControlCenter()
        self.views = QTabWidget()  # top level: Control Center (default) and Developer (the technical tabs)
        self.views.addTab(self.control_center, "Control Center")
        self.views.addTab(self.tabs, "Developer")
        self.views.setCurrentWidget(self.control_center)
        layout = QVBoxLayout()
        layout.addWidget(self.health_label)
        layout.addWidget(self.connection_detail)
        layout.addWidget(self.views)
        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)
        self.console_status = ConsoleStatusBar(self.statusBar(), state_dir)
        self.notifier = notifier or ApprovalNotifier(self)
        self.shortcut_actions = {}
        self._build_menus()
        self.pending_changed.connect(self._show_pending)
        self.poller = poller or Poller(state_dir)
        self.poller.health.connect(self.health_label.setText)
        self.poller.health_info.connect(self.console_status.set_health)
        self.poller.connection.connect(self._connection_changed)
        self.poller.event.connect(self._add_event)
        self._connect_control_center()
        self._connect_runs_poller()
        self._connect_messages_poller()
        self._connect_approvals_poller()
        self.poller.start()

    def show_developer(self, tab=None):
        """Switch to the Developer view (optionally a given technical tab)."""
        if tab is not None:
            self.tabs.setCurrentWidget(tab)
        self.views.setCurrentWidget(self.tabs)

    def _connect_control_center(self):
        cc, poller = self.control_center, self.poller
        poller.health.connect(cc.set_connection)
        poller.event.connect(cc.add_event)
        poller.runs_list.connect(cc.set_runs)
        poller.run_feed.connect(self._feed_shown)
        cc.run_selected.connect(self._cc_pick_run)
        cc.start_requested.connect(self.show_start_dialog)
        cc.developer_requested.connect(lambda: self.show_developer())
        cc.approve_requested.connect(lambda: self.decide_from_control_center("approved"))
        cc.reject_requested.connect(lambda: self.decide_from_control_center("rejected"))
        cc.cancel_requested.connect(self._cancel_agent)
        cc.composer.submit_requested.connect(self._composer_submit)

    def _cc_pick_run(self, run_id):
        self.run_id.setText(run_id)
        self._select_run()

    def _feed_shown(self, run_id, generation, messages):
        if run_id == self._active_run and generation == self._active_generation:
            self.control_center.set_messages(run_id, messages)

    def _build_menus(self):
        handlers = {
            "refresh": self.refresh,
            "tab_approvals": lambda: self.show_developer(self.approvals_tab),
            "tab_runs": lambda: self.show_developer(self.runs_tab),
            "tab_events": lambda: self.show_developer(self.events_tab),
            "tab_messages": lambda: self.show_developer(self.messages_tab),
            "approve": lambda: self.open_decision_dialog("approved"),
            "reject": lambda: self.open_decision_dialog("rejected"),
            "find_events": self._focus_event_search,
            "shortcuts": self.show_shortcuts,
            "quit": self.close,
        }
        titles = {"refresh": "&Refresh", "tab_approvals": "&Approvals", "tab_runs": "&Runs", "tab_events": "&Events",
                  "tab_messages": "&Messages", "approve": "&Approve selected…", "reject": "Re&ject selected…",
                  "find_events": "&Find in events", "shortcuts": "&Shortcuts", "quit": "&Quit"}
        for action_id, key, _description in SHORTCUTS:
            action = QAction(titles[action_id], self)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(lambda _checked=False, handler=handlers[action_id]: handler())
            self.addAction(action)
            self.shortcut_actions[action_id] = action
        menus = self.menuBar()
        file_menu = menus.addMenu("&File")
        file_menu.addAction(self.shortcut_actions["refresh"])
        file_menu.addAction(self.shortcut_actions["quit"])
        go_menu = menus.addMenu("&Go")
        home = QAction("&Control Center", self)
        home.setShortcut(QKeySequence("Ctrl+0"))
        home.triggered.connect(lambda: self.views.setCurrentWidget(self.control_center))
        self.addAction(home)
        go_menu.addAction(home)
        for action_id in ("tab_approvals", "tab_runs", "tab_events", "tab_messages", "find_events"):
            go_menu.addAction(self.shortcut_actions[action_id])
        act_menu = menus.addMenu("&Approvals")
        act_menu.addAction(self.shortcut_actions["approve"])
        act_menu.addAction(self.shortcut_actions["reject"])
        help_menu = menus.addMenu("&Help")
        help_menu.addAction(self.shortcut_actions["shortcuts"])

    def refresh(self):
        """Reload the approvals page and wake the poller (also cuts a reconnect backoff short)."""
        self._approval_page_generation = self.poller.select_approval_page(self._approval_pages[-1])
        self.poller.request_refresh()
        self.statusBar().showMessage("Refreshing…", 2000)

    def show_shortcuts(self):
        dialog = ShortcutsDialog(self)
        self.shortcuts_dialog = dialog
        dialog.exec()

    def _focus_event_search(self):
        self.show_developer(self.events_tab)
        self.event_search.setFocus()
        self.event_search.selectAll()

    def _show_pending(self, count):
        index = self.tabs.indexOf(self.approvals_tab)
        text = self.pending_text()
        self.tabs.setTabText(index, f"{APPROVALS_TAB_TITLE} ({text})" if count else APPROVALS_TAB_TITLE)
        self.console_status.set_pending(text)

    def _connection_changed(self, state, failures, delay):
        was = self.connection_state
        self.connection_state = state
        self.console_status.set_connection(state, failures, delay)
        if state == "connected":
            self.connection_detail.setText(f"Resumed after {self._last_failures} failed attempt(s); event replay continues from the last cursor."
                                           if was == "reconnecting" else "")
        else:
            self._last_failures = failures
            self.connection_detail.setText(f"Daemon unreachable: attempt {failures} failed; next retry in {delay:.0f}s. "
                                           f"Panes show their last result until the connection returns.")

    def closeEvent(self, event):
        self.poller.stop()
        if self.poller.wait(11000):
            event.accept()
        else:
            event.ignore()
