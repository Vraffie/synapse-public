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

from synapse.cli.__main__ import DaemonHTTPError, _follow_fetch
from synapse.cli.follow import FollowError, follow_events, read_cursor, write_cursor

ROOT = Path(__file__).resolve().parents[1]


def events(*sequences, salt=""):
    return {"events": [{"sequence": s, "id": f"evt-{salt}{s}", "type": "t"} for s in sequences]}


class Feed:
    """A fake daemon: serves events by sequence, can raise on chosen calls."""
    def __init__(self, total, fail=(), salt=""):
        self.total, self.calls, self.fail, self.salt = total, [], list(fail), salt

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        if self.fail:
            outcome = self.fail.pop(0)
            if outcome is not None:
                raise outcome
        after = int(endpoint.split("after=")[1].split("&")[0])
        limit = int(endpoint.split("limit=")[1])
        return events(*range(after + 1, min(self.total, after + limit) + 1), salt=self.salt)


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
    assert read_cursor(tmp_path / "cursor") == (5, "evt-5") and (tmp_path / "cursor").stat().st_mode & 0o777 == 0o600


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


@pytest.mark.parametrize("page", [{"events": [{"sequence": 5, "id": "a"}, {"sequence": 5, "id": "b"}]}, {"events": [{"sequence": 0, "id": "a"}]},
                                   {"events": [{"sequence": "7", "id": "a"}]}, {"events": [{"sequence": 7}]}, {"events": ["x"]}, {"events": 3}, [], {"nope": 1}])
def test_malformed_or_out_of_order_pages_stop_without_moving_the_cursor(tmp_path, page):
    code, seen, err = run(lambda endpoint: page, tmp_path)
    assert code == 1 and seen == [] and read_cursor(tmp_path / "cursor") is None


def test_unsafe_or_corrupt_cursor_files_are_refused_not_ignored(tmp_path):
    path = tmp_path / "cursor"
    for content in ("abc", "-5", "9" * 40, "1 2 3", "1 bad/id", "x evt-1"):
        path.write_text(content)
        os.chmod(path, 0o600)
        with pytest.raises(FollowError):
            read_cursor(path)
        assert run(Feed(3), tmp_path)[0] == 1
    path.write_text("2 evt-2")
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
    write_cursor(tmp_path / "cursor", 7, "e7")
    write_cursor(tmp_path / "cursor", 9, "e9")
    assert read_cursor(tmp_path / "cursor") == (9, "e9") and [p.name for p in tmp_path.iterdir()] == ["cursor"]


def seed(tmp_path, content):
    path = tmp_path / "cursor"
    path.write_text(content)
    os.chmod(path, 0o600)
    return path


def test_cursor_ahead_of_a_shorter_log_is_refused_not_idled(tmp_path):  # finding 1
    seed(tmp_path, "500 evt-500\n")
    code, seen, err = run(Feed(20), tmp_path)
    assert code == 1 and seen == [] and "ahead of the event log" in err
    assert read_cursor(tmp_path / "cursor") == (500, "evt-500")


def test_cursor_whose_event_id_differs_is_refused(tmp_path):  # finding 1: same length, different database
    run(Feed(5, salt="old-"), tmp_path)
    code, seen, err = run(Feed(9, salt="new-"), tmp_path)
    assert code == 1 and seen == [] and "not the one" in err
    assert read_cursor(tmp_path / "cursor") == (5, "evt-old-5")


def test_resume_checks_the_event_at_the_cursor(tmp_path):
    run(Feed(3), tmp_path)
    feed = Feed(6)
    run(feed, tmp_path)
    assert feed.calls[0] == "/api/v1/events?after=2&limit=1"


def test_legacy_bare_integer_cursor_is_accepted_once_and_rewritten(tmp_path):  # finding 1 (compat)
    path = seed(tmp_path, "3\n")
    code, seen, err = run(Feed(5), tmp_path)
    assert code == 0 and seen == [4, 5] and "legacy format" in err
    assert read_cursor(path) == (5, "evt-5")
    assert "legacy" not in run(Feed(5), tmp_path)[2]


def test_legacy_cursor_ahead_of_the_log_is_refused(tmp_path):
    seed(tmp_path, "500\n")
    code, seen, err = run(Feed(20), tmp_path)
    assert code == 1 and seen == [] and "ahead of the event log" in err


def test_missing_state_dir_or_token_stops_at_once_through_the_real_request_path(tmp_path):  # finding 2
    sleeps = []
    out, err = io.StringIO(), io.StringIO()
    code = follow_events(_follow_fetch(tmp_path / "no-such-dir"), cursor_file=tmp_path / "cursor", after=None, limit=2,
                         interval=0.1, stop_when_idle=True, out=out, err=err, sleep=sleeps.append)
    assert code == 1 and sleeps == [] and "state directory" in err.getvalue() and "retrying" not in err.getvalue()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)  # exists, but no operator.token
    err = io.StringIO()
    code = follow_events(_follow_fetch(state), cursor_file=tmp_path / "cursor", after=None, limit=2, interval=0.1,
                         stop_when_idle=True, out=out, err=err, sleep=sleeps.append)
    assert code == 1 and sleeps == [] and "retrying" not in err.getvalue()


def test_cli_with_a_bad_state_dir_exits_1_immediately(tmp_path):
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-m", "synapse.cli", "--state-dir", str(tmp_path / "nope"), "events", "--follow"],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1 and time.monotonic() - started < 10 and "retrying" not in result.stderr


