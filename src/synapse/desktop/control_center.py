"""Control Center: the default, plain-language home view. A dumb view over daemon data pushed in by MainWindow.

It never talks to the daemon. MainWindow feeds it validated rows (runs list, run snapshot, approvals, agent detail,
messages, events) and turns its signals (run picked, Start run, Approve/Reject, Stop, node selected) into the existing
poller commands. Nothing is invented: absent data shows an honest empty state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from synapse.desktop import control_model, icons, narrative, theme
from synapse.desktop.prompt_composer import PromptComposer
from synapse.desktop.topology_view import TopologyView
from synapse.desktop.workbench import ApprovalBanner, Drawer, SegmentedSwitcher, TelemetryStrip, hairline

MAX_FEED = 60
MAX_FEED_BUFFER = 400
MAX_MESSAGE_IDS = 20000
MAX_DROPPED_KEYS = 20000  # trimmed feed keys remembered so a re-delivered row is not counted as dropped twice
NO_RUN = "No run selected"
FILTERS = (("all", "All"), ("decisions", "Decisions & Tools"), ("errors", "Errors"))
BAD_AFTER = {"failed", "rejected", "expired", "revoked", "cancelled", "canceled", "blocked", "timed_out"}
RUN_TONE = {"running": "running", "completed": "done", "succeeded": "done", "failed": "failed", "cancelled": "failed",
            "waiting_for_approval": "waiting", "paused": "waiting"}


def feed_kind(event) -> str:
    """Which Activity filter an event belongs to: decision (approvals), tool (agent work), error, or other."""
    kind = event.get("type") if isinstance(event, dict) else ""
    kind = kind if isinstance(kind, str) else ""
    payload = event.get("payload") if isinstance(event, dict) and isinstance(event.get("payload"), dict) else {}
    after = payload.get("after")
    if "refused" in kind or "failed" in kind or (isinstance(after, str) and after in BAD_AFTER):
        return "error"
    if kind.startswith("approval.") or kind.startswith("task.approval_"):
        return "decision"
    if kind.startswith("agent.") or kind.startswith("task_attempt."):
        return "tool"
    return "other"


def stamp_key(value) -> tuple:
    """Sort key from a timestamp: parsed instant first (naive times are taken as UTC); unparseable sorts oldest."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (1, moment.timestamp(), "")
    except Exception:
        return (0, 0.0, str(value))


def fmt_time(value) -> str:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except Exception:
        return ""


def fmt_stamp(value) -> str:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().strftime("%b %d %H:%M")
    except Exception:
        return ""


def run_choice_label(run: dict) -> str:
    """'Running · 5 steps · Nov 20 14:03': friendly text only; the id lives in the item's tooltip and data."""
    counts = run.get("task_counts") if isinstance(run.get("task_counts"), dict) else {}
    steps = sum(v for v in counts.values() if type(v) is int)
    parts = [narrative.run_label(run.get("state"))]
    if steps:
        parts.append(f"{steps} step{'s' if steps != 1 else ''}")
    stamp = fmt_stamp(run.get("created_at"))
    if stamp:
        parts.append(stamp)
    pending = run.get("pending_approvals")
    if type(pending) is int and pending:
        parts.append("needs approval")
    return " · ".join(parts)


def _pane(title: str, trailing: QWidget | None = None):
    """A pane of the split workspace: full-bleed surface, a 30 px header with a title, no border of its own (the splitter's
    1 px handles are the only lines between panes)."""
    frame = QFrame()
    frame.setObjectName("pane")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    head = QFrame()
    head.setObjectName("paneHead")
    head.setFixedHeight(30)
    top = QHBoxLayout(head)
    top.setContentsMargins(12, 0, 8, 0)
    heading = QLabel(title)
    heading.setObjectName("h2")
    top.addWidget(heading)
    top.addStretch(1)
    if trailing is not None:
        top.addWidget(trailing)
    layout.addWidget(head)
    body = QVBoxLayout()
    body.setContentsMargins(12, 8, 12, 8)
    layout.addLayout(body, 1)
    return frame, body


