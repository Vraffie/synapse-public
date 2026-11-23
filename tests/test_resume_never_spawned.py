"""ADR 024: after a crash, only attempts PROVABLY never handed to a provider are re-queued; all else is as before.

Every crash point is reproduced with the fake CLI (no real claude) by copying the database (what a SIGKILLed daemon
leaves behind) and the spawn journal directory at that exact moment, then "restarting" a new runtime on the copies.
"""
import asyncio
import json
import shutil
import sqlite3

import pytest

from synapse.daemon.agent_runtime import AgentRuntime
from synapse.daemon.agent_service import AgentRunService as AgentService
from synapse.infrastructure.spawn_journal import SpawnJournal
from synapse.orchestration import agent_engine
from synapse.orchestration.agent_engine import AGENT_MAX_ATTEMPTS, AgentProviderRouter, AgentWorkflowEngine
from synapse.persistence import attempt_resume
from synapse.persistence.agent_abandon import ASSERTION, abandon_agent_run
from synapse.persistence.agent_cancellation import request_agent_cancellation
from synapse.persistence.migrations import open_database
from test_agent_runtime import Env, env  # noqa: F401  (env is a fixture)
from test_spawn_journal_runtime import journalled


def snapshot(db, path):
    dst = sqlite3.connect(path)
    db.backup(dst)
    dst.close()


def copy_journal(src, dst):
    shutil.copytree(src, dst)


def restart(env, image, journal_dir, *, journal=True):
    db = open_database(image)
    rt = AgentRuntime(db, env.blobs, profile=env.profile,
                      spawn_journal=SpawnJournal(journal_dir) if journal else None,
                      worktree_check=lambda run_id, task_id, path: ("a" * 40, True))
    rt._park_poll, rt._cancel_timeout = 0.01, 10
    rt.recover_interrupted()
    return db, rt


def resumed_events(db, run_id):
    return [json.loads(p) for (p,) in db.execute(
        "SELECT payload_json FROM events WHERE run_id=? AND type='agent.attempt_resumed' ORDER BY rowid", (run_id,))]


def attempts(db):
    return [r[0] for r in db.execute("SELECT state FROM task_attempts ORDER BY attempt_no")]


def run_state(db, run_id):
    return db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()[0]


def assert_as_today(db, rt, run_id):
    """The pre-ADR-024 outcome: unknown attempt, blocked task, run recovering, nothing re-driven or re-queued."""
    assert attempts(db)[-1] == "unknown"
    assert db.execute("SELECT state FROM tasks").fetchall() == [("blocked",)]
    assert run_state(db, run_id) == "recovering"
    assert rt._pending_redrive is None and not rt.resume_parked()
    assert resumed_events(db, run_id) == []


async def drive(rt):
    assert rt.resume_parked()
    await asyncio.wait_for(rt._active, 30)


@pytest.fixture
def crash_points(env, tmp_path, monkeypatch):
    """Run one task to success while copying db and journal at every crash point of the spawn path."""
    journal_dir = env.state / "spawn-journal"
    images = {}

    def capture(name):
        snapshot(env.db, tmp_path / (name + ".db"))
        copy_journal(journal_dir, tmp_path / (name + ".journal"))
        images[name] = (tmp_path / (name + ".db"), tmp_path / (name + ".journal"))

    real_selected = AgentWorkflowEngine._record_profile_selected

    def selected(self, *a, **k):
        real_selected(self, *a, **k)
        capture("after_reserve_before_mark")  # attempt reserved and running in the DB; mark not yet written
    monkeypatch.setattr(AgentWorkflowEngine, "_record_profile_selected", selected)

    real_start = AgentProviderRouter.start

    async def start(self, request):
        capture("after_mark_before_begin")  # mark committed; provider not yet asked, no journal entry
        return await real_start(self, request)
    monkeypatch.setattr(AgentProviderRouter, "start", start)

    real_begin, real_record = SpawnJournal.begin, SpawnJournal.record

    def begin(self, key):
        real_begin(self, key)
        copy_journal(journal_dir, tmp_path / "after_begin_before_record.journal")  # worker thread: files only

    def record(self, key, identity):
        real_record(self, key, identity)
        copy_journal(journal_dir, tmp_path / "after_record.journal")
    monkeypatch.setattr(SpawnJournal, "begin", begin)
    monkeypatch.setattr(SpawnJournal, "record", record)

    real_manifest = AgentWorkflowEngine._record_manifest

    def manifest(self, *a, **k):
        capture("after_finish_before_db_write")  # provider returned and reaped; terminal state not yet written
        return real_manifest(self, *a, **k)
    monkeypatch.setattr(AgentWorkflowEngine, "_record_manifest", manifest)

    async def go():
        env.runtime = journalled(env, SpawnJournal(journal_dir))
        env.runtime.start(env.run_id, "s", env.specs(prompt="ok"))
        await asyncio.wait_for(env.runtime._active, 30)
    asyncio.run(go())
    assert env.states() == ("succeeded", {"work": "completed"}) and env.spawns() == ["ok"]
    # The DB does not change between the mark commit and the terminal write, so begin/record share that image.
    for name in ("after_begin_before_record", "after_record"):
        images[name] = (images["after_mark_before_begin"][0], tmp_path / (name + ".journal"))
    monkeypatch.undo()
    return images


