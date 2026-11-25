"""Main window: composes the tab views, status bar, menus and shortcuts around one Poller."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QLabel, QMainWindow, QTabWidget, QVBoxLayout, QWidget

from synapse.desktop.agent_form import summary_text
from synapse.desktop.control_center import ControlCenter
from synapse.desktop.approvals_view import APPROVALS_TAB_TITLE, ApprovalsMixin
from synapse.desktop.connection import Poller
from synapse.desktop.events_view import EventsMixin
from synapse.desktop.messages_view import MessagesMixin
from synapse.desktop.notifications import ApprovalNotifier
from synapse.desktop.offline import MutatingControls, OfflineBar
from synapse.default_profile import read_default_profile
from synapse.desktop.runs_view import RunsMixin
from synapse.desktop.shortcuts import MENU_ONLY, SHORTCUTS, ShortcutsDialog
from synapse.desktop.status_bar import ConsoleStatusBar
from synapse.desktop.toast import ToastBar
from synapse.desktop.workbench import SegmentedSwitcher


class MainWindow(RunsMixin, MessagesMixin, ApprovalsMixin, EventsMixin, QMainWindow):
    pending_changed = Signal(int)

    def __init__(self, state_dir: Path, *, poller: Poller | None = None, notifier: ApprovalNotifier | None = None):
        super().__init__()
        self.setWindowTitle("Synapse")
        self.resize(1280, 860)
        self.health_label = QLabel("Connecting")
        self.connection_detail = QLabel("")
        self.cursor_notice = QLabel("")
        self.cursor_notice.setWordWrap(True)
        self.connection_state = "connecting"
        self._last_failures = 0
        self._init_runs_state()
        self._init_messages_state()
        self._init_approvals_state()
        self._init_events_state()
        self.events_tab = self._build_events_tab()
        self.runs_tab = self._build_runs_tab()
        default_profile = read_default_profile(state_dir)  # the operator's default account, for every start form
        self.start_panel.agent_form.set_default_profile(default_profile)
        self.messages_tab = self._build_messages_tab()
        self.approvals_tab = self._build_approvals_tab()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.events_tab, "Events")
        self.tabs.addTab(self.runs_tab, "Runs")
        self.tabs.addTab(self.messages_tab, "Messages")
        self.tabs.addTab(self.approvals_tab, APPROVALS_TAB_TITLE)
        self.tabs.setCurrentWidget(self.approvals_tab)  # approvals first: it is what needs the operator
        self.control_center = ControlCenter()
        self.control_center.composer.set_default_profile(default_profile)
        self.views = QTabWidget()  # top level: Control Center (default) and Developer (the technical tabs)
        self.views.addTab(self.control_center, "Control Center")
        self.views.addTab(self.tabs, "Developer")
        self.views.setCurrentWidget(self.control_center)
        self.views.tabBar().setVisible(False)  # the segmented switcher in each view's toolbar replaces the tab bar
        self.views.setDocumentMode(True)
        self.dev_switcher = SegmentedSwitcher()
        self.dev_switcher.set_current(1)
        self.dev_switcher.switched.connect(self._switch_view)
        self.tabs.setCornerWidget(self.dev_switcher, Qt.Corner.TopRightCorner)
        self.control_center.switcher.switched.connect(self._switch_view)
        self.views.currentChanged.connect(self._views_changed)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.offline_bar = OfflineBar()
        layout.addWidget(self.offline_bar)
        layout.addWidget(self.health_label)
        layout.addWidget(self.connection_detail)
        layout.addWidget(self.cursor_notice)
        for label in (self.health_label, self.connection_detail, self.cursor_notice):
            label.setVisible(False)  # the status bar and the offline alert now carry this; kept for tests and diagnostics
        layout.addWidget(self.views, 1)
        self.toast = ToastBar()
        layout.addWidget(self.toast)
        self.toast.action_clicked.connect(self._cancel_countdown)
        self._countdown_form = None
        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)
        self.console_status = ConsoleStatusBar(self.statusBar(), state_dir)
        self.notifier = notifier or ApprovalNotifier(self)
        self.shortcut_actions = {}
        self._build_menus()
        self.pending_changed.connect(self._show_pending)
        self.console_status.approvals_requested.connect(self._open_approvals_tab)
        self.poller = poller or Poller(state_dir)
        self.poller.health.connect(self.health_label.setText)
        self.poller.health_info.connect(self.console_status.set_health)
        self.console_status.retry_requested.connect(self.poller.request_refresh)
        self.offline_bar.retry_requested.connect(self.poller.request_refresh)
        self.poller.connection.connect(self._connection_changed)
        self.poller.cursor_notice.connect(self.cursor_notice.setText)
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
        cc.start_requested.connect(self._new_run)
        cc.composer.start_countdown.connect(self._composer_countdown)
        cc.composer.start_countdown_ended.connect(self._countdown_ended)
        self.start_panel.agent_form.start_countdown.connect(self._dialog_countdown)
        self.start_panel.agent_form.start_countdown_ended.connect(self._countdown_ended)
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
            "view_control": lambda: self._switch_view(0),
            "view_developer": lambda: self._switch_view(1),
            "tab_events": lambda: self.show_developer(self.events_tab),
            "tab_runs": lambda: self.show_developer(self.runs_tab),
            "tab_messages": lambda: self.show_developer(self.messages_tab),
            "tab_approvals": lambda: self.show_developer(self.approvals_tab),
            "approval_gate": self.jump_to_approval_gate,
            "new_run": self.configure_run,
            "find_events": self._focus_search,
            "send": self._send_prompt,
            "zoom_reset": self.control_center.topology.reset_zoom,
            "refresh": self.refresh,
            "shortcuts": self.show_shortcuts,
            "quit": self.close,
            "approve": lambda: self.open_decision_dialog("approved"),
            "reject": lambda: self.open_decision_dialog("rejected"),
        }
        titles = {"view_control": "&Control Center", "view_developer": "&Developer", "tab_events": "&Events",
                  "tab_runs": "&Runs", "tab_messages": "&Messages", "tab_approvals": "&Approvals",
                  "approval_gate": "Active approval &gate", "new_run": "&New run…", "find_events": "&Find",
                  "send": "&Send prompt", "zoom_reset": "Reset &zoom", "refresh": "&Refresh",
                  "shortcuts": "&Shortcuts", "quit": "&Quit"}
        for action_id, key, _description in SHORTCUTS:
            action = QAction(titles[action_id], self)
            action.setShortcut(QKeySequence(key))
            if action_id == "send":  # the keypad Enter key sends too
                action.setShortcuts([QKeySequence("Ctrl+Return"), QKeySequence("Ctrl+Enter")])
            action.triggered.connect(lambda _checked=False, handler=handlers[action_id]: handler())
            self.addAction(action)
            self.shortcut_actions[action_id] = action
        for action_id, title in MENU_ONLY:
            action = QAction(title, self)
            action.triggered.connect(lambda _checked=False, handler=handlers[action_id]: handler())
            self.shortcut_actions[action_id] = action
        menus = self.menuBar()
        file_menu = menus.addMenu("&File")
        file_menu.addAction(self.shortcut_actions["new_run"])
        file_menu.addAction(self.shortcut_actions["refresh"])
        file_menu.addAction(self.shortcut_actions["quit"])
        go_menu = menus.addMenu("&Go")
        for action_id in ("view_control", "view_developer", "tab_events", "tab_runs", "tab_messages", "tab_approvals",
                          "approval_gate", "find_events", "zoom_reset"):
            go_menu.addAction(self.shortcut_actions[action_id])
        act_menu = menus.addMenu("&Approvals")
        act_menu.addAction(self.shortcut_actions["approve"])
        act_menu.addAction(self.shortcut_actions["reject"])
        dev_menu = menus.addMenu("&Developer")
        mock = QAction("Run Mock &Fixture…", self)
        mock.triggered.connect(self.show_mock_fixture)
        dev_menu.addAction(mock)
        self.mock_fixture_action = mock
        help_menu = menus.addMenu("&Help")
        help_menu.addAction(self.shortcut_actions["shortcuts"])

    def jump_to_approval_gate(self):
        """Ctrl+Shift+A: focus the banner's Approve button; with no decidable gate, open the Approvals list instead."""
        self.views.setCurrentWidget(self.control_center)
        button = self.control_center.approve_button
        if button.isVisible() and button.isEnabled():
            button.setFocus(Qt.FocusReason.ShortcutFocusReason)
        else:
            self.show_developer(self.approvals_tab)

    def configure_run(self):
        """Ctrl+N: open the Configure dialog (the advanced launch options) from either view."""
        self.views.setCurrentWidget(self.control_center)
        self.control_center.composer.configure_button.click()

    def _focus_search(self):
        """Ctrl+F: the Events search in Developer, the Activity filter in Control Center."""
        if self.views.currentWidget() is self.tabs:
            self._focus_event_search()
        else:
            self.control_center.focus_filter()

    def _send_prompt(self):
        if self.views.currentWidget() is self.control_center:
            self.control_center.composer.submit_if_ready()

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

    def _new_run(self):
        """+ New Run: go to the launch bar (the prompt is the start of a run; Configure… holds the advanced options)."""
        self.views.setCurrentWidget(self.control_center)
        self.control_center.composer.prompt.setFocus()

    def _composer_countdown(self, line: str):
        self._countdown_form = self.control_center.composer
        self.toast.show_text(line, self._countdown_detail(self._countdown_form))

    def _dialog_countdown(self, line: str):
        self._countdown_form = self.start_panel.agent_form
        self.toast.show_text(line, self._countdown_detail(self._countdown_form))

    @staticmethod
    def _countdown_detail(form) -> str:
        submission = form._countdown_submission
        return summary_text(submission.payload) if submission is not None else ""

    def _countdown_ended(self):
        self.toast.clear()
        self._countdown_form = None

    def _cancel_countdown(self):
        if self._countdown_form is not None:
            self._countdown_form.cancel_start()

    def show_mock_fixture(self):
        """Developer -> Run Mock Fixture…: reveal the fixture controls and open the start dialog on the mock page."""
        self.set_mock_ui(True)
        self.start_panel.mode.setCurrentIndex(0)
        self.show_start_dialog()

    def _switch_view(self, index: int):
        self.views.setCurrentWidget(self.control_center if index == 0 else self.tabs)

    def _views_changed(self, _index: int):
        on_developer = self.views.currentWidget() is self.tabs
        self.control_center.switcher.set_current(1 if on_developer else 0)
        self.dev_switcher.set_current(1 if on_developer else 0)

    def _open_approvals_tab(self):
        self.show_developer(self.approvals_tab)

    def _show_pending(self, count):
        index = self.tabs.indexOf(self.approvals_tab)
        text = self.pending_text()
        self.tabs.setTabText(index, f"{APPROVALS_TAB_TITLE} ({text})" if count else APPROVALS_TAB_TITLE)
        self.console_status.set_pending(text)
        if not count:
            self.notifier.clear()
        for switcher in (self.control_center.switcher, self.dev_switcher):
            switcher.set_pending(count)

    def _connection_changed(self, state, failures, delay):
        was = self.connection_state
        self.connection_state = state
        self.console_status.set_connection(state, failures, delay)
        offline = state != "connected" and failures >= 1
        if offline:
            self.offline_bar.show_offline(failures, delay)
        else:
            self.offline_bar.clear()
        self._set_offline(offline)
        if state == "connected":
            self.connection_detail.setText(f"Resumed after {self._last_failures} failed attempt(s); event replay continues from the last cursor."
                                           if was == "reconnecting" else "")
        else:
            self._last_failures = failures
            self.connection_detail.setText(f"Daemon unreachable: attempt {failures} failed; next retry in {delay:.0f}s. "
                                           f"Panes show their last result until the connection returns.")

    def _set_offline(self, offline: bool):
        """Mutating controls (Send, Approve, Reject, Stop and the Developer equivalents) are dark while the daemon is away."""
        cc = self.control_center
        cc.composer.set_offline(offline)
        if not hasattr(self, "_mutating"):
            pane = self.agent_pane
            self._mutating = MutatingControls([cc.approve_button, cc.reject_button, cc.stop_button, self.approve_button,
                                               self.reject_button, pane.cancel_button, pane.abandon_button,
                                               pane.remove_button, pane.commit_button])
        self._mutating.set_offline(offline)

    def closeEvent(self, event):
        self.poller.stop()
        if self.poller.wait(11000):
            event.accept()
        else:
            event.ignore()
