"""Chrome of the workstation layout: view switcher, telemetry strip, approval banner, collapsible drawer.

Each widget takes display-safe strings from its caller and never talks to the daemon. State is always icon + text, with
colour only reinforcing it. Sizes: toolbar 44 px, telemetry strip 34 px, banner about 52 px when shown, drawer 32 / 168 px.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QStackedWidget,
                               QTabBar, QToolButton, QVBoxLayout, QWidget)

from synapse.desktop import icons, narrative, theme

TOOLBAR_HEIGHT, TELEMETRY_HEIGHT, BANNER_HEIGHT = 44, 34, 52
DRAWER_COLLAPSED, DRAWER_OPEN = 32, 168


def hairline(vertical: bool = False) -> QFrame:
    line = QFrame()
    line.setObjectName("hairline")
    line.setFrameShape(QFrame.Shape.VLine if vertical else QFrame.Shape.HLine)
    if vertical:
        line.setFixedWidth(1)
    else:
        line.setFixedHeight(1)
    return line


class SegmentedSwitcher(QWidget):
    """Control Center | Developer, with the pending-approval count in amber beside Developer (icon + number, not colour alone)."""
    switched = Signal(int)

    def __init__(self, labels=("Control Center", "Developer"), parent=None):
        super().__init__(parent)
        self.setObjectName("segmented")
        self.setAccessibleName("View switcher")
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.buttons = []
        for index, text in enumerate(labels):
            button = QPushButton(text)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setAccessibleName(f"{text} view")
            button.setFixedHeight(28)
            self.group.addButton(button, index)
            row.addWidget(button)
            self.buttons.append(button)
        self.buttons[0].setChecked(True)
        self.group.idClicked.connect(self.switched)
        self.base_labels = list(labels)

    def set_current(self, index: int):
        self.buttons[index].setChecked(True)

    def set_pending(self, count: int, index: int = 1):
        button = self.buttons[index]
        button.setText(f"{self.base_labels[index]}  {count}" if count else self.base_labels[index])
        button.setIcon(icons.state_icon("waiting") if count else QIcon())
        button.setAccessibleName(f"{self.base_labels[index]} view, {count} approval{'s' if count != 1 else ''} pending"
                                 if count else f"{self.base_labels[index]} view")


class TelemetryStrip(QFrame):
    """One monospace line: Status · Agents · Steps · Messages · Approvals. Every segment is also an accessible label."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("telemetry")
        self.setFixedHeight(TELEMETRY_HEIGHT)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(0)
        self.status_icon = QLabel()
        row.addWidget(self.status_icon)
        self.segments: dict[str, QLabel] = {}
        for index, name in enumerate(("Status", "Agents", "Steps", "Messages", "Approvals")):
            if index:
                row.addWidget(self._dot())
            label = QLabel()
            label.setObjectName("mono")
            label.setFont(theme.mono_font())
            label.setAccessibleName(name)
            row.addWidget(label)
            self.segments[name] = label
        row.addStretch(1)
        self.gates_icon = QLabel()
        self.gates = QLabel("Gates clear")
        self.gates.setObjectName("caption")
        row.addWidget(self.gates_icon)
        row.addWidget(self.gates)
        self.set_values("No run", "0/0", "Steps: 0/0", "0", 0, "idle")

    def _dot(self) -> QLabel:
        dot = QLabel("  ·  ")
        dot.setObjectName("muted")
        dot.setAccessibleName("")
        return dot

    def set_values(self, status: str, agents: str, steps: str, messages: str, approvals: int, tone: str):
        """Strings are display-ready (the caller formats them from control_model.metrics); ``approvals`` counts pending gates."""
        self.segments["Status"].setText(f"Status: {narrative.clean(status, 40)}")
        self.segments["Agents"].setText(f"Agents: {narrative.clean(agents, 20)}")
        self.segments["Steps"].setText(narrative.clean(steps, 30))
        self.segments["Messages"].setText(f"Messages: {narrative.clean(messages, 12)}")
        self.segments["Approvals"].setText(f"Approvals: {approvals}")
        self.status_icon.setPixmap(icons.state_icon(tone).pixmap(icons.SIZE, icons.SIZE))
        self.status_icon.setContentsMargins(0, 0, 8, 0)
        self.status_icon.setToolTip(icons.state_label(tone))
        self.status_icon.setAccessibleName(f"Run state: {icons.state_label(tone)}")
        clear = approvals == 0
        self.gates.setText("Gates clear" if clear else "Approval needed")
        self.gates_icon.setPixmap((icons.icon("lock") if clear else icons.state_icon("waiting")).pixmap(icons.SIZE, icons.SIZE))
        self.gates_icon.setContentsMargins(0, 0, 4, 0)
        self.gates.setAccessibleName("Approval gates: " + ("clear" if clear else "approval needed"))

    def text(self) -> str:
        return "  ·  ".join(self.segments[name].text() for name in ("Status", "Agents", "Steps", "Messages", "Approvals"))