def test_crash_after_reserve_before_spawn_is_requeued_with_a_new_attempt(env, crash_points):
    image, journal = crash_points["after_reserve_before_mark"]
    db, rt = restart(env, image, journal)
    try:
        old = db.execute("SELECT id FROM task_attempts").fetchone()[0]
        assert db.execute("SELECT count(*) FROM attempt_spawn_marks").fetchone() == (0,)
        assert attempts(db) == ["cancelled"] and run_state(db, env.run_id) == "running"
        assert db.execute("SELECT state FROM tasks").fetchall() == [("ready",)]
        assert resumed_events(db, env.run_id) == [{"run_id": env.run_id, "task_id": db.execute("SELECT id FROM tasks").fetchone()[0],
                                                   "attempt_id": old, "principal": "agent-recovery"}]
        asyncio.run(drive(rt))
        assert attempts(db) == ["cancelled", "completed"] and run_state(db, env.run_id) == "succeeded"
        new = db.execute("SELECT id FROM task_attempts WHERE attempt_no=2").fetchone()[0]
        assert new != old  # never reuses the attempt id
        assert env.spawns() == ["ok", "ok"]  # exactly one new spawn
        shown = asyncio.run(AgentService(db, rt, None).show(env.run_id))
        assert shown["attempts_resumed"] == 1
    finally:
        db.close()


@pytest.mark.parametrize("point", ["after_mark_before_begin", "after_begin_before_record", "after_record",
                                   "after_finish_before_db_write"])
def test_crash_once_a_spawn_may_have_begun_recovers_exactly_as_before(env, crash_points, point):
    image, journal = crash_points[point]
    db, rt = restart(env, image, journal)
    try:
        assert db.execute("SELECT count(*) FROM attempt_spawn_marks").fetchone() == (1,)
        assert_as_today(db, rt, env.run_id)
        if point == "after_begin_before_record":
            assert rt.fence_report.unverified == 1  # begin-only entry stays a doubt for the operator
        assert asyncio.run(AgentService(db, rt, None).show(env.run_id))["attempts_resumed"] == 0
        # A second restart is idempotent and still never re-queues or re-spawns.
        rt2 = AgentRuntime(db, env.blobs, profile=env.profile, spawn_journal=SpawnJournal(journal))
        rt2.recover_interrupted()
        assert_as_today(db, rt2, env.run_id)
        assert env.spawns() == ["ok"]
    finally:
        db.close()


def test_crash_mid_run_with_a_live_agent_recovers_exactly_as_before(env, tmp_path):
    journal_dir = env.state / "spawn-journal"
    image = tmp_path / "mid.db"

    async def go():
        env.runtime = journalled(env, SpawnJournal(journal_dir))
        env.runtime.start(env.run_id, "s", env.specs(prompt="hang"))
        await env.until(lambda: (env.worktrees["work"] / "pid").exists() and env.attempts() == ["running"])
        snapshot(env.db, image)
        db, rt = restart(env, image, journal_dir)  # the fence kills the live process group
        try:
            assert rt.fence_report.killed == 1
            assert_as_today(db, rt, env.run_id)
        finally:
            db.close()
        env.runtime._active.cancel()
        await asyncio.gather(env.runtime._active, return_exceptions=True)
    asyncio.run(go())
    assert env.spawns() == ["hang"]


