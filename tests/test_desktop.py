import os
from pathlib import Path
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication
from synapse.desktop.__main__ import MainWindow, Poller


def test_poller_reconnects_and_resumes_cursor(tmp_path):
    app = QApplication.instance() or QApplication([])
    calls = []
    def fetch(_dir, endpoint):
        calls.append(endpoint)
        if len(calls) == 1:
            raise OSError("disconnected")
        if "health" in endpoint:
            return {"status": "ok"}
        if "after=0" in endpoint:
            return {"events": [{"sequence": 3, "type": "task.started"}]}
        return {"events": []}
    poller = Poller(Path(tmp_path), fetch=fetch, interval=0.01)
    window = MainWindow(tmp_path, poller=poller)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not any("after=3" in c for c in calls):
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert any("after=3" in c for c in calls)
    assert window.events.count() == 1
    assert "task.started" in window.events.item(0).text()
    assert window.health_label.text() == "Connected"
    window.close()
    assert not poller.isRunning()


def test_run_graph_and_approvals_poll_and_mark_stale(tmp_path):
    app = QApplication.instance() or QApplication([])
    fail = [False]
    def fetch(_dir, endpoint):
        if "health" in endpoint:
            return {"status": "ok"}
        if "events" in endpoint:
            return {"events": []}
        if "approvals" in endpoint:
            if fail[0]:
                raise OSError("lost")
            return {"approvals": [{"id": "approval", "state": "requested", "operation": "push", "target": "main"}]}
        if "/runs/run" in endpoint:
            if fail[0]:
                raise OSError("lost")
            return {"run": {"id": "run", "state": "running"},
                    "tasks": [{"id": "a", "node_id": "build", "state": "completed"},
                              {"id": "b", "node_id": "test", "state": "ready"}],
                    "dependencies": [{"task_id": "b", "depends_on_id": "a"}]}
        raise AssertionError(endpoint)
    poller = Poller(tmp_path, fetch=fetch, interval=0.01)
    window = MainWindow(tmp_path, poller=poller)
    window.run_id.setText("run")
    window._select_run()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (window.approvals_list.count() != 1 or "2 tasks" not in window.run_status.text()):
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert window.approvals_list.count() == 1
    assert "requested" in window.approvals_list.item(0).text()
    assert "2 tasks" in window.run_status.text()
    assert len(window.graph_scene.items()) == 7  # two boxes, two labels, line and two arrowhead lines
    fail[0] = True
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and "unavailable" not in window.run_status.text():
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert "unavailable" in window.run_status.text()
    assert "unavailable" in window.approvals_status.text()
    assert window.approvals_list.count() == 1
    assert len(window.graph_scene.items()) == 7
    window.close()
    assert not poller.isRunning()


def test_stale_graph_response_cannot_replace_selected_run(tmp_path):
    app = QApplication.instance() or QApplication([])
    def fetch(_dir, endpoint):
        if "health" in endpoint:
            return {"status": "ok"}
        if "events" in endpoint:
            return {"events": []}
        if "approvals" in endpoint:
            return {"approvals": []}
        raise OSError("offline")
    poller = Poller(tmp_path, fetch=fetch, interval=0.1)
    window = MainWindow(tmp_path, poller=poller)
    window.run_id.setText("new")
    window._select_run()
    window._show_run("old", 0, {"run": {"id": "old"}, "tasks": [{"id": "old-task"}], "dependencies": []})
    assert not window.graph_scene.items()
    window.run_id.setText("../other")
    window._select_run()
    assert window.run_status.text() == "Enter a valid run ID."
    window.close()