def swatch(colour: str, dashed: bool = False) -> QPixmap:
    """A 22x8 sample of an edge style for the legend: colour AND line style (dashed / solid) carry the meaning, with a label."""
    pm = QPixmap(22, 8)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    pen = QPen(QColor(colour), 2)
    if dashed:
        pen.setStyle(Qt.PenStyle.DashLine)
    painter.setPen(pen)
    painter.drawLine(0, 4, 22, 4)
    painter.end()
    return pm


def _label(text="", name=None, wrap=True, rich=False):
    """Labels are PlainText unless they deliberately carry HTML (callers then escape every dynamic part)."""
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    label.setText(text)
    label.setWordWrap(wrap)
    if name:
        label.setObjectName(name)
    return label


class RosterCard(QFrame):
    """A clickable roster card. Emits `picked(task_id)`; no closure captures the Control Center, so there is no reference cycle
    for the cyclic GC to free on a non-GUI thread."""
    picked = Signal(str)

    def __init__(self, task_id: str):
        super().__init__()
        self.task_id = task_id

    def mousePressEvent(self, event):
        self.picked.emit(self.task_id)


class DetailsDrawer(QDialog):
    def __init__(self, parent, text: str):
        super().__init__(parent)
        self.setWindowTitle("Technical details")
        self.resize(520, 360)
        layout = QVBoxLayout(self)
        layout.addWidget(_label("Ids for support and debugging. They are never needed to operate a run."))
        self.text = QPlainTextEdit(text)
        self.text.setReadOnly(True)
        layout.addWidget(self.text)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)


