"""Pure data model of the Control Center: DAG layout, phases, edges, metrics, gate state, roster. No Qt, no I/O.

Inputs are the validated snapshot rows from GET /api/v1/runs/{id} (tasks, dependencies) plus approval rows. Nothing
here invents data: when the API lacks a field the model leaves it out.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from synapse.desktop import narrative

RUNNING_STATES = ("assigned", "running", "verifying")
ACTIVE_STATES = RUNNING_STATES + ("ready", "awaiting_approval", "retry_wait", "changes_required")
BAD_STATES = ("failed", "blocked", "cancelled")
CHECK_WORDS = ("review", "verif", "check", "test", "lens", "qa", "valid", "lint")
MAX_NODES = 200  # the view draws at most this many cards; the rest are counted, not drawn

NODE_W, NODE_H = 176, 84
GAP_X, GAP_Y = 36, 64


@dataclass(frozen=True)
class Node:
    id: str
    node_id: str
    name: str
    state: str
    badge: str
    tone: str
    phase: int
    attempts: int
    layer_index: int
    x: float
    y: float
    waits_for: tuple = ()
    hands_to: tuple = ()
    progress: int = 0


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: str  # dependency | parallel | feedback
    blocked: bool = False


@dataclass(frozen=True)
class Phase:
    index: int
    label: str
    status: str  # pending | active | complete | blocked
    task_ids: tuple = ()


@dataclass
class GraphLayout:
    nodes: dict = field(default_factory=dict)  # task id -> Node
    order: list = field(default_factory=list)  # task ids in draw order
    edges: list = field(default_factory=list)
    phases: list = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0
    hidden: int = 0


PROGRESS = {"completed": 100, "verifying": 85, "running": 55, "awaiting_approval": 50, "assigned": 25, "ready": 10,
            "failed": 40, "blocked": 40, "changes_required": 60, "retry_wait": 30}


def depths(ids, deps) -> dict:
    """Longest-path depth from the roots (0 = no prerequisites). Cycles cannot occur in a valid run; if one does, the
    nodes on it fall back to depth 0 instead of looping."""
    parents = {i: [] for i in ids}
    for child, parent in deps:
        if child in parents and parent in parents and child != parent:
            parents[child].append(parent)
    result, visiting = {}, set()

    def depth(node):
        if node in result:
            return result[node]
        if node in visiting:
            return 0
        visiting.add(node)
        value = 1 + max((depth(p) for p in parents[node]), default=-1)
        visiting.discard(node)
        result[node] = value
        return value

    for i in ids:
        # iterative warm-up keeps recursion shallow on long chains
        stack = [i]
        while stack:
            top = stack[-1]
            pending = [p for p in parents[top] if p not in result]
            if pending and len(stack) < 500:
                stack.extend(pending)
            else:
                stack.pop()
                depth(top)
    return result


def phase_status(states) -> str:
    states = list(states)
    if any(s in BAD_STATES for s in states):
        return "blocked"
    if states and all(s == "completed" for s in states):
        return "complete"
    if any(s in ACTIVE_STATES for s in states):
        return "active"
    return "pending"


def build_layout(tasks, dependencies) -> GraphLayout:
    """Layer the DAG by dependency depth, order each layer by the mean position of its parents, and derive edges.

    Edge kinds: 'parallel' when a prerequisite fans out to several steps that can run side by side, otherwise
    'dependency'. 'feedback' edges (purple, dashed) are only drawn from a step in the changes_required state back to
    the steps it depends on, because that is the one revision relationship the API states explicitly.
    """
    layout = GraphLayout()
    tasks = [t for t in tasks if isinstance(t, dict) and isinstance(t.get("id"), str)]
    shown = tasks[:MAX_NODES]
    layout.hidden = len(tasks) - len(shown)
    if not shown:
        return layout
    by_id = {t["id"]: t for t in shown}
    pairs = [(d["task_id"], d["depends_on_id"]) for d in dependencies
             if isinstance(d, dict) and d.get("task_id") in by_id and d.get("depends_on_id") in by_id]
    depth = depths(list(by_id), pairs)
    layers: dict = {}
    for task in shown:
        layers.setdefault(depth[task["id"]], []).append(task["id"])
    parents = {i: [p for c, p in pairs if c == i] for i in by_id}
    children = {i: [c for c, p in pairs if p == i] for i in by_id}
    index_of: dict = {}
    for level in sorted(layers):
        row = layers[level]
        if level:
            row.sort(key=lambda i: (sum(index_of.get(p, 0) for p in parents[i]) / max(1, len(parents[i])), i))
        for position, i in enumerate(row):
            index_of[i] = position
    widest = max(len(row) for row in layers.values())
    layout.width = widest * NODE_W + (widest - 1) * GAP_X
    layout.height = len(layers) * NODE_H + (len(layers) - 1) * GAP_Y
    for level in sorted(layers):
        row = layers[level]
        offset = (layout.width - (len(row) * NODE_W + (len(row) - 1) * GAP_X)) / 2
        for position, i in enumerate(row):
            task = by_id[i]
            state = task.get("state") if isinstance(task.get("state"), str) else "created"
            badge, tone = narrative.task_badge(state)
            layout.nodes[i] = Node(
                id=i, node_id=narrative.clean(task.get("node_id"), 200), name=narrative.humanize_node(task.get("node_id", i)),
                state=state, badge=badge, tone=tone, phase=level,
                attempts=task.get("attempt_count") if type(task.get("attempt_count")) is int else 0,
                layer_index=position, x=offset + position * (NODE_W + GAP_X), y=level * (NODE_H + GAP_Y),
                progress=PROGRESS.get(state, 0))
            layout.order.append(i)
    names = {i: n.name for i, n in layout.nodes.items()}
    for i, node in list(layout.nodes.items()):
        layout.nodes[i] = Node(**{**node.__dict__, "waits_for": tuple(names[p] for p in parents[i]),
                                  "hands_to": tuple(names[c] for c in children[i])})
    for child, parent in pairs:
        kind = "parallel" if len(children[parent]) > 1 else "dependency"
        bad = layout.nodes[child].state in BAD_STATES or layout.nodes[parent].state in BAD_STATES
        layout.edges.append(Edge(parent, child, kind, bad))
    for i, node in layout.nodes.items():
        if node.state == "changes_required":
            layout.edges.extend(Edge(i, p, "feedback") for p in parents[i])
    for level in sorted(layers):
        states = [layout.nodes[i].state for i in layers[level]]
        layout.phases.append(Phase(level, narrative.phase_label(level), phase_status(states), tuple(layers[level])))
    return layout


def is_check(node_id) -> bool:
    text = node_id.lower() if isinstance(node_id, str) else ""
    return any(word in text for word in CHECK_WORDS)


def run_approvals(rows, run_id) -> list:
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("run_id"), str) and r.get("run_id") == run_id]


@dataclass(frozen=True)
class Gate:
    state: str  # none | pending | approved | rejected | expired | revoked
    title: str
    body: str
    row: dict | None = None
    decidable: bool = False


GATE_TEXT = {
    "none": ("No approval needed right now", "Nothing in this run is waiting for you. If an agent ever needs to do something "
             "risky, it stops here first and asks you. Without your clear yes, it does not go ahead (fail closed)."),
    "pending": ("Your approval is needed", "An agent wants to {operation} ({target}). Nothing will happen until you decide. "
                "If you approve, it carries on. If you reject, it stops and nothing is changed."),
    "approved": ("You approved this", "Your approval is recorded. The agent may now continue with: {operation} ({target})."),
    "rejected": ("You rejected this", "Your rejection is recorded. The agent did not go ahead and nothing was changed."),
    "expired": ("The request expired", "No decision arrived in time, so it was treated as a no. Nothing was changed. "
                "Start the run again if you still want it."),
    "revoked": ("The approval was withdrawn", "A previous approval was taken back. The agent will not go ahead."),
}


def _approval_sort(row):
    return (str(row.get("requested_at") or ""), str(row.get("id") or ""))


def gate_for_run(rows, run_id) -> Gate:
    """Gate state from the real approval rows of this run: a pending request wins; otherwise the most recent decision."""
    mine = sorted(run_approvals(rows, run_id), key=_approval_sort)
    pending = [r for r in mine if r.get("state") == "requested"]
    row = pending[-1] if pending else (mine[-1] if mine else None)
    raw = row.get("state") if row is not None else None
    state = "none" if not isinstance(raw, str) else {"requested": "pending"}.get(raw, raw)
    if state not in GATE_TEXT:
        state = "none"
    title, body = GATE_TEXT[state]
    operation = narrative.clean((row or {}).get("operation"), 60) or "continue"
    target = narrative.clean((row or {}).get("target"), 80) or "this run"
    return Gate(state, title, body.format(operation=operation, target=target), row, decidable=state == "pending")


def metrics(run, layout: GraphLayout, message_count: int, rows) -> list:
    """The five header tiles as (title, value, caption). Labels stay honest: 'Checks passed' is only used when some steps
    are identifiable as review/verification steps; otherwise the tile is plain step counts."""
    states = [n.state for n in layout.nodes.values()]
    total = len(states)
    run_id = (run or {}).get("id")
    state = (run or {}).get("state")
    status = narrative.run_label(state) if run else "No run"
    checks = [n for n in layout.nodes.values() if is_check(n.node_id)]
    if checks:
        done = sum(1 for n in checks if n.state == "completed")
        third = ("Checks passed", f"{done} / {len(checks)}", "Review and verification steps")
    else:
        done = sum(1 for s in states if s == "completed")
        third = ("Steps done", f"{done} / {total}", "No separate checks in this run")
    gate = gate_for_run(rows, run_id) if run else None
    gate_value = {"none": "Not needed", "pending": "Waiting for you", "approved": "Approved", "rejected": "Rejected",
                  "expired": "Expired", "revoked": "Withdrawn"}[gate.state] if gate else "None"
    active = sum(1 for s in states if s in RUNNING_STATES)
    return [("Run status", status, "Overall progress"),
            ("Agents working", f"{active} / {total}", "Steps being worked on now"),
            third,
            ("Messages exchanged", str(message_count), "Between agents, this run"),
            ("Approval gate", gate_value, "A person decides risky steps")]
