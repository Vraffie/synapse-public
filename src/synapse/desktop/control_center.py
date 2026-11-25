"""Control Center: the default, plain-language home view. A dumb view over daemon data pushed in by MainWindow.

It never talks to the daemon. MainWindow feeds it validated rows (runs list, run snapshot, approvals, agent detail,
messages, events) and turns its signals (run picked, Start run, Approve/Reject, Stop, node selected) into the existing
poller commands. Nothing is invented: absent data shows an honest empty state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from synapse.desktop import control_model, narrative, theme
from synapse.desktop.prompt_composer import PromptComposer
from synapse.desktop.topology_view import TopologyView

MAX_FEED = 60
MAX_FEED_BUFFER = 400
MAX_MESSAGE_IDS = 20000
MAX_DROPPED_KEYS = 20000  # trimmed feed keys remembered so a re-delivered row is not counted as dropped twice
NO_RUN = "No run selected"


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


def _panel(title: str, trailing: QWidget | None = None):
    frame = QFrame()
    frame.setObjectName("panel")
    layout = QVBoxLayout(frame)
    top = QHBoxLayout()
    heading = QLabel(title)
    heading.setObjectName("h2")
    heading.setWordWrap(True)  # long titles and hints wrap instead of widening the page
    top.addWidget(heading)
    top.addStretch(1)
    if trailing is not None:
        if isinstance(trailing, QLabel):
            trailing.setWordWrap(True)
            trailing.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(trailing)
    layout.addLayout(top)
    return frame, layout


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
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(_label("SYNAPSE / OPERATIONS", "eyebrow"))
        titles.addWidget(_label("Agent Control Center", "h1"))
        titles.addWidget(_label("See what your agents are doing, step by step, and decide when they need a person.", "muted"))
        header.addLayout(titles, 1)
        self.connection_pill = _label("Connecting", "pill")
        self.run_combo = QComboBox()
        self.run_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.run_combo.setMinimumContentsLength(14)  # a long run label elides instead of widening the page
        self.run_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.run_combo.setMinimumWidth(120)
        self.run_combo.setAccessibleName("Selected run")
        self.run_combo.activated.connect(self._combo_activated)
        self.start_button = QPushButton("▶ Start run")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start_requested)
        self.developer_button = QPushButton("Developer view")
        self.developer_button.clicked.connect(self.developer_requested)
        controls = QVBoxLayout()
        row, buttons_row = QHBoxLayout(), QHBoxLayout()  # two short rows keep the header narrow enough for any font
        for widget in (self.connection_pill, self.run_combo):
            row.addWidget(widget)
        buttons_row.addStretch(1)
        for widget in (self.start_button, self.developer_button):
            buttons_row.addWidget(widget)
        controls.addLayout(row)
        controls.addLayout(buttons_row)
        header.addLayout(controls)

        self.tiles = []
        tile_row = QHBoxLayout()
        for _ in range(5):
            frame = QFrame()
            frame.setObjectName("metric")
            box = QVBoxLayout(frame)
            title, value, caption = _label("", "caption", False), _label("", "metricValue", False), _label("", "caption", False)
            for widget in (title, value, caption):
                widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # clip, never force the page wider
                box.addWidget(widget)
            tile_row.addWidget(frame, 1)
            self.tiles.append((title, value, caption))

        self.topology = TopologyView()
        self.topology.node_clicked.connect(self.select)
        legend = _label("<b style='color:%s'>━━</b> Depends on &nbsp; <b style='color:%s'>━━</b> Works in parallel &nbsp; "
                        "<b style='color:%s'>┄┄</b> Sent back for changes &nbsp; <b style='color:%s'>━━</b> Blocked &nbsp; "
                        "<span style='color:%s'>●</span> Needs your approval"
                        % (self.p["edge"], self.p["cyan"], self.p["purple"], self.p["red"], self.p["amber"]), "caption", rich=True)
        self.topology_hint = QLabel("Click a step to inspect it")
        self.topology_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.topology_hint.setObjectName("pill")
        topo_panel, topo_layout = _panel("Execution topology", self.topology_hint)
        topo_layout.addWidget(self.topology, 1)
        topo_layout.addWidget(legend)

        self.phase_text = _label("", "muted", False)
        phase_panel, phase_layout = _panel("Execution phases", self.phase_text)
        self.phases = _label("", rich=True)
        phase_layout.addWidget(self.phases)

        roster_panel, roster_layout = _panel("Agent roster", _label("Select a step", "muted", False))
        self.roster_grid = QGridLayout()
        roster_layout.addLayout(self.roster_grid)
        self.roster_note = _label("", "caption")
        roster_layout.addWidget(self.roster_note)
        self.roster_cards: dict = {}

        self.selected_pill = _label("Idle", "pill", False)
        sel_panel, sel_layout = _panel("Selected agent", self.selected_pill)
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
        self.stop_button.clicked.connect(self.cancel_requested)
        self.stop_button.setVisible(False)
        for widget in (self.sel_name, self.sel_state, self.sel_links, self.sel_summary, self.sel_changes):
            sel_layout.addWidget(widget)
        buttons = QHBoxLayout()
        buttons.addWidget(self.details_button)
        buttons.addStretch(1)
        buttons.addWidget(self.stop_button)
        sel_layout.addLayout(buttons)

        gate_pill = _label("Fails closed", "pill", False)
        gate_panel, gate_layout = _panel("Approval & safety gates", gate_pill)
        self.gate_frame = QFrame()
        self.gate_frame.setObjectName("gate")
        box = QVBoxLayout(self.gate_frame)
        self.gate_title = _label("", "gateTitle")
        self.gate_body = _label("")
        self.gate_context = _label("")
        self.approve_button = QPushButton("Approve")
        self.approve_button.setObjectName("primary")
        self.reject_button = QPushButton("Reject")
        self.reject_button.setObjectName("danger")
        self.approve_button.clicked.connect(self.approve_requested)
        self.reject_button.clicked.connect(self.reject_requested)
        buttons = QHBoxLayout()
        buttons.addWidget(self.approve_button)
        buttons.addWidget(self.reject_button)
        buttons.addStretch(1)
        self.gate_status = _label("", "caption")
        for widget in (self.gate_title, self.gate_body, self.gate_context):
            box.addWidget(widget)
        box.addLayout(buttons)
        box.addWidget(self.gate_status)
        gate_layout.addWidget(self.gate_frame)

        feed_panel, feed_layout = _panel("Agent communication", _label("What is happening, in plain words", "muted", False))
        self.feed_box = QVBoxLayout()
        self.feed_box.setSpacing(6)
        feed_layout.addLayout(self.feed_box)

        left, right = QVBoxLayout(), QVBoxLayout()
        for panel in (topo_panel, phase_panel, roster_panel):
            left.addWidget(panel)
        left.addStretch(1)
        for panel in (sel_panel, feed_panel):
            right.addWidget(panel)
        right.addStretch(1)
        columns = QHBoxLayout()
        columns.addLayout(left, 17)
        columns.addLayout(right, 10)

        content = QWidget()
        content.setObjectName("controlCenter")
        inner = QVBoxLayout(content)
        inner.addLayout(header)
        inner.addWidget(gate_panel)  # the decision card sits right under the header, full width: never pushed below the fold
        inner.addLayout(tile_row)
        inner.addLayout(columns)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll, 1)
        self.composer.prompt.setMinimumHeight(56)
        self.composer.prompt.setMaximumHeight(72)  # a compact prompt box leaves the content area room for the gate card
        composer_frame = QFrame()
        composer_frame.setObjectName("panel")
        QVBoxLayout(composer_frame).addWidget(self.composer)
        outer.addWidget(composer_frame)

    # ---- data in --------------------------------------------------------------------------------
    def set_connection(self, text: str):
        self.connection_pill.setText(narrative.clean(text, 60))

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
                                                     narrative.narrate_message(message, self._names), f"Message id: {message['id'][:64]}"))
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
                                   f"Event {seq}: {narrative.clean(event.get('type'), 64)}"))
        self._trim_feed()
        self.refresh_feed()

    def _trim_feed(self):
        if len(self._feed) > MAX_FEED_BUFFER:
            for key in sorted(self._feed, key=lambda k: stamp_key(self._feed[k][0]))[:len(self._feed) - MAX_FEED_BUFFER]:
                del self._feed[key]
                self._dropped[key] = None
                if len(self._dropped) > MAX_DROPPED_KEYS:
                    del self._dropped[next(iter(self._dropped))]
                    self._dropped_forgotten += 1

    def dropped_count(self) -> int:
        """Distinct feed rows not shown: trimmed from the buffer or older than the visible window; a re-delivered row counts once."""
        return self._dropped_forgotten + len(self._dropped) + max(0, len(self._feed) - MAX_FEED)

    def _feed_add(self, key, value):
        self._dropped.pop(key, None)  # a trimmed row that came back (reconnect re-delivery) is in the buffer again
        self._feed[key] = value

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
        for (title, value, caption), (t, v, c) in zip(self.tiles, control_model.metrics(self.run, layout, self.message_count, self.rows)):
            title.setText(t)
            value.setText(v)
            caption.setText(c)
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

    def _render_phases(self):
        layout = self.layout_model
        colors = {"complete": self.p["green"], "active": self.p["cyan"], "blocked": self.p["red"], "pending": self.p["muted"]}
        marks = {"complete": "✓", "active": "▶", "blocked": "⚠", "pending": ""}
        spans = []
        for phase in layout.phases:
            names = ", ".join(layout.nodes[i].name for i in phase.task_ids[:3]) + (" +%d" % (len(phase.task_ids) - 3) if len(phase.task_ids) > 3 else "")
            spans.append(f"<span style='color:{colors[phase.status]}'>[{marks[phase.status]} {phase.index + 1} · {escape(names)}]</span>".replace("[ ", "["))
        self.phases.setText("&nbsp; ".join(spans) if spans else "Phases appear once a run has steps.")
        active = next((p for p in layout.phases if p.status in ("active", "blocked")), None)
        self.phase_text.setText(f"{active.label} of {len(layout.phases)}" if active else
                                ("All steps complete" if layout.phases and all(p.status == "complete" for p in layout.phases) else "Awaiting task"))

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
            name = _label(f"<b>{escape(node.name)}</b> <span style='color:{self.p[theme.TONES[node.tone]]}'>● {escape(node.badge.title())}</span>",
                          wrap=True, rich=True)
            detail = _label(f"Step {node.phase + 1} · {narrative.task_sentence(node.state)}", "caption")
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(node.progress)
            bar.setTextVisible(False)
            for widget in (name, detail, bar):
                box.addWidget(widget)
            card.picked.connect(self.select)
            self.roster_grid.addWidget(card, position // 2, position % 2)
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
            self.sel_state.setText("Select a step in the topology or roster to see what it does.")
            for label in (self.sel_links, self.sel_summary, self.sel_changes):
                label.setText("")
            return
        self.sel_name.setText(node.name)
        self.selected_pill.setText(node.badge.title())
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
        self.gate_title.setText({"pending": "⏳ ", "approved": "✓ ", "rejected": "✗ ", "expired": "⌛ ", "none": "\U0001f512 "}.get(gate.state, "") + gate.title)
        self.gate_body.setText(gate.body)
        stat = narrative.clean(((self.detail or {}).get("worktree") or {}).get("diff_stat"), 300)
        self.gate_context.setText(f"What the agent changed so far: {stat}\nYou will see the full changes before you decide." if gate.decidable and stat else "")
        self.gate_context.setVisible(bool(self.gate_context.text()))
        self.approve_button.setEnabled(gate.decidable)
        self.reject_button.setEnabled(gate.decidable)
        if self._note_key != self._gate_key():
            self._note, self._note_key = "", None
        default = {"none": "Locked until an agent asks.", "pending": "Waiting for you.", "approved": "Decision recorded: approved.",
                   "rejected": "Decision recorded: rejected.", "expired": "Expired: treated as not approved."}.get(gate.state, "")
        self.gate_status.setText(self._note or default)

    def refresh_feed(self):
        entries = sorted(self._feed.values(), key=lambda e: stamp_key(e[0]), reverse=True)[:MAX_FEED]
        dropped = self.dropped_count()
        signature = (tuple(entries), dropped)
        if signature == self._feed_sig:
            return
        self._feed_sig = signature
        while self.feed_box.count():
            widget = self.feed_box.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        if not entries:
            self.feed_box.addWidget(_label("Nothing has happened yet. Start or select a run to see what the agents are doing.", "muted"))
            return
        for stamp, text, tip in entries:
            frame = QFrame()
            frame.setObjectName("message")
            frame.setToolTip(narrative.safe_tip(tip))
            box = QVBoxLayout(frame)
            box.setContentsMargins(8, 5, 8, 5)
            box.addWidget(_label(fmt_time(stamp), "caption", False))
            box.addWidget(_label(text))
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
