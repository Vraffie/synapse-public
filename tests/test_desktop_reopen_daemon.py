"""Real daemon runs continue while their read-only desktop is closed."""
import os
import math
from pathlib import Path
import signal
import subprocess
import sys
import time

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QGraphicsLineItem, QGraphicsTextItem
from synapse.desktop.__main__ import MainWindow, Poller

ROOT = Path(__file__).resolve().parents[1]


def test_reopen_desktop_reconstructs_displayed_run_after_daemon_continues(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    harness = tmp_path / "harness"
    harness.mkdir()
    release = tmp_path / "release"
    # Delay the real deterministic mock stream only. No daemon API, state,
    # event, scheduling, verification, or desktop response is replaced.
    (harness / "sitecustomize.py").write_text(f'''
import asyncio
from pathlib import Path
from synapse.providers.mock import MockProvider
_original = MockProvider.stream
async def delayed(self, handle):
    while not Path({str(release)!r}).exists():
        await asyncio.sleep(.02)
    async for event in _original(self, handle):
        yield event
MockProvider.stream = delayed
''')
    env = dict(os.environ)
    env["PYTHONPATH"] = str(harness) + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(state),
                                "--migrations", str(ROOT / "migrations")], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    app = QApplication.instance() or QApplication([])
    first = second = None
    try:
        deadline = time.monotonic() + 15
        while (not (state / "operator.token").exists() or not (state / "agentd.sock").exists()) and time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("daemon exited before creating token")
            time.sleep(.02)
        token = (state / "operator.token").read_text()
        headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "publish", "X-Correlation-ID": "trace"}
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(state / "agentd.sock")), base_url="http://localhost", timeout=8) as client:
            while time.monotonic() < deadline:
                try:
                    if client.get("/api/v1/system/health", headers=headers).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(.02)
            else:
                raise AssertionError("daemon did not become healthy")
            response = client.post("/api/v1/workflows", headers=headers,
                                   json={"workflow_id": "wf", "version": "1", "nodes": [
                                       {"id": "build", "dependencies": []},
                                       {"id": "review", "dependencies": ["build"]}]})
            assert response.status_code == 201
            headers["Idempotency-Key"] = "queue"
            response = client.post("/api/v1/runs", headers=headers,
                                   json={"workflow_id": "wf", "workflow_version": "1"})
            assert response.status_code == 202
            run_id = response.json()["run_id"]
            headers["Idempotency-Key"] = "start"
            assert client.post(f"/api/v1/runs/{run_id}/mock-start", headers=headers).status_code == 202
            first = MainWindow(state, poller=Poller(state, interval=.03))
            first.show()
            first.run_id.setText(run_id)
            first._select_run()
            while time.monotonic() < deadline:
                app.processEvents()
                snapshot = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
                if snapshot["run"]["state"] == "running" and any(t["state"] == "running" for t in snapshot["tasks"]) and "running" in first.run_status.text():
                    break
                time.sleep(.02)
            assert snapshot["run"]["state"] == "running"
            assert any(t["state"] == "running" for t in snapshot["tasks"])
            assert "running" in first.run_status.text()
            first.close()
            assert not first.poller.isRunning()
            assert process.poll() is None
            assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["run"]["state"] == "running"
            release.touch()
            while time.monotonic() < deadline:
                snapshot = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
                if snapshot["run"]["state"] == "succeeded":
                    break
                time.sleep(.02)
            assert snapshot["run"]["state"] == "succeeded"
            assert len(snapshot["dependencies"]) == 1
            assert all(t["state"] == "completed" for t in snapshot["tasks"])
            events = client.get("/api/v1/events?after=0&limit=1000", headers=headers).json()["events"]
            assert events and events[-1]["sequence"] == snapshot["last_sequence"]
            # The first desktop persisted its event cursor; the reopened one resumes after it (no replay of
            # events it already delivered). The run graph is still rebuilt from the daemon's snapshot.
            from synapse.desktop.cursor_store import cursor_path, read_cursor
            saved = read_cursor(cursor_path(state))
            assert saved is not None and saved.sequence < events[-1]["sequence"]
            events = [event for event in events if event["sequence"] > saved.sequence]
            second = MainWindow(state, poller=Poller(state, interval=.03))
            second.show()
            second.run_id.setText(run_id)
            second._select_run()
            expected_labels = {f"{t['node_id']}\n{t['state']}" for t in snapshot["tasks"]}
            while time.monotonic() < deadline:
                app.processEvents()
                labels = {item.toPlainText() for item in second.graph_scene.items() if isinstance(item, QGraphicsTextItem)}
                if ("succeeded" in second.run_status.text() and labels == expected_labels
                        and second.events.count() == len(events)):
                    break
                time.sleep(.02)
            assert "succeeded" in second.run_status.text()
            assert labels == expected_labels
            assert len(second.graph_scene.items()) == 7  # two nodes and one arrow
            label_positions = {
                item.toPlainText().split("\n", 1)[0]: item.pos()
                for item in second.graph_scene.items() if isinstance(item, QGraphicsTextItem)
            }
            task_names = {t["id"]: t["node_id"] for t in snapshot["tasks"]}
            edge, = snapshot["dependencies"]
            predecessor = label_positions[task_names[edge["depends_on_id"]]]
            successor = label_positions[task_names[edge["task_id"]]]
            # Labels are placed at (node_x + 5, node_y + 4); the arrow runs
            # from the predecessor's bottom center to the successor's top center.
            x1, y1 = predecessor.x() + 90, predecessor.y() + 51
            x2, y2 = successor.x() + 90, successor.y() - 4
            angle = math.atan2(y2 - y1, x2 - x1)
            expected_lines = {(round(x1, 5), round(y1, 5), round(x2, 5), round(y2, 5))}
            for offset in (-0.5, 0.5):
                expected_lines.add((round(x2, 5), round(y2, 5),
                                    round(x2 - 10 * math.cos(angle + offset), 5),
                                    round(y2 - 10 * math.sin(angle + offset), 5)))
            actual_lines = set()
            for item in second.graph_scene.items():
                if isinstance(item, QGraphicsLineItem):
                    line = item.line()
                    actual_lines.add(tuple(round(v, 5) for v in (line.x1(), line.y1(), line.x2(), line.y2())))
            assert actual_lines == expected_lines
            displayed_sequences = [int(second.events.item(i).text().split()[0]) for i in range(second.events.count())]
            assert displayed_sequences == [event["sequence"] for event in events]
    finally:
        for window in (first, second):
            if window is not None:
                window.close()
                assert not window.poller.isRunning()
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        process.communicate(timeout=8)
    assert process.returncode == 0
