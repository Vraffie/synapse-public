import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from synapse.desktop.__main__ import MAX_EVENT_ROWS, MainWindow, Poller, backoff_delay


def wait(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert predicate()


def test_backoff_delay_grows_and_caps():
    assert [backoff_delay(1.0, n) for n in (1, 2, 3, 4, 5, 6)] == [1, 2, 4, 8, 15, 15]
    assert backoff_delay(1.0, 10**6) == 15.0
    assert backoff_delay(0.01, 1) == 0.01


def test_reconnect_backoff_state_and_lossless_resume(tmp_path):
    app = QApplication.instance() or QApplication([])
    outage = [True]
    drop_mid_page = [True]
    attempts = []
    states = []

    def fetch(_dir, endpoint):
        if "health" in endpoint:
            if outage[0]:
                attempts.append(time.monotonic())
                raise OSError("socket gone")
            return {"status": "ok"}
        if "/events" in endpoint:
            after = int(endpoint.split("after=")[1].split("&")[0])
            events = [{"sequence": n, "type": "t", "aggregate_id": "a"} for n in range(after + 1, 6)]
            return {"events": events}
        return {"approvals": []}

    poller = Poller(tmp_path, fetch=fetch, interval=0.02)
    poller.max_backoff = 0.2
    window = MainWindow(tmp_path, poller=poller)
    poller.connection.connect(lambda state, failures, delay: states.append((state, failures, delay)))
    wait(app, lambda: len(attempts) >= 4)
    assert "reconnecting" in window.health_label.text()
    assert "attempt" in window.health_label.text()
    assert "retry" in window.connection_detail.text()
    gaps = [b - a for a, b in zip(attempts, attempts[1:])]
    assert gaps[2] > gaps[0]  # delays grow while the daemon stays down
    assert window.events.count() == 0
    outage[0] = False
    wait(app, lambda: window.events.count() == 5)
    wait(app, lambda: window.health_label.text() == "Connected")
    assert [window.events.item(i).text().split()[0] for i in range(5)] == ["1", "2", "3", "4", "5"]
    assert "Resumed" in window.connection_detail.text()
    # a second outage after delivery resumes from the cursor: no duplicates
    outage[0] = True
    wait(app, lambda: "reconnecting" in window.health_label.text())
    outage[0] = False
    wait(app, lambda: window.health_label.text() == "Connected")
    time.sleep(0.1)
    app.processEvents()
    assert window.events.count() == 5
    assert poller.cursor == 5
    assert states[0][0] == "reconnecting" and states[0][1] == 1
    window.close()


def test_failure_mid_page_does_not_lose_or_duplicate_events(tmp_path):
    app = QApplication.instance() or QApplication([])
    served = {"fail_once": True}

    def fetch(_dir, endpoint):
        if "health" in endpoint:
            return {"status": "ok"}
        if "/events" in endpoint:
            after = int(endpoint.split("after=")[1].split("&")[0])
            if after == 0 and served["fail_once"]:
                served["fail_once"] = False
                # sequences 1,2 are good; the third is malformed and aborts the page
                return {"events": [{"sequence": 1}, {"sequence": 2}, {"sequence": "bad"}]}
            return {"events": [{"sequence": n} for n in range(after + 1, 5)]}
        return {"approvals": []}

    poller = Poller(tmp_path, fetch=fetch, interval=0.01)
    window = MainWindow(tmp_path, poller=poller)
    wait(app, lambda: window.events.count() == 4)
    time.sleep(0.1)
    app.processEvents()
    assert [window.events.item(i).text().split()[0] for i in range(4)] == ["1", "2", "3", "4"]
    window.close()


def test_long_event_backlog_is_capped_and_caught_up_without_idle_waits(tmp_path):
    app = QApplication.instance() or QApplication([])
    total = MAX_EVENT_ROWS + 450

    def fetch(_dir, endpoint):
        if "health" in endpoint:
            return {"status": "ok"}
        if "/events" in endpoint:
            after = int(endpoint.split("after=")[1].split("&")[0])
            return {"events": [{"sequence": n} for n in range(after + 1, min(total, after + 100) + 1)]}
        return {"approvals": []}

    poller = Poller(tmp_path, fetch=fetch, interval=5.0)  # a full interval per page would time out the test
    window = MainWindow(tmp_path, poller=poller)
    wait(app, lambda: window._events_seen == total, timeout=10)
    assert window.events.count() == MAX_EVENT_ROWS
    assert window.events.item(MAX_EVENT_ROWS - 1).text().startswith(str(total))
    assert "450 older dropped" in window.events_status.text()
    window.close()


def test_run_detail_lists_tasks_parked_approvals_and_review_jumps_to_decision(tmp_path):
    app = QApplication.instance() or QApplication([])
    run_calls = []

    def fetch(_dir, endpoint):
        if "health" in endpoint:
            return {"status": "ok"}
        if "/events" in endpoint:
            return {"events": []}
        if "/approvals" in endpoint:
            return {"approvals": [
                {"id": "ap-1", "run_id": "run", "state": "requested", "operation": "git.push", "target": "origin/main"},
                {"id": "ap-2", "run_id": "other", "state": "requested", "operation": "x", "target": "y"},
                {"id": "ap-3", "run_id": "run", "state": "approved", "operation": "old", "target": "old"}]}
        if endpoint.endswith("/threads?after=0&limit=100"):
            return {"threads": []}
        if "/runs/run" in endpoint:
            run_calls.append(endpoint)
            return {"run": {"id": "run", "state": "running"},
                    "tasks": [{"id": "a", "node_id": "build", "state": "completed", "attempt_count": 1},
                              {"id": "b", "node_id": "deploy", "state": "awaiting_approval", "attempt_count": 1}],
                    "dependencies": [{"task_id": "b", "depends_on_id": "a"}]}
        raise AssertionError(endpoint)

    poller = Poller(tmp_path, fetch=fetch, interval=0.01)
    window = MainWindow(tmp_path, poller=poller)
    window.run_id.setText("run")
    window._select_run()
    wait(app, lambda: window.parked_list.count() == 1 and window.task_list.count() == 2)
    assert "deploy  awaiting_approval" in window.task_list.item(1).text()
    row = window.parked_list.item(0).text()
    assert "ap-1" in row and "git.push" in row and "origin/main" in row
    assert "1 task(s) awaiting approval" in window.parked_status.text()
    assert window.recent_runs.count() == 1 and "running" in window.recent_runs.item(0).text()
    assert not window.review_approval_button.isEnabled()
    window.parked_list.setCurrentRow(0)
    assert window.review_approval_button.isEnabled()
    window.review_approval_button.click()
    assert window.tabs.currentIndex() == 3
    assert window._selected_approval_id() == "ap-1"
    # unchanged snapshots must not rebuild the scene every poll
    items = window.graph_scene.items()
    n = len(run_calls)
    wait(app, lambda: len(run_calls) > n + 2)
    assert window.graph_scene.items() == items
    window.close()


def test_task_list_is_capped(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path, poller=Poller(tmp_path, fetch=lambda *_: (_ for _ in ()).throw(OSError()), interval=0.01))
    tasks = [{"id": f"t{i}", "node_id": f"n{i}", "state": "ready"} for i in range(900)]
    window._active_run = "r"
    window._show_run("r", window._active_generation, {"run": {"id": "r", "state": "running"}, "tasks": tasks, "dependencies": []})
    assert window.task_list.count() == 200
    assert "showing first 200" in window.task_status.text()
    window.close()
