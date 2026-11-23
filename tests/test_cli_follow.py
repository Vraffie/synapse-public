"""``agentctl events --follow``: cursor persistence, retry, and fail-closed behaviour."""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from synapse.cli.__main__ import DaemonHTTPError
from synapse.cli.follow import FollowError, follow_events, read_cursor, write_cursor

ROOT = Path(__file__).resolve().parents[1]


def events(*sequences):
    return {"events": [{"sequence": s, "type": "t"} for s in sequences]}


class Feed:
    """A fake daemon: serves events by sequence, can raise on chosen calls."""
    def __init__(self, total, fail=()):
        self.total, self.calls, self.fail = total, [], list(fail)

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        if self.fail:
            outcome = self.fail.pop(0)
            if outcome is not None:
                raise outcome
        after = int(endpoint.split("after=")[1].split("&")[0])
        limit = int(endpoint.split("limit=")[1])
        return events(*range(after + 1, min(self.total, after + limit) + 1))


def run(feed, tmp_path, **kw):
    out, err = io.StringIO(), io.StringIO()
    kw.setdefault("after", None)
    kw.setdefault("limit", 2)
    code = follow_events(feed, cursor_file=tmp_path / "cursor", interval=0.1, stop_when_idle=True, out=out, err=err,
                         sleep=lambda s: None, **kw)
    return code, [json.loads(line)["sequence"] for line in out.getvalue().splitlines()], err.getvalue()


def test_follow_prints_every_event_once_in_pages_and_saves_the_cursor(tmp_path):
    code, seen, _ = run(Feed(5), tmp_path)
    assert code == 0 and seen == [1, 2, 3, 4, 5]
    assert read_cursor(tmp_path / "cursor") == 5 and (tmp_path / "cursor").stat().st_mode & 0o777 == 0o600


def test_a_second_run_resumes_from_the_cursor_and_prints_only_new_events(tmp_path):
    run(Feed(3), tmp_path)
    code, seen, _ = run(Feed(6), tmp_path)
    assert code == 0 and seen == [4, 5, 6]
    assert run(Feed(6), tmp_path)[1] == []


def test_explicit_after_overrides_the_cursor(tmp_path):
    run(Feed(3), tmp_path)
    assert run(Feed(5), tmp_path, after=1)[1] == [2, 3, 4, 5]


def test_cursor_is_not_saved_when_the_reader_goes_away_so_nothing_is_skipped(tmp_path):
    class Broken(io.StringIO):
        def write(self, text):
            raise BrokenPipeError
    code = follow_events(Feed(3), cursor_file=tmp_path / "cursor", after=None, limit=10, interval=0.1,
                         stop_when_idle=True, out=Broken(), err=io.StringIO(), sleep=lambda s: None)
    assert code == 1 and read_cursor(tmp_path / "cursor") is None


def test_a_daemon_that_is_down_is_retried_with_backoff_then_resumes_without_loss_or_duplicates(tmp_path):
    sleeps = []
    feed = Feed(3, fail=[ConnectionRefusedError(), FileNotFoundError(), DaemonHTTPError(503)])
    out, err = io.StringIO(), io.StringIO()
    code = follow_events(feed, cursor_file=tmp_path / "cursor", after=None, limit=10, interval=0.1, stop_when_idle=True,
                         out=out, err=err, sleep=sleeps.append)
    assert code == 0 and [json.loads(l)["sequence"] for l in out.getvalue().splitlines()] == [1, 2, 3]
    assert sleeps == [0.5, 1.0, 2.0]


def test_it_gives_up_after_too_many_consecutive_failures(tmp_path):
    feed = Feed(3, fail=[ConnectionRefusedError()] * 10)
    code, seen, err = run(feed, tmp_path, max_errors=3)
    assert code == 1 and seen == [] and "giving up" in err


@pytest.mark.parametrize("failure", [DaemonHTTPError(401), DaemonHTTPError(404), PermissionError("socket"), ValueError("x")])
def test_non_transient_errors_stop_at_once(tmp_path, failure):
    feed = Feed(3, fail=[failure])
    code, seen, err = run(feed, tmp_path)
    assert code == 1 and seen == [] and len(feed.calls) == 1 and "agentctl:" in err