def test_crash_before_reserve_redrives_the_run_without_any_resume_event(env, tmp_path):
    image = tmp_path / "pre.db"

    async def go():
        env.runtime.start(env.run_id, "s", env.specs(prompt="ok"))
        snapshot(env.db, image)  # run committed running; the engine has not claimed anything yet
        env.runtime._active.cancel()
        await asyncio.gather(env.runtime._active, return_exceptions=True)
    asyncio.run(go())
    db, rt = restart(env, image, tmp_path / "journal")
    try:
        assert attempts(db) == [] and rt._pending_redrive == env.run_id and resumed_events(db, env.run_id) == []
        asyncio.run(drive(rt))
        assert run_state(db, env.run_id) == "succeeded" and attempts(db) == ["completed"] and env.spawns() == ["ok"]
    finally:
        db.close()


def test_repeated_restarts_are_idempotent_and_the_retry_budget_bounds_resumes(env, crash_points, tmp_path, monkeypatch):
    image, journal = crash_points["after_reserve_before_mark"]
    db, _ = restart(env, image, journal)
    db.close()
    db, rt = restart(env, image, journal)  # a second crash right after the re-queue committed, before relaunch
    try:
        assert len(resumed_events(db, env.run_id)) == 1 and attempts(db) == ["cancelled"]
        assert rt._pending_redrive == env.run_id
        second = tmp_path / "second.db"

        class Crash(Exception):
            pass

        real_selected = AgentWorkflowEngine._record_profile_selected

        def selected(self, *a, **k):
            real_selected(self, *a, **k)
            snapshot(db, second)  # the re-queued attempt crashes before ITS spawn too

        def mark(conn, *, attempt_id):
            raise Crash()  # the crashed daemon never got further
        monkeypatch.setattr(AgentWorkflowEngine, "_record_profile_selected", selected)
        monkeypatch.setattr(agent_engine, "mark_spawn_begun", mark)
        asyncio.run(drive(rt))
        monkeypatch.undo()
    finally:
        db.close()
    db, rt = restart(env, second, journal)
    try:
        # attempt 2 of 2 is unmarked, but a third would exceed the engine's budget: today's recovery.
        assert AGENT_MAX_ATTEMPTS == 2
        assert attempts(db) == ["cancelled", "unknown"] and run_state(db, env.run_id) == "recovering"
        assert len(resumed_events(db, env.run_id)) == 1 and not rt.resume_parked()
        assert env.spawns() == ["ok"]
    finally:
        db.close()


def test_cancellation_request_is_never_resumed(env, crash_points):
    image, journal = crash_points["after_reserve_before_mark"]
    pre = open_database(image)
    request_agent_cancellation(pre, run_id=env.run_id, key="c")
    pre.close()
    db, rt = restart(env, image, journal)
    try:
        assert resumed_events(db, env.run_id) == [] and rt._pending_redrive is None and not rt.resume_parked()
        assert run_state(db, env.run_id) != "running" and "cancelled" not in attempts(db)
        assert env.spawns() == ["ok"]
    finally:
        db.close()


def test_unverified_fence_or_missing_journal_keeps_todays_recovery(env, crash_points, tmp_path):
    image, journal = crash_points["after_reserve_before_mark"]
    stray = tmp_path / "stray"
    shutil.copytree(journal, stray)
    SpawnJournal(stray).begin("some-other-spawn-whose-pid-was-never-recorded")
    copy = tmp_path / "copy.db"
    shutil.copy(image, copy)
    db, rt = restart(env, image, stray)
    try:
        assert not rt.fence_report.all_clear
        assert_as_today(db, rt, env.run_id)
    finally:
        db.close()
    db, rt = restart(env, copy, journal, journal=False)
    try:
        assert_as_today(db, rt, env.run_id)
    finally:
        db.close()


