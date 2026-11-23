"""'Execution topology': a QGraphicsView drawing agent cards and typed edges from a control_model.GraphLayout.

Cards show name, step, a coloured status badge. Edges: solid = dependency, cyan = parallel, purple dashed = feedback,
red = blocked. Edges touching a running step animate (dashes flow); the timer only runs while the view is visible,
something is running, and reduced motion is off.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPainterPathStroker, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject, QGraphicsPathItem, QGraphicsScene, QGraphicsView

from synapse.desktop import theme
from synapse.desktop.narrative import safe_tip
from synapse.desktop.control_model import NODE_H, NODE_W, GraphLayout

MARGIN = 24
MIN_HEIGHT = 260
MAX_FIT_HEIGHT = 560
FRAME_MS = 50
EMPTY_TEXT = "No runs yet — start one"


def _color(p, key):
    return QColor(p[key])


class NodeItem(QGraphicsObject):
    def __init__(self, node, palette, view):
        super().__init__()
        self.node, self.p, self.view = node, palette, view
        self.selected = False
        self.setPos(node.x, node.y)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(safe_tip(f"{node.name}\n{node.badge.title()}\nDetails drawer shows the technical ids."))
        self.setZValue(2)

    def boundingRect(self):
        return QRectF(-3, -3, NODE_W + 6, NODE_H + 6)

    def paint(self, painter, _option, _widget=None):
        p, node = self.p, self.node
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        tone = _color(p, theme.TONES[node.tone])
        rect = QRectF(0, 0, NODE_W, NODE_H)
        painter.setBrush(_color(p, "node"))
        painter.setPen(QPen(_color(p, "cyan") if self.selected else _color(p, "node_line"), 2.5 if self.selected else 1.2))
        painter.drawRoundedRect(rect, 12, 12)
        painter.setPen(_color(p, "text"))
        font = QFont(self.view.font())
        font.setPointSizeF(9.5)
        font.setBold(True)
        painter.setFont(font)
        inner = rect.adjusted(11, 9, -11, 0)
        painter.drawText(QRectF(inner.left(), inner.top(), inner.width(), 20), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         painter.fontMetrics().elidedText(node.name, Qt.TextElideMode.ElideRight, int(inner.width())))
        font.setBold(False)
        font.setPointSizeF(8)
        painter.setFont(font)
        painter.setPen(_color(p, "muted"))
        painter.drawText(QRectF(inner.left(), inner.top() + 22, inner.width(), 16), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         f"Step {node.phase + 1}" + (f" · attempt {node.attempts}" if node.attempts > 1 else ""))
        painter.setPen(tone)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(inner.left(), NODE_H - 28, inner.width(), 18), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         "● " + node.badge)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.view.node_clicked.emit(self.node.id)
            event.accept()
        else:
            super().mousePressEvent(event)


class EdgeItem(QGraphicsPathItem):
    def __init__(self, path, edge, palette, animated):
        super().__init__(path)
        self.edge, self.p, self.animated, self.offset = edge, palette, animated, 0.0
        self.setZValue(1)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def color(self):
        key = "red" if self.edge.blocked else {"feedback": "purple", "parallel": "cyan"}.get(self.edge.kind, "edge")
        return _color(self.p, key)

    def boundingRect(self):
        return super().boundingRect().adjusted(-8, -8, 8, 8)

    def shape(self):
        stroker = QPainterPathStroker()
        stroker.setWidth(10)
        return stroker.createStroke(self.path())

    def paint(self, painter, _option, _widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self.color(), 2)
        if self.edge.kind == "feedback":
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([3, 4])
        elif self.animated:
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([4, 3.5])
        pen.setDashOffset(self.offset)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())
        angle = math.radians(self.path().angleAtPercent(1.0))
        tip = self.path().pointAtPercent(1.0)
        wing = [QPointF(tip.x() - 9 * math.cos(angle - 0.45), tip.y() + 9 * math.sin(angle - 0.45)),
                QPointF(tip.x() - 9 * math.cos(angle + 0.45), tip.y() + 9 * math.sin(angle + 0.45))]
        painter.setPen(QPen(self.color(), 1.6))
        painter.drawPolyline(QPolygonF([wing[0], tip, wing[1]]))


def edge_path(source, target, kind) -> QPainterPath:
    path = QPainterPath()
    if kind == "feedback":
        start, end = QPointF(source.x + NODE_W, source.y + 28), QPointF(target.x + NODE_W, target.y + 28)
        bulge = max(start.x(), end.x()) + 50
        path.moveTo(start)
        path.cubicTo(QPointF(bulge, start.y() + 20), QPointF(bulge, end.y() - 20), end)
        return path
    start, end = QPointF(source.x + NODE_W / 2, source.y + NODE_H), QPointF(target.x + NODE_W / 2, target.y)
    mid = (start.y() + end.y()) / 2
    path.moveTo(start)
    path.cubicTo(QPointF(start.x(), mid), QPointF(end.x(), mid), end)
    return path


class TopologyView(QGraphicsView):
    node_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setMinimumHeight(MIN_HEIGHT)
        self.setAccessibleName("Execution topology")
        self.p = theme.palette()
        self.layout_model = GraphLayout()
        self.node_items: dict = {}
        self.edge_items: list = []
        self.selected_id = ""
        self.empty_text = EMPTY_TEXT
        self.motion = not theme.reduced_motion()
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)
        self.set_layout(GraphLayout())

    # -- public ----------------------------------------------------------------------------------
    def set_palette(self, palette):
        self.p = palette
        self.set_layout(self.layout_model)

    def set_layout(self, layout: GraphLayout):
        self.layout_model = layout
        scene = self.scene()
        scene.clear()
        self.node_items, self.edge_items = {}, []
        scene.setBackgroundBrush(_color(self.p, "panel"))
        for edge in layout.edges:
            source, target = layout.nodes[edge.source], layout.nodes[edge.target]
            running = "running" in (source.tone, target.tone) and not edge.blocked
            item = EdgeItem(edge_path(source, target, edge.kind), edge, self.p, running and edge.kind != "feedback")
            scene.addItem(item)
            self.edge_items.append(item)
        for task_id in layout.order:
            item = NodeItem(layout.nodes[task_id], self.p, self)
            item.selected = task_id == self.selected_id
            scene.addItem(item)
            self.node_items[task_id] = item
        if layout.nodes:
            scene.setSceneRect(-MARGIN - 60, -MARGIN, layout.width + 2 * MARGIN + 120, layout.height + 2 * MARGIN)
        else:
            scene.setSceneRect(0, 0, 10, 10)
        self._refit()
        self._sync_timer()
        self.viewport().update()

    def set_selected(self, task_id: str):
        self.selected_id = task_id
        for key, item in self.node_items.items():
            item.selected = key == task_id
            item.update()

    def animating(self) -> bool:
        return self._timer.isActive()

    # -- animation -------------------------------------------------------------------------------
    def wants_animation(self) -> bool:
        return self.motion and self.isVisible() and any(e.animated for e in self.edge_items)

    def _sync_timer(self):
        if self.wants_animation():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def _tick(self):
        for item in self.edge_items:
            if item.animated:
                item.offset -= 0.9
                item.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._refit()
        self._sync_timer()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    # -- geometry --------------------------------------------------------------------------------
    def _refit(self):
        self.resetTransform()
        if not self.layout_model.nodes:
            return
        rect = self.scene().sceneRect()
        view = self.viewport().rect()
        if view.width() < 50 or view.height() < 50:
            return
        scale = min(1.0, view.width() / rect.width(), view.height() / rect.height())
        scale = max(scale, 0.55)  # beyond this, scroll instead of shrinking cards to illegibility
        self._fit_height(rect, view.width())
        self.scale(scale, scale)
        self.centerOn(rect.center().x(), rect.top() if rect.height() * scale > view.height() else rect.center().y())

    def _fit_height(self, rect, width):
        """Ask for the height that shows the whole graph at the width-limited scale, so no inner scrollbar is needed (capped)."""
        scale = max(min(1.0, width / rect.width()), 0.55)
        wanted = max(MIN_HEIGHT, min(MAX_FIT_HEIGHT, int(rect.height() * scale) + 4))
        if wanted != self.minimumHeight():
            self.setMinimumHeight(wanted)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refit()

    def drawForeground(self, painter, _rect):
        if self.layout_model.nodes:
            return
        painter.resetTransform()
        painter.setPen(QColor(self.p["muted"]))
        font = QFont(self.font())
        font.setPointSizeF(12)
        painter.setFont(font)
        painter.drawText(self.viewport().rect(), int(Qt.AlignmentFlag.AlignCenter), self.empty_text)
