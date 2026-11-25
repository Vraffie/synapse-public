"""Live-daemon acceptance tests for the desktop Control Center.

A REAL agentd subprocess (mock provider, approval steps attached in that process only, as scripts/demo_mock.py does) is
driven through queued -> running (parallel) -> awaiting approval -> approved -> completed, plus rejected, cancelled,
fail-closed and daemon-restart paths, while the REAL Poller / MainWindow / ControlCenter run offscreen. At every stage the
DISPLAYED state is compared with what the daemon reports over its HTTP API. Tokens are never printed or asserted on.

Set SYNAPSE_LIVE_SHOTS=<dir> to also save widget PNGs per stage (never committed).
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("SYNAPSE_REDUCED_MOTION", "1")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication
from synapse.cli.__main__ import _get
from synapse.desktop import approval_decision, narrative
from synapse.desktop.__main__ import MainWindow, Poller

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import demo_mock  # noqa: E402  (the real-daemon harness: Demo.ctl/provision/decide helpers)

CHILD = '''
import sys
from pathlib import Path
sys.path.insert(0, %(scripts)r)
import demo_mock as dm
from synapse.daemon import __main__ as daemon
from synapse.daemon.mock_runtime import MockRuntime
from synapse.providers.mock import MockProvider

state = Path(sys.argv[1])
release = state.parent / "release"
# workflow id -> (scenarios, gated node or None)
PLANS = {
    "live-gated": ({"build-a": "slow", "build-b": "slow"}, "review"),
    "live-reject": ({"build-a": "slow", "build-b": "slow"}, "review"),
    "live-cancel": ({"build-a": "slow", "build-b": "slow"}, None),
}
orig_start, orig_init, orig_stream = MockRuntime.start, MockRuntime.__init__, MockProvider.stream

def init(self, *a, **k):
    orig_init(self, *a, **k)
    self._park_poll = 0.2

def start(self, run_id, key, scenarios=None, approval_steps=None):
    row = self.db.execute("SELECT workflow_id FROM runs WHERE id=?", (run_id,)).fetchone()
    plan = PLANS.get(row[0]) if row else None
    if plan is not None and scenarios is None and approval_steps is None:
        scenarios = plan[0]
        if plan[1]:
            approval_steps = {plan[1]: {**dm.BINDING, "ttl_seconds": 600}}
    return orig_start(self, run_id, key, scenarios, approval_steps)

async def stream(self, handle):
    import asyncio
    slow = len(self._execution(handle).scenario.chunks) > 100
    async for event in orig_stream(self, handle):
        yield event
        if slow and event.kind == "started":  # hold slow tasks "running" until the test releases them
            while not release.exists() and not self._execution(handle).cancelled:
                await asyncio.sleep(0.02)

MockRuntime.__init__, MockRuntime.start, MockProvider.stream = init, start, stream
daemon.run(state, Path(%(migrations)r))
'''

NODES = [{"id": "plan", "dependencies": []}, {"id": "build-a", "dependencies": ["plan"]},
         {"id": "build-b", "dependencies": ["plan"]}, {"id": "review", "dependencies": ["build-a", "build-b"]}]
PLAIN_NODES = [{"id": "build-a", "dependencies": []}, {"id": "build-b", "dependencies": []}]


def spin(app, predicate, seconds=10, what="condition"):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    app.processEvents()
    assert predicate(), "timed out waiting for " + what


class Secret:
    """A token that never shows up in a traceback, assertion diff or log."""
    __slots__ = ("_value",)

    def __init__(self, value):
        self._value = value

    def __repr__(self):
        return "<secret>"

    __str__ = __repr__

    def reveal(self):
        return self._value


class Live(demo_mock.Demo):
    """Real daemon subprocess + desktop window, with API-truth readers."""

    def __init__(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir(mode=0o700)
        super().__init__(state, quiet=True)
        self.tmp = tmp_path
        self.child = tmp_path / "child.py"
        self.child.write_text(CHILD % {"scripts": str(ROOT / "scripts"), "migrations": str(ROOT / "migrations")})
        self.app = QApplication.instance() or QApplication([])
        self.window = None
        self.shots = Path(os.environ["SYNAPSE_LIVE_SHOTS"]) if os.environ.get("SYNAPSE_LIVE_SHOTS") else None
        self.release = tmp_path / "release"

    # -- daemon
    def start_daemon(self):
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen([sys.executable, str(self.child), str(self.state)], stdout=log, stderr=log, env=demo_mock.child_env())
        log.close()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert self.proc.poll() is None, "daemon exited early"
            if self.ctl("health", check=False).returncode == 0:
                return
            time.sleep(0.05)
        raise AssertionError("daemon did not become ready")

    def publish(self, workflow_id, nodes=NODES):
        import json
        path = self.tmp / f"{workflow_id}.json"
        path.write_text(json.dumps({"workflow_id": workflow_id, "version": "1", "nodes": nodes}))
        self.ctl("publish-workflow", str(path), "--idempotency-key", "pub-" + workflow_id, "--correlation-id", "live")
        path.unlink()

    def queue(self, workflow_id, tag=""):
        out = self.ctl_json("queue-run", workflow_id, "1", "--idempotency-key", f"q-{workflow_id}{tag}", "--correlation-id", "live")
        return out["run_id"]

    # -- API truth
    def api(self, endpoint):
        return _get(self.state, endpoint)

    def api_tasks(self, run_id):
        return {t["node_id"]: t["state"] for t in self.api(f"/api/v1/runs/{run_id}/tasks")["tasks"]}

    def api_run(self, run_id):
        return self.api(f"/api/v1/runs/{run_id}")["run"]

    def api_approval(self, run_id):
        rows = [a for a in self.api("/api/v1/approvals?limit=100")["approvals"] if a["run_id"] == run_id]
        return rows[-1] if rows else None

    # -- desktop
    def open_window(self):
        self.window = MainWindow(self.state, poller=Poller(self.state, interval=0.05))
        self.window.resize(1280, 900)
        self.window.show()
        return self.window

    @property
    def cc(self):
        return self.window.control_center

    def select(self, run_id):
        self.spin(lambda: self.cc.run_combo.findData(run_id) >= 0, "run to appear in the picker")
        self.cc.run_combo.setCurrentIndex(self.cc.run_combo.findData(run_id))
        self.cc._combo_activated(self.cc.run_combo.currentIndex())

    def spin(self, predicate, what="condition", seconds=10):
        spin(self.app, predicate, seconds, what)

    # -- displayed state
    def tile(self, index):
        return tuple(self.cc.metric_rows[index])

    def node_states(self):
        return {n.node_id: n.state for n in self.cc.layout_model.nodes.values()}

    def topology_badges(self):
        """What the topology scene draws: node id -> badge, read from the live graphics items."""
        return {item.node.node_id: item.node.badge for item in self.cc.topology.node_items.values()}

    def feed_texts(self):
        texts = []
        for i in range(self.cc.feed_box.count()):
            widget = self.cc.feed_box.itemAt(i).widget()
            texts.extend(label.text() for label in widget.findChildren(type(self.cc.gate_title)))
        return texts

    def combo_items(self):
        return [(self.cc.run_combo.itemData(i), self.cc.run_combo.itemText(i)) for i in range(self.cc.run_combo.count())]

    def shot(self, name):
        if self.shots is None:
            return
        self.shots.mkdir(parents=True, exist_ok=True)
        self.app.processEvents()
        from PySide6.QtWidgets import QScrollArea
        self.cc.findChild(QScrollArea).widget().grab().save(str(self.shots / f"{name}.png"))  # whole content, below the fold too
        self.window.grab().save(str(self.shots / f"{name}-window.png"))

    def decide_with(self, token, decision="approved", reason="live test"):
        """Click the Control Center button; the modal token dialog is replaced by a canned entry (token never printed)."""
        calls = []
        original = approval_decision.prompt_decision
        approval_decision.prompt_decision = lambda *a, **k: (calls.append(1), (token.reveal(), reason))[1]
        try:
            button = self.cc.approve_button if decision == "approved" else self.cc.reject_button
            assert button.isEnabled()
            button.click()  # decides from the gate's own approval row: no wait for the Approvals list poll
            assert calls, "token dialog was not requested"
        finally:
            approval_decision.prompt_decision = original

    def provision(self):
        super().provision()
        self.approver_secret = Secret(self.approver)
        self.approver = ""  # only the wrapper keeps it

    def wait_decided(self):
        self.spin(lambda: self.window._decision_pending is None, "decision round trip to finish")

    def close(self):
        if self.window is not None:
            self.window.close()
            assert not self.window.poller.isRunning()
            self.window.deleteLater()
            self.window = None
        self.stop_daemon(signal.SIGKILL)
        self.app.processEvents()


@pytest.fixture
def live(tmp_path):
    harness = Live(tmp_path)
    try:
        harness.start_daemon()
        harness.provision()
        for workflow in ("live-gated", "live-reject"):
            harness.publish(workflow)
        harness.publish("live-cancel", PLAIN_NODES)
        harness.publish("live-queued", PLAIN_NODES)
        yield harness
    finally:
        harness.close()


def gate_shows(live, state):
    return live.cc.gate.state == state


def test_gated_run_lifecycle_approve_through_control_center(live):
    queued_only = live.queue("live-queued")
    run = live.queue("live-gated")
    window = live.open_window()
    cc = live.cc
    live.spin(lambda: len(live.combo_items()) == 2, "run picker to list both runs")

    # -- queued: picker + tiles show a run that has not started; nothing to approve
    assert {rid for rid, _ in live.combo_items()} == {queued_only, run}
    assert all(text.startswith("Ready to start") for _, text in live.combo_items())
    live.select(run)
    live.spin(lambda: cc.run and cc.run["state"] == "queued" and len(cc.layout_model.nodes) == 4, "queued run snapshot")
    assert live.api_run(run)["state"] == "queued"
    assert live.tile(0)[:2] == ("Run status", "Ready to start")
    assert live.node_states() == live.api_tasks(run)
    assert not cc.approve_button.isEnabled() and not cc.reject_button.isEnabled()
    assert cc.gate_title.text().endswith("No approval needed right now")
    live.shot("1-queued")

    # -- running: two parallel tasks running (held by the harness), plan done, review waiting
    live.mock_start(run)
    live.spin(lambda: live.api_tasks(run).get("build-a") == "running" and live.api_tasks(run).get("build-b") == "running", "parallel tasks running (API)")
    truth = live.api_tasks(run)
    live.spin(lambda: live.node_states() == truth and live.tile(0)[1] == "Running", "UI to show running state")
    assert truth == {"plan": "completed", "build-a": "running", "build-b": "running", "review": "waiting_dependencies"}
    assert live.tile(1)[:2] == ("Agents working", "2 / 4")
    assert live.topology_badges() == {"plan": "DONE", "build-a": "RUNNING", "build-b": "RUNNING", "review": "IDLE"}
    live.spin(lambda: any(t.startswith("Running") for _, t in live.combo_items()), "picker to label the live run Running")
    live.spin(lambda: any("working now" in t for t in live.feed_texts()), "feed row for a step starting")
    live.shot("2-running-parallel")

    # -- awaiting approval
    live.release.write_text("go")
    live.spin(lambda: live.api_tasks(run).get("review") == "awaiting_approval", "review parked (API)")
    approval = live.api_approval(run)
    assert approval["state"] == "requested"
    live.spin(lambda: cc.gate.state == "pending" and cc.approve_button.isEnabled(), "gate card to show the pending approval")
    # The gate (approvals poll) and the topology (run snapshot poll) are separate reads in one poller cycle: the snapshot
    # can predate the park by one cycle, so wait for the topology to catch up instead of sampling it once.
    truth = live.api_tasks(run)
    assert truth == {"plan": "completed", "build-a": "completed", "build-b": "completed", "review": "awaiting_approval"}
    live.spin(lambda: live.node_states() == truth, "topology to show the parked review")
    assert "approval is needed" in cc.gate_title.text()
    assert approval["operation"] in cc.gate_body.text() and approval["target"] in cc.gate_body.text()
    assert cc.reject_button.isEnabled() and "Waiting for you" in cc.gate_status.text()
    assert live.tile(4)[1] == "Waiting for you"
    assert live.tile(0)[1] == "Needs your approval" or live.tile(0)[1] == "Running"
    live.spin(lambda: any("waiting for your approval" in t for t in live.feed_texts()), "feed row for the approval gate")
    assert any("Approval requested" in t for t in live.feed_texts())
    live.shot("3-awaiting-approval")

    # -- fail closed 1: a wrong approver token is refused and the gate stays open
    live.decide_with(Secret("x" * 43))
    live.wait_decided()
    assert "not allowed" in cc.gate_status.text()
    assert live.api_approval(run)["state"] == "requested" and live.api_tasks(run)["review"] == "awaiting_approval"
    assert cc.approve_button.isEnabled()
    # -- fail closed 2: the operator token is not an approver token
    operator = (live.state / "operator.token").read_text().strip()
    live.decide_with(Secret(operator))
    del operator
    live.wait_decided()
    assert "not allowed" in cc.gate_status.text() or "credentials" in cc.gate_status.text()
    assert live.api_approval(run)["state"] == "requested"
    # -- fail closed 3: cancelling the token dialog submits nothing
    original = approval_decision.prompt_decision
    approval_decision.prompt_decision = lambda *a, **k: None
    try:
        cc.approve_button.click()
    finally:
        approval_decision.prompt_decision = original
    live.app.processEvents()
    assert live.api_approval(run)["state"] == "requested"

    # -- approved through the real decision path (real approver token)
    live.decide_with(live.approver_secret, "approved")
    live.spin(lambda: live.api_approval(run)["state"] == "approved", "approval accepted by daemon")
    live.spin(lambda: live.api_run(run)["state"] == "succeeded", "run to complete")
    truth = live.api_tasks(run)
    assert set(truth.values()) == {"completed"}
    live.spin(lambda: live.node_states() == truth and live.tile(0)[1] == "Complete" and cc.gate.state == "approved", "UI to show completion")
    assert not cc.approve_button.isEnabled() and not cc.reject_button.isEnabled()
    assert "You approved this" in cc.gate_title.text() and live.tile(4)[1] == "Approved"
    assert live.tile(2)[:2] == ("Checks passed", "1 / 1")
    assert set(live.topology_badges().values()) == {"DONE"}
    live.spin(lambda: any(t.startswith("Complete") for _, t in live.combo_items()), "picker to label the run Complete")
    assert not window.control_center.stop_button.isVisible()
    live.spin(lambda: any("decision was applied" in t for t in live.feed_texts()), "decision row in the feed")
    live.shot("4-completed")
    assert live.approver_secret.reveal() not in " ".join(live.outputs) + cc.gate_status.text() + " ".join(live.feed_texts())


def test_rejected_run_shows_rejection_and_failed_run(live):
    run = live.queue("live-reject")
    live.release.write_text("go")
    live.open_window()
    cc = live.cc
    live.mock_start(run)
    live.spin(lambda: live.api_tasks(run).get("review") == "awaiting_approval", "review parked")
    live.select(run)
    live.spin(lambda: cc.gate.state == "pending" and cc.approve_button.isEnabled(), "pending gate")
    live.decide_with(live.approver_secret, "rejected", "not today")
    live.spin(lambda: live.api_approval(run)["state"] == "rejected", "rejection recorded")
    live.spin(lambda: live.api_run(run)["state"] in ("failed", "cancelled", "succeeded"), "run to settle", 20)
    truth, run_state = live.api_tasks(run), live.api_run(run)["state"]
    assert truth["review"] != "completed" and run_state != "succeeded"
    live.spin(lambda: cc.gate.state == "rejected" and live.node_states() == truth, "UI to show rejection")
    assert "You rejected this" in cc.gate_title.text() and live.tile(4)[1] == "Rejected"
    assert not cc.approve_button.isEnabled() and not cc.reject_button.isEnabled()
    assert live.tile(0)[1] == narrative.run_label(run_state)
    assert narrative.task_badge(truth["review"])[0] == live.topology_badges()["review"]
    live.shot("5-rejected")


def test_cancelled_run_is_shown_as_stopped(live):
    run = live.queue("live-cancel")
    live.open_window()
    cc = live.cc
    live.mock_start(run)
    live.spin(lambda: set(live.api_tasks(run).values()) == {"running"}, "both tasks running")
    live.select(run)
    live.spin(lambda: live.node_states() == live.api_tasks(run) and live.tile(1)[1] == "2 / 2", "UI shows two running")
    live.shot("6-cancel-before")
    # the fixed-mock cancel control lives in the Developer view; click it as an operator would
    live.window.cancel_mock_button.click()
    live.spin(lambda: live.api_run(run)["state"] == "cancelled", "run cancelled (API)")
    truth = live.api_tasks(run)
    live.spin(lambda: live.node_states() == truth and live.tile(0)[1] == "Stopped", "UI to show stopped")
    assert set(live.topology_badges().values()) <= {"STOPPED", "DONE"}
    assert live.tile(1)[1].startswith("0 /")
    assert not cc.approve_button.isEnabled()
    live.shot("7-cancelled")


def test_daemon_restart_mid_run_ui_recovers(live):
    run = live.queue("live-gated")
    live.open_window()
    cc = live.cc
    live.release.write_text("go")
    live.mock_start(run)
    live.spin(lambda: live.api_tasks(run).get("review") == "awaiting_approval", "review parked")
    live.select(run)
    live.spin(lambda: cc.gate.state == "pending" and cc.run is not None and live.node_states() == live.api_tasks(run), "snapshot and pending gate before the restart")
    live.spin(lambda: live.window.connection_state == "connected", "connected")

    live.stop_daemon(signal.SIGKILL)
    live.spin(lambda: live.window.connection_state == "reconnecting", "UI to notice the daemon is gone", 15)
    live.shot("8-daemon-down")
    assert live.tile(0)[1] == "Running" and cc.gate.state == "pending" and len(cc.layout_model.nodes) == 4  # last result stays visible
    # fail closed while down: the last gate is still shown, but deciding is disabled (with the reason) and the alert bar is up
    assert not cc.approve_button.isEnabled() and not cc.reject_button.isEnabled() and "offline" in cc.approve_button.toolTip()
    assert live.window.offline_bar.isVisible() and not cc.composer.submit_button.isEnabled()

    live.start_daemon()
    live.spin(lambda: live.window.connection_state == "connected", "UI to reconnect", 20)
    live.spin(lambda: live.api_run(run)["state"] == "running" and live.api_tasks(run)["review"] == "awaiting_approval", "parked run survives (API)")
    live.window.refresh()
    live.spin(lambda: cc.gate.state == "pending" and cc.approve_button.isEnabled() and live.node_states() == live.api_tasks(run), "gate and graph recovered")
    assert live.api_approval(run)["state"] == "requested"
    live.shot("9-after-restart")
    # and the run can still be approved after the restart
    live.decide_with(live.approver_secret, "approved")
    live.spin(lambda: live.api_run(run)["state"] == "succeeded", "run completes after restart", 20)
    live.spin(lambda: cc.gate.state == "approved" and live.tile(0)[1] == "Complete", "UI shows completion after restart")


def test_feed_has_no_raw_event_names_for_live_events(live):
    """Every event type the real daemon emits for a stopped run must read as a sentence, never a dotted internal name."""
    run = live.queue("live-cancel")
    live.open_window()
    live.mock_start(run)
    live.spin(lambda: set(live.api_tasks(run).values()) == {"running"}, "both tasks running")
    live.select(run)
    live.spin(lambda: live.window.cancel_mock_button.isEnabled(), "cancel control")
    live.window.cancel_mock_button.click()
    live.spin(lambda: live.api_run(run)["state"] == "cancelled", "cancelled")
    live.spin(lambda: live.tile(0)[1] == "Stopped" and len(live.feed_texts()) >= 8, "feed to fill")
    assert [text for text in live.feed_texts() if text.startswith("Something happened")] == []