class ApprovalBanner(QFrame):
    """Contextual banner under the telemetry strip: takes no space when nothing is pending, ~52 px when an approval waits.

    The existing gate labels and buttons live here (same attribute names the Control Center always had)."""
    inspect_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("gate")
        self.setMinimumHeight(0)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(10)
        self.icon = QLabel()
        self.icon.setPixmap(icons.state_icon("waiting").pixmap(icons.SIZE, icons.SIZE))
        self.icon.setAccessibleName("Approval needed")
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.title = QLabel()
        self.title.setObjectName("gateTitle")
        self.body = QLabel()  # the full plain-language explanation: shown as the banner's tooltip, not as a third line
        self.payload = QLabel()
        self.payload.setObjectName("mono")
        self.payload.setFont(theme.mono_font())
        self.payload.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.payload.setAccessibleName("Payload preview")
        self.context = QLabel()
        self.context.setObjectName("caption")
        self.context.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status = QLabel()
        self.status.setObjectName("caption")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        for widget in (self.title, self.body, self.payload, self.context, self.status):
            widget.setTextFormat(Qt.TextFormat.PlainText)
        line1 = QHBoxLayout()
        line1.setSpacing(10)
        line1.addWidget(self.title)
        line1.addWidget(self.payload, 1)
        texts.addLayout(line1)
        line2 = QHBoxLayout()
        line2.setSpacing(10)
        line2.addWidget(self.context, 1)
        line2.addWidget(self.status, 1)
        texts.addLayout(line2)
        self.body.setVisible(False)
        self.approve = QPushButton("Approve")
        self.approve.setObjectName("primary")
        self.approve.setAccessibleName("Approve this request")
        self.reject = QPushButton("Reject")
        self.reject.setObjectName("danger")
        self.reject.setAccessibleName("Reject this request")
        self.inspect = QToolButton()
        self.inspect.setObjectName("link")
        self.inspect.setText("Inspect full diff")
        self.inspect.setIcon(icons.icon("arrow-right"))
        self.inspect.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.inspect.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.inspect.setAccessibleName("Inspect full diff")
        self.inspect.setToolTip("You will see the full changes before you decide")
        self.inspect.clicked.connect(self.inspect_requested)
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(texts, 1)
        row.addWidget(self.inspect)
        row.addWidget(self.approve)
        row.addWidget(self.reject)
        self.setVisible(False)

    def show_for(self, active: bool):
        """Visible only while there is something to decide (or a note about the decision to read)."""
        self.setVisible(active)


class Drawer(QFrame):
    """Collapsible bottom drawer with Phases | Roster tabs: 32 px (tab strip only) or 168 px (open)."""
    toggled = Signal(bool)

    def __init__(self, pages: dict[str, QWidget], parent=None):
        super().__init__(parent)
        self.setObjectName("drawer")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 0, 8, 0)
        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.tabs.setAccessibleName("Drawer tabs")
        for title in pages:
            self.tabs.addTab(title)
        self.toggle = QToolButton()
        self.toggle.setAccessibleName("Expand drawer")
        self.toggle.setToolTip("Expand or collapse the drawer")
        self.toggle.clicked.connect(self.toggle_open)
        self.summary = QLabel()
        self.summary.setObjectName("caption")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        bar.addWidget(self.tabs)
        bar.addWidget(self.summary, 1)
        bar.addWidget(self.toggle)
        column.addLayout(bar)
        self.stack = QStackedWidget()
        for page in pages.values():
            self.stack.addWidget(page)
        column.addWidget(self.stack, 1)
        self.tabs.currentChanged.connect(self.stack.setCurrentIndex)
        self.tabs.tabBarClicked.connect(self._tab_clicked)
        self.is_open = False
        self._apply()

    def _tab_clicked(self, _index: int):
        if not self.is_open:
            self.set_open(True)

    def toggle_open(self):
        self.set_open(not self.is_open)

    def set_open(self, value: bool):
        if value != self.is_open:
            self.is_open = value
            self._apply()
            self.toggled.emit(value)

    def _apply(self):
        self.setFixedHeight(DRAWER_OPEN if self.is_open else DRAWER_COLLAPSED)
        self.stack.setVisible(self.is_open)
        self.toggle.setIcon(icons.icon("chevron-down" if self.is_open else "chevron-up"))
        self.toggle.setAccessibleName("Collapse drawer" if self.is_open else "Expand drawer")