def test_run_selection_generation_discards_a_b_a_response(tmp_path):
    from threading import Event

    app = QApplication.instance() or QApplication([])
    first_started, release_first, second_started, release_second = (Event() for _ in range(4))
    count = [0]
    def fetch(_dir, endpoint):
        if "health" in endpoint:
            return {"status": "ok"}
        if "events" in endpoint:
            return {"events": []}
        if "approvals" in endpoint:
            return {"approvals": []}
        if "/runs/A" in endpoint:
            count[0] += 1
            if count[0] == 1:
                first_started.set()
                assert release_first.wait(3)
                return {"run": {"id": "A", "state": "old"}, "tasks": [], "dependencies": []}
            second_started.set()
            assert release_second.wait(3)
            return {"run": {"id": "A", "state": "new"}, "tasks": [], "dependencies": []}
        raise OSError("unexpected run")
    poller = Poller(tmp_path, fetch=fetch, interval=0.01)
    window = MainWindow(tmp_path, poller=poller)
    try:
        window.run_id.setText("A")
        window._select_run()
        assert first_started.wait(3)
        window.run_id.setText("B")
        window._select_run()
        window.run_id.setText("A")
        window._select_run()
        release_first.set()
        assert second_started.wait(3)
        app.processEvents()
        assert window.run_status.text() == "Loading run…"
        release_second.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "new" not in window.run_status.text():
            app.processEvents()
            time.sleep(0.01)
        assert "new" in window.run_status.text()
    finally:
        release_first.set()
        release_second.set()
        window.close()
        assert not poller.isRunning()