def test_abandoned_run_is_never_resumed(env, crash_points):
    image, journal = crash_points["after_reserve_before_mark"]
    db, rt = restart(env, image, journal, journal=False)  # no journal: recovering, as today
    try:
        version = db.execute("SELECT state_version FROM runs WHERE id=?", (env.run_id,)).fetchone()[0]
        abandon_agent_run(db, run_id=env.run_id, expected_run_version=version, assertion=ASSERTION, key="a")
    finally:
        db.close()
    db, rt = restart(env, image, journal)
    try:
        assert resumed_events(db, env.run_id) == [] and not rt.resume_parked()
        assert run_state(db, env.run_id) not in ("running",)
    finally:
        db.close()


def test_per_run_cap_and_guards_in_the_store(env, crash_points):
    image, _ = crash_points["after_reserve_before_mark"]
    db = open_database(image)
    try:
        assert attempt_resume.requeue_never_spawned(db, run_id=env.run_id, principal="agent-recovery",
                                                    max_attempts=AGENT_MAX_ATTEMPTS, cap=0) is None
        assert attempt_resume.requeue_never_spawned(db, run_id=env.run_id, principal="agent-recovery",
                                                    max_attempts=1) is None  # budget already spent
        assert attempts(db) == ["running"] and resumed_events(db, env.run_id) == []  # nothing changed
        attempt = db.execute("SELECT id FROM task_attempts").fetchone()[0]
        db.execute("INSERT INTO attempt_spawn_marks(attempt_id,marked_at) VALUES(?, 'now')", (attempt,))
        for sql in ("UPDATE attempt_spawn_marks SET marked_at='x'", "DELETE FROM attempt_spawn_marks",
                    "INSERT OR REPLACE INTO attempt_spawn_marks(attempt_id,marked_at) VALUES('%s','y')" % attempt):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)
        # Marked now: the store refuses to re-queue it whatever the budget.
        assert attempt_resume.requeue_never_spawned(db, run_id=env.run_id, principal="agent-recovery",
                                                    max_attempts=99, cap=99) is None
    finally:
        db.close()


def test_migration_0017_marks_every_existing_attempt_so_an_upgrade_never_requeues_old_work(env, crash_points, tmp_path):
    from pathlib import Path
    from synapse.persistence.migrations import apply_migrations
    root = Path(__file__).resolve().parents[1] / "migrations"
    image, journal = crash_points["after_reserve_before_mark"]  # an in-flight attempt with no mark
    old = tmp_path / "v16.db"
    db = open_database(old)
    try:
        assert apply_migrations(db, root, limit=16)[-1] == 16
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("ATTACH DATABASE ? AS src", (str(image),))
        tables = [r[0] for r in db.execute("SELECT name FROM main.sqlite_master WHERE type='table' "
                                           "AND name NOT IN ('schema_migrations','sqlite_sequence')")]
        db.execute("BEGIN")
        for table in tables:
            cols = ",".join(r[1] for r in db.execute("PRAGMA main.table_info(%s)" % table))
            db.execute("INSERT INTO main.%s(%s) SELECT %s FROM src.%s" % (table, cols, cols, table))
        db.execute("COMMIT")
        db.execute("DETACH DATABASE src")
        db.execute("PRAGMA foreign_keys = ON")
        assert attempts(db) == ["running"]  # what a pre-0017 daemon left in flight
        assert apply_migrations(db, root, limit=17) == [17]
        assert db.execute("SELECT count(*) FROM attempt_spawn_marks").fetchone() == (1,)  # data kept, attempt marked
    finally:
        db.close()
    db, rt = restart(env, old, journal)
    try:
        assert_as_today(db, rt, env.run_id)  # an attempt from before the upgrade may have run: never re-queued
    finally:
        db.close()


def test_open_database_sets_synchronous_full_so_a_committed_mark_is_durable(tmp_path):
    db = open_database(tmp_path / "d.db")
    try:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert db.execute("PRAGMA synchronous").fetchone() == (2,)  # FULL, set explicitly at open (ADR 024)
    finally:
        db.close()