@pytest.mark.parametrize("page", [{"events": [{"sequence": 5}, {"sequence": 5}]}, {"events": [{"sequence": 0}]},
                                   {"events": [{"sequence": "7"}]}, {"events": ["x"]}, {"events": 3}, [], {"nope": 1}])
def test_malformed_or_out_of_order_pages_stop_without_moving_the_cursor(tmp_path, page):
    code, seen, err = run(lambda endpoint: page, tmp_path)
    assert code == 1 and seen == [] and read_cursor(tmp_path / "cursor") is None


def test_unsafe_or_corrupt_cursor_files_are_refused_not_ignored(tmp_path):
    path = tmp_path / "cursor"
    for content in ("abc", "-5", "9" * 40, "1 2"):
        path.write_text(content)
        os.chmod(path, 0o600)
        with pytest.raises(FollowError):
            read_cursor(path)
        assert run(Feed(3), tmp_path)[0] == 1
    path.write_text("2")
    os.chmod(path, 0o644)
    with pytest.raises(FollowError):
        read_cursor(path)
    path.unlink()
    link = tmp_path / "real"
    link.write_text("1")
    path.symlink_to(link)
    with pytest.raises(FollowError):
        read_cursor(path)


def test_write_cursor_is_atomic_and_leaves_no_temp_files(tmp_path):
    write_cursor(tmp_path / "cursor", 7)
    write_cursor(tmp_path / "cursor", 9)
    assert read_cursor(tmp_path / "cursor") == 9 and [p.name for p in tmp_path.iterdir()] == ["cursor"]


def test_follow_against_a_real_daemon_resumes_across_a_daemon_restart(tmp_path):
    cmd = [sys.executable, "-m", "synapse.daemon", "--state-dir", str(tmp_path), "--migrations", str(ROOT / "migrations")]

    def start():
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while not (tmp_path / "agentd.sock").exists() and time.monotonic() < deadline:
            assert proc.poll() is None, "daemon exited before listening"
            time.sleep(0.02)
        return proc

    def stop(proc):
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)

    def cli(*args):
        return subprocess.run([sys.executable, "-m", "synapse.cli", "--state-dir", str(tmp_path), *args],
                              capture_output=True, text=True, timeout=15)

    workflow = tmp_path / "wf.json"
    workflow.write_text(json.dumps({"workflow_id": "wf", "version": "1", "nodes": [{"id": "task", "dependencies": []}]}))

    def seqs(result):
        assert result.returncode == 0, result.stderr
        return [json.loads(line)["sequence"] for line in result.stdout.splitlines()]

    proc = start()
    try:
        deadline = time.monotonic() + 10
        while cli("health").returncode != 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert cli("publish-workflow", str(workflow), "--idempotency-key", "p", "--correlation-id", "t").returncode == 0
        before = seqs(cli("events", "--follow", "--stop-when-idle"))
        assert before and before == sorted(set(before))
        assert seqs(cli("events", "--follow", "--stop-when-idle")) == []  # caught up: nothing repeated
        assert read_cursor(tmp_path / "events.cursor") == before[-1]
    finally:
        stop(proc)
    proc = start()
    try:
        deadline = time.monotonic() + 10
        while cli("health").returncode != 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert seqs(cli("events", "--follow", "--stop-when-idle")) == []  # restart: still nothing repeated
        assert cli("queue-run", "wf", "1", "--idempotency-key", "r", "--correlation-id", "t").returncode == 0
        new = seqs(cli("events", "--follow", "--stop-when-idle"))
        assert new and new[0] == before[-1] + 1 and new == sorted(set(new))  # exactly the events after the cursor
        assert read_cursor(tmp_path / "events.cursor") == new[-1]
        # replaying from the start still works and sees both generations
        everything = seqs(cli("events", "--follow", "--stop-when-idle", "--after", "0", "--cursor-file", str(tmp_path / "other")))
        assert everything == before + new
    finally:
        stop(proc)
