import os
import stat
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from synapse.desktop import cursor_store
from synapse.desktop.__main__ import MainWindow, Poller
from synapse.desktop.cursor_store import Cursor, CursorError, cursor_path, read_cursor, write_cursor

LOG = [{"sequence": n, "id": f"evt-{n}", "type": "task.started", "aggregate_id": "a"} for n in range(1, 6)]


def make_fetch(log, calls):
    def fetch(_dir, endpoint):
        calls.append(endpoint)
        if "health" in endpoint:
            return {"status": "ok"}
        after = int(endpoint.split("after=")[1].split("&")[0])
        limit = int(endpoint.split("limit=")[1].split("&")[0])
        return {"events": [e for e in log if e["sequence"] > after][:limit]}
    return fetch


def run_poller(tmp_path, log, until, **kw):
    app = QApplication.instance() or QApplication([])
    calls, delivered, notices = [], [], []
    poller = Poller(Path(tmp_path), fetch=make_fetch(log, calls), interval=0.01)
    poller.event.connect(lambda e: delivered.append(e["sequence"]))
    poller.cursor_notice.connect(notices.append)
    poller.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not until(poller, delivered):
        app.processEvents()
        time.sleep(0.01)
    poller.stop()
    poller.wait(5000)
    app.processEvents()
    return poller, delivered, notices, calls


def test_restart_resumes_without_replay(tmp_path):
    _, first, _, _ = run_poller(tmp_path, LOG, lambda p, d: len(d) >= 5)
    assert first == [1, 2, 3, 4, 5]
    path = cursor_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == "5 evt-5\n"
    log = LOG + [{"sequence": 6, "id": "evt-6", "type": "x"}]
    _, second, notices, _ = run_poller(tmp_path, log, lambda p, d: len(d) >= 1)
    assert second == [6]
    assert any("Resumed" in n for n in notices)


def test_corrupt_file_replays_and_is_visible(tmp_path):
    path = cursor_path(tmp_path)
    path.write_text("not a cursor\n")
    os.chmod(path, 0o600)
    _, delivered, notices, _ = run_poller(tmp_path, LOG, lambda p, d: len(d) >= 5)
    assert delivered == [1, 2, 3, 4, 5]
    assert any("corrupt" in n for n in notices)
    assert path.read_text() == "5 evt-5\n"  # repaired by the next save


def test_group_readable_file_is_ignored(tmp_path):
    path = cursor_path(tmp_path)
    path.write_text("3 evt-3\n")
    os.chmod(path, 0o644)
    _, delivered, notices, _ = run_poller(tmp_path, LOG, lambda p, d: len(d) >= 5)
    assert delivered == [1, 2, 3, 4, 5]
    assert any("private" in n for n in notices)


def test_cursor_ahead_of_log_replays(tmp_path):
    write_cursor(cursor_path(tmp_path), Cursor(99, "evt-99"))
    _, delivered, notices, _ = run_poller(tmp_path, LOG, lambda p, d: len(d) >= 5)
    assert delivered == [1, 2, 3, 4, 5]
    assert any("ahead" in n for n in notices)


def test_mismatched_daemon_replays(tmp_path):
    write_cursor(cursor_path(tmp_path), Cursor(3, "other-daemon-evt-3"))
    _, delivered, notices, _ = run_poller(tmp_path, LOG, lambda p, d: len(d) >= 5)
    assert delivered == [1, 2, 3, 4, 5]
    assert any("different event log" in n for n in notices)


def test_write_failure_is_visible_and_polling_continues(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr("synapse.desktop.connection.write_cursor", boom)
    _, delivered, notices, _ = run_poller(tmp_path, LOG, lambda p, d: len(d) >= 5)
    assert delivered == [1, 2, 3, 4, 5]
    assert sum("not being saved" in n for n in notices) == 1
    assert "disk full" not in " ".join(notices)
    assert not cursor_path(tmp_path).exists()


def test_events_without_id_are_not_persisted(tmp_path):
    log = [{"sequence": 1, "type": "x"}]
    _, delivered, notices, _ = run_poller(tmp_path, log, lambda p, d: len(d) >= 1)
    assert delivered == [1]
    assert not cursor_path(tmp_path).exists()
    assert any("not being saved" in n for n in notices)


def test_verification_fetch_failure_retries_without_skipping(tmp_path):
    write_cursor(cursor_path(tmp_path), Cursor(3, "evt-3"))
    calls = []
    base = make_fetch(LOG, calls)
    state = {"failed": False}
    def fetch(d, endpoint):
        if "after=2" in endpoint and not state["failed"]:
            state["failed"] = True
            raise OSError("down")
        return base(d, endpoint)
    app = QApplication.instance() or QApplication([])
    poller = Poller(Path(tmp_path), fetch=fetch, interval=0.01)
    got = []
    poller.event.connect(lambda e: got.append(e["sequence"]))
    poller.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and len(got) < 2:
        app.processEvents()
        time.sleep(0.01)
    poller.stop()
    poller.wait(5000)
    assert got == [4, 5]


def test_window_shows_notice(tmp_path):
    app = QApplication.instance() or QApplication([])
    write_cursor(cursor_path(tmp_path), Cursor(99, "x"))
    calls = []
    window = MainWindow(tmp_path, poller=Poller(Path(tmp_path), fetch=make_fetch(LOG, calls), interval=0.01))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not window.cursor_notice.text():
        app.processEvents()
        time.sleep(0.01)
    assert "ahead" in window.cursor_notice.text()
    window.close()


@pytest.mark.parametrize("content", [b"", b"0 evt\n", b"-1 evt\n", b"1\n", b"1 a b\n", b"1 bad/id\n", b"\xff\xfe\n", b"9" * 300, b"1 " + b"a" * 200])
def test_read_rejects_corrupt(tmp_path, content):
    path = tmp_path / "c"
    path.write_bytes(content)
    os.chmod(path, 0o600)
    with pytest.raises(CursorError):
        read_cursor(path)


def test_read_rejects_symlink_and_missing_ok(tmp_path):
    assert read_cursor(tmp_path / "none") is None
    target = tmp_path / "t"
    write_cursor(target, Cursor(1, "e"))
    link = tmp_path / "l"
    link.symlink_to(target)
    with pytest.raises(CursorError):
        read_cursor(link)


def test_write_leaves_no_temp_on_failure(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("x")
    monkeypatch.setattr(cursor_store.os, "replace", boom)
    with pytest.raises(OSError):
        write_cursor(tmp_path / "c", Cursor(1, "e"))
    assert list(tmp_path.iterdir()) == []