def test_desktop_reads_run_and_approvals_from_real_daemon(tmp_path):
    from datetime import datetime, timedelta, timezone
    import signal
    import subprocess
    import sys

    from synapse.infrastructure.daemon_database import daemon_database
    from synapse.persistence.approval_store import write_approval_request
    from synapse.domain.contracts import MessageEnvelope, MessageId, MessageType, RunId
    from synapse.persistence.message_store import send_message

    root = Path(__file__).resolve().parents[1]
    migrations = root / "migrations"
    with daemon_database(tmp_path / "synapse.sqlite", migrations) as db:
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES('task','run','build','created','now','now')")
        db.execute("INSERT INTO agent_definitions VALUES('agent','1',NULL,'{}')")
        for instance in ("sender", "receiver"):
            db.execute("INSERT INTO agent_instances(id,run_id,definition_id,definition_version,state,created_at) VALUES(?,'run','agent','1','running','now')", (instance,))
        db.execute("INSERT INTO threads VALUES('thread','run','Review','open','now')")
        message = MessageEnvelope(id=MessageId("msg"), run_id=RunId("run"), thread_id="thread",
                                  sender_instance_id="sender", type=MessageType.QUESTION,
                                  payload={"text": "real message"}, created_at="2026-11-20T00:00:00+00:00")
        send_message(db, message, recipients=("receiver",), principal="sender", idempotency_key="message")
        db.execute("BEGIN IMMEDIATE")
        write_approval_request(db, approval_id="approval", run_id="run", operation="push", target="repo",
                               candidate_sha256="a" * 64, policy_version="policy-1",
                               expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        db.execute("COMMIT")
    app = QApplication.instance() or QApplication([])
    poller = Poller(tmp_path, interval=0.05)
    window = MainWindow(tmp_path, poller=poller)
    proc = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(tmp_path),
                             "--migrations", str(migrations)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        window.run_id.setText("run")
        window._select_run()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            app.processEvents()
            if (window.health_label.text() == "Connected" and "1 tasks" in window.run_status.text()
                    and window.approvals_list.count() == 1):
                break
            if proc.poll() is not None:
                raise AssertionError("daemon terminated before desktop connected")
            time.sleep(0.02)
        assert window.health_label.text() == "Connected"
        assert "1 tasks" in window.run_status.text()
        assert "approval" in window.approvals_list.item(0).text()
        assert len(window.graph_scene.items()) == 2
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and window.threads_list.count() == 0:
            app.processEvents()
            time.sleep(0.01)
        assert window.threads_list.count() == 1
        window.threads_list.setCurrentRow(0)
        while time.monotonic() < deadline and window.messages_list.count() == 0:
            app.processEvents()
            time.sleep(0.01)
        assert "real message" in window.messages_list.item(0).text()
    finally:
        window.close()
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
    assert not poller.isRunning()
    assert proc.returncode == 0


def test_threads_and_messages_poll_selection_and_stale_state(tmp_path):
    app = QApplication.instance() or QApplication([])
    fail = [False]
    def fetch(_dir, endpoint):
        if "health" in endpoint:
            return {"status": "ok"}
        if "events" in endpoint:
            return {"events": []}
        if "approvals" in endpoint:
            return {"approvals": []}
        if "/threads/thread/messages" in endpoint:
            if fail[0]:
                raise OSError("offline")
            return {"messages": [{"id": "msg", "type": "question", "payload": {"text": "hello"}}]}
        if "/threads?" in endpoint:
            if fail[0]:
                raise OSError("offline")
            return {"threads": [{"id": "thread", "subject": "Review"}]}
        if "/runs/run" in endpoint:
            return {"run": {"id": "run"}, "tasks": [], "dependencies": []}
        raise OSError("unknown endpoint")
    poller = Poller(tmp_path, fetch=fetch, interval=0.01)
    window = MainWindow(tmp_path, poller=poller)
    try:
        window.run_id.setText("run")
        window._select_run()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and window.threads_list.count() == 0:
            app.processEvents()
            time.sleep(0.01)
        assert window.threads_list.count() == 1
        window._select_thread(window.threads_list.item(0))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and window.messages_list.count() == 0:
            app.processEvents()
            time.sleep(0.01)
        assert "hello" in window.messages_list.item(0).text()
        fail[0] = True
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and ("unavailable" not in window.messages_status.text() or "unavailable" not in window.threads_status.text()):
            app.processEvents()
            time.sleep(0.01)
        assert "unavailable" in window.threads_status.text()
        assert "unavailable" in window.messages_status.text()
        assert window.messages_list.count() == 1
        window.run_id.setText("another")
        window._select_run()
        window._show_messages("run", window._active_generation - 1, "thread", window._thread_generation,
                              [{"id": "old", "payload": {}}])
        assert window.messages_list.count() == 0
    finally:
        window.close()
        assert not poller.isRunning()


def test_keyboard_selection_and_stale_thread_generations(tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    app = QApplication.instance() or QApplication([])
    def fetch(_dir, endpoint):
        if "health" in endpoint:
            return {"status": "ok"}
        if "events" in endpoint:
            return {"events": []}
        if "approvals" in endpoint:
            return {"approvals": []}
        if "/threads?" in endpoint:
            return {"threads": [{"id": "A", "subject": "First"}, {"id": "B", "subject": "Second"}]}
        if "/messages?" in endpoint:
            return {"messages": []}
        return {"run": {"id": "run"}, "tasks": [], "dependencies": []}
    poller = Poller(tmp_path, fetch=fetch, interval=0.05)
    window = MainWindow(tmp_path, poller=poller)
    try:
        window.run_id.setText("run")
        window._select_run()
        deadline = time.monotonic() + 3
        while window.threads_list.count() < 2 and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert window.threads_list.count() == 2
        window.threads_list.setCurrentRow(0)
        first_a = window._thread_generation
        assert window._active_thread == "A"
        QTest.keyClick(window.threads_list, Qt.Key.Key_Down)
        assert window._active_thread == "B"
        QTest.keyClick(window.threads_list, Qt.Key.Key_Up)
        assert window._active_thread == "A" and window._thread_generation > first_a
        current = window._thread_generation
        window._show_messages("run", window._active_generation, "A", current, [{"id": "new", "payload": {}}])
        window._show_messages("run", window._active_generation, "A", first_a, [{"id": "old", "payload": {}}])
        window._messages_unavailable("run", window._active_generation, "A", first_a)
        assert "new" in window.messages_list.item(0).text()
        assert "unavailable" not in window.messages_status.text()
        window._show_threads("run", window._active_generation, [{"id": "A"}, {"id": "B"}])
        assert window._thread_generation == current
        window._show_threads("run", window._active_generation, [{"id": "B"}])
        assert window._active_thread == "" and window.messages_list.count() == 0
        window._show_messages("run", window._active_generation, "A", current, [{"id": "late", "payload": {}}])
        assert window.messages_list.count() == 0
    finally:
        window.close()
        assert not poller.isRunning()