def test_a_missing_socket_with_a_valid_token_is_still_transient(tmp_path):
    import base64
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    token = state / "operator.token"
    token.write_text(base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="))
    os.chmod(token, 0o600)
    sleeps, err = [], io.StringIO()
    code = follow_events(_follow_fetch(state), cursor_file=tmp_path / "cursor", after=None, limit=2, interval=0.1,
                         stop_when_idle=True, out=io.StringIO(), err=err, sleep=sleeps.append, max_errors=2)
    assert code == 1 and sleeps == [0.5, 1.0] and "giving up" in err.getvalue()


def test_after_below_the_saved_cursor_needs_an_explicit_cursor_file(tmp_path):  # finding 3
    run(Feed(6), tmp_path)
    out, err = io.StringIO(), io.StringIO()
    code = follow_events(Feed(6), cursor_file=tmp_path / "cursor", cursor_file_explicit=False, after=0, limit=2,
                         interval=0.1, stop_when_idle=True, out=out, err=err, sleep=lambda s: None)
    assert code == 2 and out.getvalue() == "" and "--cursor-file" in err.getvalue()
    assert read_cursor(tmp_path / "cursor") == (6, "evt-6")


def test_after_at_or_above_the_saved_cursor_is_fine_without_a_cursor_file(tmp_path):
    run(Feed(3), tmp_path)
    code = follow_events(Feed(6), cursor_file=tmp_path / "cursor", cursor_file_explicit=False, after=3, limit=2,
                         interval=0.1, stop_when_idle=True, out=io.StringIO(), err=io.StringIO(), sleep=lambda s: None)
    assert code == 0 and read_cursor(tmp_path / "cursor") == (6, "evt-6")


def test_cli_after_zero_without_cursor_file_is_refused_and_keeps_the_cursor(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    write_cursor(state / "events.cursor", 100, "evt-100")
    result = subprocess.run([sys.executable, "-m", "synapse.cli", "--state-dir", str(state), "events", "--follow", "--after", "0"],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2 and "--cursor-file" in result.stderr
    assert read_cursor(state / "events.cursor") == (100, "evt-100")


def test_a_cursor_that_cannot_be_saved_is_one_line_and_nonzero_not_a_traceback(tmp_path):  # finding 4
    err = io.StringIO()
    code = follow_events(Feed(3), cursor_file=tmp_path / "missing-dir" / "cursor", after=None, limit=10, interval=0.1,
                         stop_when_idle=True, out=io.StringIO(), err=err, sleep=lambda s: None)
    assert code == 1 and "cannot save the cursor" in err.getvalue() and "Traceback" not in err.getvalue()
    assert len(err.getvalue().strip().splitlines()) == 1


def test_a_read_only_cursor_directory_is_reported_not_raised(tmp_path):
    directory = tmp_path / "ro"
    directory.mkdir()
    os.chmod(directory, 0o500)
    try:
        if os.access(directory, os.W_OK):
            pytest.skip("directory permissions are not enforced (running as root)")
        err = io.StringIO()
        code = follow_events(Feed(3), cursor_file=directory / "cursor", after=None, limit=10, interval=0.1,
                             stop_when_idle=True, out=io.StringIO(), err=err, sleep=lambda s: None)
        assert code == 1 and "cannot save the cursor" in err.getvalue()
    finally:
        os.chmod(directory, 0o700)


@pytest.mark.parametrize("interval", [0, 0.01, 61, float("nan")])
def test_interval_is_checked_in_follow_events(tmp_path, interval):
    err = io.StringIO()
    code = follow_events(Feed(3), cursor_file=tmp_path / "cursor", after=None, limit=2, interval=interval,
                         stop_when_idle=True, out=io.StringIO(), err=err, sleep=lambda s: None)
    assert code == 2 and "--interval" in err.getvalue()


SCRIPT = """
import sys
from pathlib import Path
from synapse.cli.follow import follow_events
def fetch(endpoint):
    after = int(endpoint.split("after=")[1].split("&")[0])
    n = {n}
    return {{"events": [{{"sequence": s, "id": f"e{{s}}"}} for s in range(after + 1, min(n, after + 50) + 1)]}}
sys.exit(follow_events(fetch, cursor_file=Path(sys.argv[1]), after=None, limit=50, interval=0.1, stop_when_idle=False))
"""


def test_a_closed_pipe_is_quiet(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", SCRIPT.format(n=5000), str(tmp_path / "cursor")],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc.stdout.close()
    err = proc.stderr.read()
    assert proc.wait(timeout=15) == 1 and err == ""


def test_sigterm_exits_0_like_ctrl_c_with_the_cursor_saved(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", SCRIPT.format(n=3), str(tmp_path / "cursor")],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert len(proc.stdout.readline()) > 0
    deadline = time.monotonic() + 10
    while read_cursor(tmp_path / "cursor") != (3, "e3") and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.3)  # idle in the poll loop
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) == 0 and "Traceback" not in proc.stderr.read()
    assert read_cursor(tmp_path / "cursor") == (3, "e3")


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
        assert read_cursor(tmp_path / "events.cursor").sequence == before[-1]
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
        assert read_cursor(tmp_path / "events.cursor").sequence == new[-1]
        # replaying from the start still works and sees both generations
        everything = seqs(cli("events", "--follow", "--stop-when-idle", "--after", "0", "--cursor-file", str(tmp_path / "other")))
        assert everything == before + new
    finally:
        stop(proc)
