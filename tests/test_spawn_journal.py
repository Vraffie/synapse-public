"""SpawnJournal: a restart proves old agent processes dead or kills them; it never signals an ambiguous pid."""
import os
import signal
import subprocess
import sys
import time

import pytest

from synapse.infrastructure import spawn_journal as sj
from synapse.infrastructure.mock_process_identity import ProcessIdentity, _boot_id, _stat, capture_mock_child
from synapse.infrastructure.spawn_journal import FenceReport, SpawnJournal


def spawn_leader(code="import time; time.sleep(60)"):
    return subprocess.Popen([sys.executable, "-c", code], start_new_session=True, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


def gone(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _stat(pid)[0] in ("Z", "X"):
                return True
        except (FileNotFoundError, ValueError):
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def journal(tmp_path):
    return SpawnJournal(tmp_path / "spawn")


@pytest.fixture
def reap():
    procs = []
    yield procs.append
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.wait(timeout=5)


def test_entries_are_private_and_hold_only_ids(journal, tmp_path, reap):
    p = spawn_leader(); reap(p)
    journal.begin("k1")
    journal.record("k1", capture_mock_child(p))
    f = tmp_path / "spawn" / "k1.json"
    assert oct(f.stat().st_mode & 0o777) == "0o600" and oct((tmp_path / "spawn").stat().st_mode & 0o777) == "0o700"
    assert set(__import__("json").loads(f.read_text())) == {"v", "state", "pid", "start_ticks", "boot_id"}
    assert journal.pending() == ["k1"]
    journal.finish("k1")
    assert journal.pending() == []
    journal.finish("k1")  # idempotent


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a" * 129, "sp ace", None, 5])
def test_keys_are_validated(journal, bad):
    with pytest.raises(ValueError):
        journal.begin(bad)


def test_a_surviving_agent_is_killed_with_its_whole_group_and_the_entry_removed(journal, reap, tmp_path):
    # leader plus a grandchild in the same group, like a sandbox or a shell tool
    p = spawn_leader("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)")
    reap(p)
    time.sleep(0.4)
    journal.begin("k"); journal.record("k", capture_mock_child(p))
    members = sj._group_members(p.pid)
    assert len(members) >= 2
    report = SpawnJournal(tmp_path / "spawn").fence_all()  # a "restarted daemon": a fresh object on the same dir
    assert report == FenceReport(0, 1, 0) and report.all_clear
    assert all(gone(pid) for pid in members) and journal.pending() == []
    p.wait(timeout=5)


def test_a_reused_pid_is_not_signalled(journal, reap):
    p = spawn_leader(); reap(p)
    ident = capture_mock_child(p)
    journal.record("k", ProcessIdentity(ident.pid, ident.start_ticks + 1, ident.boot_id, ident.supervisor_pid,
                                        ident.supervisor_start_ticks))  # recorded ticks differ: pid now "belongs" to someone else
    report = journal.fence_all()
    assert report.killed == 0 and report.unverified == 1 and not report.all_clear  # live group under that id: ambiguous
    assert not gone(p.pid, timeout=0.3)  # the bystander is untouched
    assert journal.pending() == [] and (journal._dir / "k.unverified").exists()


def test_a_reused_pid_with_no_live_group_is_provably_dead(journal):
    dead_pid = max(int(n) for n in os.listdir("/proc") if n.isdigit()) + 50000
    journal.record("k", ProcessIdentity(dead_pid, 12345, _boot_id(), os.getpid(), 1))
    assert journal.fence_all() == FenceReport(1, 0, 0)


def test_a_different_boot_means_everything_is_dead_without_any_signal(journal, reap):
    p = spawn_leader(); reap(p)
    ident = capture_mock_child(p)
    journal.record("k", ProcessIdentity(ident.pid, ident.start_ticks, "some-other-boot", ident.supervisor_pid,
                                        ident.supervisor_start_ticks))
    assert journal.fence_all() == FenceReport(1, 0, 0)
    assert not gone(p.pid, timeout=0.3)  # not touched: a stale boot id proves nothing about THIS pid


def test_a_spawn_that_crashed_before_its_pid_was_recorded_is_unverified_never_assumed_dead(journal):
    journal.begin("k")
    report = journal.fence_all()
    assert report == FenceReport(0, 0, 1) and not report.all_clear
    assert (journal._dir / "k.unverified").exists()


def test_zombie_leader_alone_is_dead(journal):
    p = spawn_leader("pass")
    ident = capture_mock_child(p)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _stat(p.pid)[0] != "Z":
        time.sleep(0.02)
    journal.record("k", ident)
    assert journal.fence_all() == FenceReport(1, 0, 0)
    p.wait(timeout=5)


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"v":2}', '{"v":1,"state":"running"}',
                                     '{"v":1,"state":"running","pid":"1","start_ticks":1,"boot_id":"b"}',
                                     '{"v":1,"state":"running","pid":1,"start_ticks":1,"boot_id":"b"}',
                                     "x" * 5000])
def test_malformed_entries_are_unverified_and_never_signal(journal, content):
    (journal._dir / "bad.json").write_text(content)
    report = journal.fence_all()
    assert report == FenceReport(0, 0, 1)


def test_interrupted_temp_files_are_cleaned_up(journal):
    (journal._dir / "k.json.tmp").write_text("{")
    assert journal.fence_all() == FenceReport(0, 0, 0) and not list(journal._dir.glob("*.tmp"))