class ControlCenter(QWidget):
    run_selected = Signal(str)
    start_requested = Signal()
    developer_requested = Signal()
    approve_requested = Signal()
    reject_requested = Signal()
    cancel_requested = Signal()
    node_selected = Signal(str)
    inspect_requested = Signal()

    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.setObjectName("controlCenter")
        self.p = theme.palette()
        self.setStyleSheet(theme.stylesheet(self.p))
        self.run_id = ""
        self.run = None
        self.layout_model = control_model.GraphLayout()
        self.rows: list = []
        self.detail = None
        self.selected = ""
        self.message_count = 0
        self._message_ids: dict = {}
        self.gate = control_model.gate_for_run([], "")
        self._note, self._note_key = "", None
        self._feed: dict = {}
        self._kinds: dict = {}
        self.metric_rows: list = []
        self._feed_sig = None
        self._dropped: dict = {}
        self._dropped_forgotten = 0
        self._roster_sig = None
        self._topology_sig = None
        self._names: dict = {}
        self.drawer = None
        self.composer = PromptComposer(settings)
        self._build()
        self.refresh()

    # ---- construction ---------------------------------------------------------------------------
    def _build(self):
        p = self.p
        # toolbar (44 px): run selector + New Run | live state + repo pill | view switcher
        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        toolbar.setFixedHeight(44)
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(8)
        self.run_combo = QComboBox()
        self.run_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.run_combo.setMinimumContentsLength(14)  # a long run label elides instead of widening the page
        self.run_combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.run_combo.setMinimumWidth(220)
        self.run_combo.setMaximumWidth(380)
        self.run_combo.setAccessibleName("Selected run")
        self.run_combo.activated.connect(self._combo_activated)
        self.new_run_button = QPushButton("New Run")
        self.new_run_button.setIcon(icons.icon("plus"))
        self.new_run_button.setAccessibleName("New run")
        self.new_run_button.setToolTip("Configure and start a new run (Ctrl+N)")
        self.new_run_button.clicked.connect(self.start_requested)
        self.state_icon = QLabel()
        self.state_badge = _label("No run", "stateBadge", False)
        self.state_badge.setAccessibleName("Run state")
        self.repo_pill = _label("", "pill", False)
        self.repo_pill.setVisible(False)
        self.repo_pill.setAccessibleName("Repository and branch")
        self.switcher = SegmentedSwitcher()
        self.switcher.switched.connect(self._switched)
        row.addWidget(self.run_combo)
        row.addWidget(self.new_run_button)
        row.addStretch(1)
        row.addWidget(self.state_icon)
        row.addWidget(self.state_badge)
        row.addWidget(self.repo_pill)
        row.addStretch(1)
        row.addWidget(self.switcher)
        self.connection_text = "Connecting"

        self.telemetry = TelemetryStrip()
        self.banner = ApprovalBanner()
        self.gate_frame = self.banner  # the gate card is now the banner (same labels and buttons as before)
        self.gate_title, self.gate_body, self.gate_context, self.gate_status = self.banner.title, self.banner.body, self.banner.context, self.banner.status
        self.gate_payload = self.banner.payload
        self.approve_button, self.reject_button = self.banner.approve, self.banner.reject
        self.approve_button.clicked.connect(self.approve_requested)
        self.reject_button.clicked.connect(self.reject_requested)
        self.banner.inspect_requested.connect(self.inspect_requested)

        # topology pane (left, 55%): full-bleed graph with zoom controls in its header
        self.topology = TopologyView()
        self.topology.fill = True
        self.topology.setMinimumHeight(140)
        self.topology.node_clicked.connect(self.select)
        zoom = QHBoxLayout()
        zoom.setSpacing(2)
        zoom.setContentsMargins(0, 0, 0, 0)
        for name, text, handler in (("minus", "Zoom out", self.topology.zoom_out), ("plus", "Zoom in", self.topology.zoom_in),
                                    ("fit", "Fit to view (Ctrl+0)", self.topology.reset_zoom)):
            button = QToolButton()
            button.setIcon(icons.icon(name))
            button.setToolTip(text)
            button.setAccessibleName(text)
            button.clicked.connect(handler)
            zoom.addWidget(button)
            setattr(self, "zoom_" + name, button)
        zoom_box = QWidget()
        zoom_box.setLayout(zoom)
        self.topology_hint = QLabel("Click a step to inspect it")
        self.topology_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.topology_hint.setObjectName("caption")
        hint_and_zoom = QWidget()
        hz = QHBoxLayout(hint_and_zoom)
        hz.setContentsMargins(0, 0, 0, 0)
        hz.addWidget(self.topology_hint)
        hz.addWidget(zoom_box)
        topo_panel, topo_layout = _pane("Topology", hint_and_zoom)
        topo_layout.setContentsMargins(0, 0, 0, 0)
        topo_layout.addWidget(self.topology, 1)
        legend = QHBoxLayout()
        legend.setContentsMargins(12, 4, 12, 6)
        legend.setSpacing(6)
        for colour, dashed, text in ((p["edge"], False, "Depends on"), (p["running"], False, "Works in parallel"),
                                     (p["purple"], True, "Sent back for changes"), (p["failed"], False, "Blocked")):
            sample = QLabel()
            sample.setPixmap(swatch(colour, dashed))
            sample.setAccessibleName("")
            legend.addWidget(sample)
            legend.addWidget(_label(text, "caption", False))
            legend.addSpacing(8)
        legend.addStretch(1)
        topo_layout.addLayout(legend)

        # inspector pane (right top): the selected agent
        self.selected_icon = QLabel()
        self.selected_pill = _label("Idle", "pill", False)
        pill_box = QWidget()
        pb = QHBoxLayout(pill_box)
        pb.setContentsMargins(0, 0, 0, 0)
        pb.addWidget(self.selected_icon)
        pb.addWidget(self.selected_pill)
        sel_panel, sel_layout = _pane("Inspector", pill_box)
        self.sel_name = _label("", "h2")
        self.sel_state = _label("", rich=True)
        self.sel_links = _label("", rich=True)
        self.sel_summary = _label("", "muted", rich=True)
        self.sel_changes = _label("", "muted", rich=True)
        self.details_button = QPushButton("Show technical details")
        self.details_button.setObjectName("link")
        self.details_button.clicked.connect(self.show_details)
        self.stop_button = QPushButton("Stop this run…")
        self.stop_button.setObjectName("danger")
        self.stop_button.setIcon(icons.icon("stop", p["failed"], p["failed"]))
        self.stop_button.clicked.connect(self.cancel_requested)
        self.stop_button.setVisible(False)
        inspector_body = QWidget()
        ib = QVBoxLayout(inspector_body)
        ib.setContentsMargins(0, 0, 0, 0)
        for widget in (self.sel_name, self.sel_state, self.sel_links, self.sel_summary, self.sel_changes):
            ib.addWidget(widget)
        ib.addStretch(1)
        inspector_scroll = self._scroll(inspector_body)
        sel_layout.addWidget(inspector_scroll, 1)
        buttons = QHBoxLayout()
        buttons.addWidget(self.details_button)
        buttons.addStretch(1)
        buttons.addWidget(self.stop_button)
        sel_layout.addLayout(buttons)

        # activity pane (right bottom): the stream, with a filter
        self.filter = "all"
        self.filter_group = QButtonGroup(self)
        self.filter_buttons = {}
        filters = QWidget()
        fl = QHBoxLayout(filters)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)
        for key, text in FILTERS:
            button = QPushButton(text.replace("&", "&&"))  # '&&' shows one ampersand (a lone '&' is a mnemonic)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setFixedHeight(24)
            button.setAccessibleName(f"Show {text}")
            button.setProperty("filter_key", key)
            self.filter_group.addButton(button)
            fl.addWidget(button)
            self.filter_buttons[key] = button
        self.filter_buttons["all"].setChecked(True)
        self.filter_group.buttonClicked.connect(self._filter_clicked)
        feed_panel, feed_layout = _pane("Activity", filters)
        self.feed_box = QVBoxLayout()
        self.feed_box.setSpacing(4)
        feed_body = QWidget()
        feed_wrap = QVBoxLayout(feed_body)
        feed_wrap.setContentsMargins(0, 0, 0, 0)
        feed_wrap.addLayout(self.feed_box)
        feed_wrap.addStretch(1)
        self.feed_scroll = self._scroll(feed_body)
        feed_layout.addWidget(self.feed_scroll, 1)

        # drawer: Phases | Roster
        self.phase_text = _label("", "muted", False)
        self.phases = _label("", rich=True)
        phases_page = QWidget()
        pl = QVBoxLayout(phases_page)
        pl.setContentsMargins(12, 6, 12, 6)
        pl.addWidget(self.phases)
        pl.addStretch(1)
        self.roster_grid = QGridLayout()
        self.roster_grid.setSpacing(6)
        self.roster_note = _label("", "caption")
        roster_body = QWidget()
        rl = QVBoxLayout(roster_body)
        rl.setContentsMargins(12, 6, 12, 6)
        rl.addLayout(self.roster_grid)
        rl.addWidget(self.roster_note)
        rl.addStretch(1)
        self.roster_cards = {}
        self.bottom_drawer = Drawer({"Phases": phases_page, "Roster": self._scroll(roster_body)})

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(sel_panel)
        right.addWidget(feed_panel)
        right.setSizes([38, 62])
        right.setChildrenCollapsible(False)
        self.right_split = right
        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(topo_panel)
        main.addWidget(right)
        main.setSizes([55, 45])
        main.setChildrenCollapsible(False)
        self.main_split = main
        for splitter in (main, right):
            splitter.setHandleWidth(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        for widget in (toolbar, hairline(), self.telemetry, hairline(), self.banner):
            outer.addWidget(widget)
        outer.addWidget(main, 1)
        outer.addWidget(hairline())
        outer.addWidget(self.bottom_drawer)
        self.composer.prompt.setMinimumHeight(40)
        self.composer.prompt.setMaximumHeight(72)
        composer_frame = QFrame()
        composer_frame.setObjectName("launchBar")
        cl = QVBoxLayout(composer_frame)
        cl.setContentsMargins(12, 6, 12, 6)
        cl.addWidget(self.composer)
        outer.addWidget(hairline())
        outer.addWidget(composer_frame)
        self.setMinimumSize(900, 520)

    @staticmethod
    def _scroll(widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def set_repo_pill(self, text: str):
        """Repository / branch shown beside the run state (client-side knowledge; the daemon's run data does not carry it)."""
        text = narrative.clean(text, 60)
        self.repo_pill.setText(text)
        self.repo_pill.setVisible(bool(text))

    def _switched(self, index: int):
        if index == 1:
            self.switcher.set_current(0)  # the window flips views; this page stays "Control Center" until it is shown again
            self.developer_requested.emit()

    def _filter_clicked(self, button):
        self.filter = button.property("filter_key")
        self.refresh_feed()

    # ---- data in --------------------------------------------------------------------------------
    def set_connection(self, text: str):
        self.connection_text = narrative.clean(text, 60)
        self.switcher.setToolTip(self.connection_text)

    def set_runs(self, runs):
        """Fill the run switcher from GET /api/v1/runs; the current run stays selectable even if it is not listed."""
        entries = [(r["id"], run_choice_label(r)) for r in runs if isinstance(r, dict) and isinstance(r.get("id"), str)]
        if self.run_id and self.run_id not in [i for i, _ in entries]:
            entries.insert(0, (self.run_id, run_choice_label(self.run or {})))
        with QSignalBlocker(self.run_combo):
            self.run_combo.clear()
            if not entries:
                self.run_combo.addItem("No runs yet")
            for run_id, label in entries:
                self.run_combo.addItem(label, run_id)
                self.run_combo.setItemData(self.run_combo.count() - 1, narrative.safe_tip(f"Run id: {run_id[:128]}"), Qt.ItemDataRole.ToolTipRole)
            index = self.run_combo.findData(self.run_id)
            if len(runs) >= control_model.RUNS_SHOWN:  # a full page: older runs exist that this list cannot show
                self.run_combo.addItem("Newest %d shown; older: agentctl runs --cursor" % control_model.RUNS_SHOWN)
                self.run_combo.model().item(self.run_combo.count() - 1).setEnabled(False)
            self.run_combo.setCurrentIndex(index if index >= 0 else -1)
            if index < 0 and entries:
                self.run_combo.setPlaceholderText("Pick a run")
        self.refresh()

    def _combo_activated(self, index):
        run_id = self.run_combo.itemData(index)
        if isinstance(run_id, str) and run_id:
            self.run_selected.emit(run_id)

    def set_run_id(self, run_id: str):
        """The window changed the inspected run: drop everything that belongs to the previous one."""
        if run_id != self.run_id:
            self.run_id, self.run, self.detail, self.selected = run_id, None, None, ""
            self.layout_model = control_model.GraphLayout()
            self._feed, self._feed_sig, self._roster_sig, self._topology_sig, self.message_count = {}, None, None, None, 0
            self._message_ids = {}
            self._dropped, self._dropped_forgotten = {}, 0
            self._note, self._note_key = "", None
            self._names = {}
            with QSignalBlocker(self.run_combo):
                self.run_combo.setCurrentIndex(self.run_combo.findData(run_id))
        self.refresh()

    def set_snapshot(self, run_id, snapshot):
        if run_id != self.run_id:
            return
        self.run = snapshot.get("run") if isinstance(snapshot.get("run"), dict) else None
        self.layout_model = control_model.build_layout(snapshot["tasks"], snapshot["dependencies"])
        self._names = {i: n.name for i, n in self.layout_model.nodes.items()}
        if self.selected not in self.layout_model.nodes:
            self.selected = self._default_selection()
        self.refresh()

    def set_approvals(self, rows):
        self.rows = list(rows)
        self.refresh()

    def set_agent_detail(self, detail):
        self.detail = detail
        self.refresh()

    def set_gate_note(self, text: str):
        """Show a note about the approval currently on the gate; it is dropped as soon as another approval takes over."""
        self._note, self._note_key = narrative.clean(text, 200), self._gate_key()
        self.gate_status.setText(self._note)

    def _gate_key(self):
        row = self.gate.row
        approval_id = row.get("id") if isinstance(row, dict) else None
        return (self.run_id, approval_id, self.gate.state) if isinstance(approval_id, str) else None

    def set_can_cancel(self, enabled: bool):
        self.stop_button.setVisible(bool(enabled))

    def set_messages(self, run_id, messages):
        if run_id != self.run_id:
            return
        for message in messages:
            if isinstance(message, dict) and isinstance(message.get("id"), str):
                if message["id"] not in self._message_ids:
                    self._message_ids[message["id"]] = None
                    self.message_count += 1
                    if len(self._message_ids) > MAX_MESSAGE_IDS:
                        del self._message_ids[next(iter(self._message_ids))]
                self._feed_add("m" + message["id"], (str(message.get("created_at") or ""),
                                                     narrative.narrate_message(message, self._names), f"Message id: {message['id'][:64]}"), "other")
        self._trim_feed()
        self.refresh()

    def add_event(self, event):
        if not isinstance(event, dict) or not self.run_id or event.get("run_id") != self.run_id or event.get("type") == "message.created":
            return
        seq = event.get("sequence")
        if isinstance(seq, bool) or not isinstance(seq, (int, str)) or seq == "":
            seq = event.get("id")
            if not isinstance(seq, str) or not seq:
                return
        self._feed_add(f"e{seq}", (str(event.get("occurred_at") or ""), narrative.narrate_event(event, self._names),
                                   f"Event {seq}: {narrative.clean(event.get('type'), 64)}"), feed_kind(event))
        self._trim_feed()
        self.refresh_feed()

    def _trim_feed(self):
        if len(self._feed) > MAX_FEED_BUFFER:
            for key in sorted(self._feed, key=lambda k: stamp_key(self._feed[k][0]))[:len(self._feed) - MAX_FEED_BUFFER]:
                del self._feed[key]
                self._kinds.pop(key, None)
                self._dropped[key] = None
                if len(self._dropped) > MAX_DROPPED_KEYS:
                    del self._dropped[next(iter(self._dropped))]
                    self._dropped_forgotten += 1

    def dropped_count(self) -> int:
        """Distinct feed rows not shown: trimmed from the buffer or older than the visible window; a re-delivered row counts once."""
        return self._dropped_forgotten + len(self._dropped) + max(0, len(self._feed) - MAX_FEED)

    def _feed_add(self, key, value, kind="other"):
        self._dropped.pop(key, None)  # a trimmed row that came back (reconnect re-delivery) is in the buffer again
        self._feed[key] = value
        self._kinds[key] = kind

    # ---- selection ------------------------------------------------------------------------------
    def _default_selection(self) -> str:
        nodes = self.layout_model.nodes.values()
        for tones in (("running",), ("waiting",), ("failed",)):
            hit = next((n for n in nodes if n.tone in tones), None)
            if hit:
                return hit.id
        return self.layout_model.order[0] if self.layout_model.order else ""

    def select(self, task_id: str):
        if task_id in self.layout_model.nodes:
            self.selected = task_id
            self.node_selected.emit(task_id)
            self.refresh()

    # ---- rendering ------------------------------------------------------------------------------
    def refresh(self):
        layout = self.layout_model
        self.gate = control_model.gate_for_run(self.rows, self.run_id) if self.run_id else control_model.gate_for_run([], "")
        self._render_telemetry()
        self.topology.empty_text = ("Pick a run above, or start a new one" if self.run_combo.count() and self.run_combo.itemData(0) is not None
                                    and not self.run_id else ("Loading run…" if self.run_id and self.run is None
                                    else ("This run has no steps yet" if self.run_id else "No runs yet — start one")))
        signature = (tuple(layout.nodes[i] for i in layout.order), tuple(layout.edges), layout.hidden, layout.width, layout.height)
        if signature != self._topology_sig:
            self._topology_sig = signature
            self.topology.set_layout(layout)
        self.topology.set_selected(self.selected)
        self._render_phases()
        self._render_roster()
        self._render_selected()
        self._render_gate()
        self.refresh_feed()

    def _render_telemetry(self):
        self.metric_rows = control_model.metrics(self.run, self.layout_model, self.message_count, self.rows)
        status, agents, steps, messages, _gate = self.metric_rows
        state = (self.run or {}).get("state") if self.run else None
        pending = sum(1 for r in control_model.run_approvals(self.rows, self.run_id) if r.get("state") == "requested") if self.run_id else 0
        tone = "waiting" if self.gate.state == "pending" else RUN_TONE.get(str(state), "idle" if not self.run else "running")
        label = "Checks" if steps[0] == "Checks passed" else "Steps"
        self.telemetry.set_values(status[1], agents[1].replace(" ", ""), f"{label}: {steps[1].replace(' ', '')}", messages[1], pending, tone)
        self.state_icon.setPixmap(icons.state_icon(tone).pixmap(icons.SIZE, icons.SIZE))
        self.state_badge.setText(status[1] if self.run else "No run")
        self.state_icon.setVisible(bool(self.run))
        self.state_icon.setAccessibleName("")

    def _render_phases(self):
        layout = self.layout_model
        colors = {"complete": self.p["done"], "active": self.p["running"], "blocked": self.p["failed"], "pending": self.p["text_secondary"]}
        words = {"complete": "complete", "active": "active", "blocked": "blocked", "pending": "pending"}
        spans = []
        for phase in layout.phases:
            names = ", ".join(layout.nodes[i].name for i in phase.task_ids[:3]) + (" +%d" % (len(phase.task_ids) - 3) if len(phase.task_ids) > 3 else "")
            spans.append(f"<span style='color:{colors[phase.status]}'>Phase {phase.index + 1} ({words[phase.status]}): {escape(names)}</span>")
        self.phases.setText("<br>".join(spans) if spans else "Phases appear once a run has steps.")
        active = next((p for p in layout.phases if p.status in ("active", "blocked")), None)
        self.phase_text.setText(f"{active.label} of {len(layout.phases)}" if active else
                                ("All steps complete" if layout.phases and all(p.status == "complete" for p in layout.phases) else "Awaiting task"))
        self.bottom_drawer.summary.setText(self.phase_text.text())

    def _render_roster(self):
        layout = self.layout_model
        signature = tuple((i, layout.nodes[i].state, layout.nodes[i].attempts, i == self.selected) for i in layout.order)
        if signature == self._roster_sig:
            return
        self._roster_sig = signature
        while self.roster_grid.count():
            widget = self.roster_grid.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.roster_cards = {}
        for position, task_id in enumerate(layout.order):
            node = layout.nodes[task_id]
            card = RosterCard(task_id)
            card.setObjectName("agentCard")
            card.setProperty("selected", task_id == self.selected)
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card.setToolTip(narrative.safe_tip(f"{node.name}: {narrative.task_sentence(node.state)}"))
            box = QVBoxLayout(card)
            head = QHBoxLayout()
            icon = QLabel()
            icon.setPixmap(icons.state_icon(node.tone if node.tone in theme.STATES else "idle").pixmap(icons.SIZE, icons.SIZE))
            icon.setAccessibleName("")
            name = _label(f"<b>{escape(node.name)}</b> {escape(node.badge.title())}", wrap=True, rich=True)
            head.addWidget(icon)
            head.addWidget(name, 1)
            detail = _label(f"Step {node.phase + 1} · {narrative.task_sentence(node.state)}", "caption")
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(node.progress)
            bar.setTextVisible(False)
            box.addLayout(head)
            for widget in (detail, bar):
                box.addWidget(widget)
            card.picked.connect(self.select)
            self.roster_grid.addWidget(card, position // 4, position % 4)
            self.roster_cards[task_id] = card
        self.roster_note.setText(
            ("Progress bars reflect each step's state, not a measured percentage. " if layout.order else "")
            + (f"{layout.hidden} more steps are not drawn." if layout.hidden else ""))

    def _render_selected(self):
        node = self.layout_model.nodes.get(self.selected)
        self.details_button.setEnabled(node is not None or bool(self.run_id))
        if node is None:
            self.sel_name.setText("Nothing selected")
            self.selected_pill.setText("Idle")
            self.selected_icon.setPixmap(icons.state_icon("idle").pixmap(icons.SIZE, icons.SIZE))
            self.sel_state.setText("Select a step in the topology or roster to see what it does.")
            for label in (self.sel_links, self.sel_summary, self.sel_changes):
                label.setText("")
            return
        self.sel_name.setText(node.name)
        self.selected_pill.setText(node.badge.title())
        self.selected_icon.setPixmap(icons.state_icon(node.tone if node.tone in theme.STATES else "idle").pixmap(icons.SIZE, icons.SIZE))
        self.sel_state.setText(f"<b>Current state:</b> this step {narrative.task_sentence(node.state)}."
                               + (f" It has been tried {node.attempts} times." if node.attempts > 1 else ""))
        waits = ", ".join(node.waits_for) or "nothing (it starts the run)"
        hands = ", ".join(node.hands_to) or "nobody (it is a final step)"
        self.sel_links.setText(f"<b>Waits for:</b> {escape(waits)}<br><b>Hands work to:</b> {escape(hands)}")
        detail = self.detail
        self.sel_summary.setText("")
        self.sel_changes.setText("")
        if detail:
            summary = narrative.clean(detail.get("summary_text"), 400)
            self.sel_summary.setText(("<b>Agent's own summary (whole run):</b> " + escape(summary)) if summary else "")
            wt = detail.get("worktree")
            stat = narrative.clean((wt or {}).get("diff_stat"), 300)
            self.sel_changes.setText(("<b>Changes made so far:</b> " + escape(stat)) if stat else "")

    def _render_gate(self):
        gate = self.gate
        self.gate_title.setText(gate.title)
        self.gate_body.setText(gate.body)
        self.banner.setToolTip(narrative.safe_tip(gate.body))
        row = gate.row or {}
        agent = narrative.clean(row.get("agent_id") or row.get("requested_by") or row.get("principal"), 60)
        operation = narrative.clean(row.get("operation"), 60)
        target = narrative.clean(row.get("target"), 120)
        self.gate_payload.setText(f"{operation} {target}".strip() if gate.decidable else "")
        self.gate_payload.setVisible(bool(self.gate_payload.text()))
        self.banner.icon.setPixmap(icons.state_icon({"pending": "waiting", "approved": "done", "rejected": "failed", "expired": "failed"}.get(gate.state, "idle")).pixmap(icons.SIZE, icons.SIZE))
        self.banner.inspect.setVisible(gate.decidable)
        self.banner.approve.setVisible(gate.decidable)
        self.banner.reject.setVisible(gate.decidable)
        stat = narrative.clean(((self.detail or {}).get("worktree") or {}).get("diff_stat"), 300)
        self.gate_context.setText(f"What the agent changed so far: {stat}" if gate.decidable and stat else "")
        self.gate_context.setVisible(bool(self.gate_context.text()))
        self.approve_button.setEnabled(gate.decidable)
        self.reject_button.setEnabled(gate.decidable)
        if self._note_key != self._gate_key():
            self._note, self._note_key = "", None
        default = {"none": "Locked until an agent asks.", "pending": "Waiting for you.", "approved": "Decision recorded: approved.",
                   "rejected": "Decision recorded: rejected.", "expired": "Expired: treated as not approved."}.get(gate.state, "")
        self.gate_status.setText(self._note or default)
        self.gate_status.setVisible(bool(self._note))
        self.banner.show_for(gate.decidable or bool(self._note))

    def refresh_feed(self):
        wanted = {"all": None, "decisions": ("decision", "tool"), "errors": ("error",)}[self.filter]
        pairs = [(k, v) for k, v in self._feed.items() if wanted is None or self._kinds.get(k, "other") in wanted]
        entries = [v for _k, v in sorted(pairs, key=lambda kv: stamp_key(kv[1][0]), reverse=True)][:MAX_FEED]
        dropped = self.dropped_count()
        signature = (tuple(entries), dropped, self.filter)
        if signature == self._feed_sig:
            return
        self._feed_sig = signature
        while self.feed_box.count():
            widget = self.feed_box.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        if not entries:
            self.feed_box.addWidget(_label("Nothing has happened yet. Start or select a run to see what the agents are doing."
                                           if self.filter == "all" and not self._feed else "Nothing matches this filter.", "muted"))
            return
        for stamp, text, tip in entries:
            frame = QFrame()
            frame.setObjectName("message")
            frame.setToolTip(narrative.safe_tip(tip))
            box = QHBoxLayout(frame)
            box.setContentsMargins(8, 4, 8, 4)
            stamp_label = _label(fmt_time(stamp), "mono", False)
            stamp_label.setFont(theme.mono_font())
            box.addWidget(stamp_label, 0, Qt.AlignmentFlag.AlignTop)
            box.addWidget(_label(text), 1)
            self.feed_box.addWidget(frame)
        if dropped:
            self.feed_box.addWidget(_label(f"{dropped} older update{'s' if dropped != 1 else ''} not shown.", "muted"))

    def show_details(self):
        node = self.layout_model.nodes.get(self.selected)
        gate_row = self.gate.row or {}
        lines = [f"Run id: {self.run_id}", f"Run state: {(self.run or {}).get('state', '-')}",
                 f"Workflow: {(self.run or {}).get('workflow_id', '-')}"]
        if node:
            lines += [f"Task id: {node.id}", f"Node id: {node.node_id}", f"Task state: {node.state}", f"Attempts: {node.attempts}"]
        if gate_row:
            lines += [f"Approval id: {gate_row.get('id', '-')}", f"Approval state: {gate_row.get('state', '-')}",
                      f"Operation: {gate_row.get('operation', '-')}", f"Target: {gate_row.get('target', '-')}"]
        self.drawer = DetailsDrawer(self, "\n".join(narrative.clean(line, 300) for line in lines))
        self.drawer.show()
