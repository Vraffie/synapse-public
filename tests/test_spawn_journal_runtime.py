"""ADR 023: the daemon records every real spawn and, at startup, kills or proves dead what a crash left behind."""
import asyncio
import json
import os
import sqlite3
import time

import pytest

from synapse.daemon.agent_runtime import AgentRuntime
from synapse.infrastructure.spawn_journal import FenceReport, SpawnJournal
from synapse.persistence.migrations import open_database
from synapse.providers.claude_cli import ClaudeCliProvider
from test_agent_runtime import Env, env  # noqa: F401  (env is a fixture)
from test_claude_cli_provider import env as provider_env, profile, request, run  # noqa: F401


def alive(pid):
    try:
        raw = open(f"/proc/{pid}/stat").read()
    except FileNotFoundError:
        return False
    return raw[raw.rfind(")") + 2] not in "ZX"


def journalled(env, journal):
    runtime = AgentRuntime(env.db, env.blobs, profile=env.profile, spawn_journal=journal,
                           worktree_check=lambda run_id, task_id, path: ("a" * 40, True))
    runtime._park_poll, runtime._cancel_timeout = 0.01, 10
    return runtime


def fence_events(db, run_id):
    return [json.loads(p) for (p,) in db.execute(
        "SELECT payload_json FROM events WHERE run_id=? AND type='agent.worker_fence'", (run_id,))]


def test_a_crashed_daemons_orphan_agent_is_killed_at_restart_and_the_audit_says_so(env, tmp_path):
    journal = SpawnJournal(env.state / "spawn-journal")
    image = tmp_path / "crash.db"
    seen = {}

    async def go():
        env.runtime = journalled(env, journal)
        env.runtime.start(env.run_id, "s", env.specs(prompt="hang"))
        await env.until(lambda: (env.worktrees["work"] / "pid").exists() and env.attempts() == ["running"])
        seen["pid"] = int((env.worktrees["work"] / "pid").read_text())
        assert alive(seen["pid"]) and len(journal.pending()) == 1  # the spawn is on record while it runs
        dst = sqlite3.connect(image)
        env.db.backup(dst)  # what a SIGKILLed daemon leaves behind; the agent process keeps running
        dst.close()
        # "restart": a new runtime on the crash image and a fresh journal object over the same directory
        db2 = open_database(image)
        try:
            rt2 = AgentRuntime(db2, env.blobs, profile=env.profile, spawn_journal=SpawnJournal(env.state / "spawn-journal"))
            rt2.recover_interrupted()
            seen["report"] = rt2.fence_report
            seen["events"] = fence_events(db2, env.run_id)
            seen["run"] = db2.execute("SELECT state FROM runs WHERE id=?", (env.run_id,)).fetchone()[0]
            seen["attempts"] = db2.execute("SELECT state FROM task_attempts").fetchall()
        finally:
            db2.close()
        env.runtime._active.cancel()
        await asyncio.gather(env.runtime._active, return_exceptions=True)
    asyncio.run(go())
    deadline = time.monotonic() + 5
    while alive(seen["pid"]) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not alive(seen["pid"])
    assert seen["report"] == FenceReport(0, 1, 0) and seen["report"].all_clear
    assert seen["events"] == [{"run_id": env.run_id, "principal": "agent-recovery", "verified_dead": 0, "killed": 1,
                               "unverified": 0, "all_clear": True}]
    assert seen["run"] == "recovering" and seen["attempts"] == [("unknown",)]  # recovery itself is unchanged
    assert env.spawns() == ["hang"]  # nothing was re-spawned


def test_an_unprovable_spawn_is_reported_unverified_and_recovery_still_fails_closed(env, tmp_path):
    journal = SpawnJournal(env.state / "spawn-journal")
    image = tmp_path / "crash.db"

    async def go():
        env.runtime = journalled(env, journal)
        env.runtime.start(env.run_id, "s", env.specs(prompt="hang"))
        await env.until(lambda: (env.worktrees["work"] / "pid").exists() and env.attempts() == ["running"])
        dst = sqlite3.connect(image)
        env.db.backup(dst)
        dst.close()
        env.runtime._active.cancel()
        await asyncio.gather(env.runtime._active, return_exceptions=True)
    asyncio.run(go())
    for leftover in journal._dir.glob("*.json"):
        leftover.unlink()
    journal.begin("crashed-before-pid-was-recorded")  # a spawn whose pid never reached disk
    db = open_database(image)
    try:
        runtime = AgentRuntime(db, env.blobs, profile=env.profile, spawn_journal=SpawnJournal(env.state / "spawn-journal"))
        runtime.recover_interrupted()
        assert runtime.fence_report == FenceReport(0, 0, 1) and not runtime.fence_report.all_clear
        assert fence_events(db, env.run_id)[0]["all_clear"] is False and fence_events(db, env.run_id)[0]["unverified"] == 1
        assert db.execute("SELECT state FROM task_attempts").fetchall() == [("unknown",)]
        assert db.execute("SELECT state FROM runs WHERE id=?", (env.run_id,)).fetchone() == ("recovering",)
        assert (journal._dir / "crashed-before-pid-was-recorded.unverified").exists()
    finally:
        db.close()


def test_a_clean_restart_records_nothing_and_a_run_without_unresolved_work_gets_no_event(env):
    runtime = journalled(env, SpawnJournal(env.state / "spawn-journal"))
    runtime.recover_interrupted()
    assert runtime.fence_report == FenceReport() and fence_events(env.db, env.run_id) == []


# --------------------------------------------------------------------------- provider lifecycle
def pending_pids(journal):
    return [json.loads((journal._dir / (k + ".json")).read_text()).get("pid") for k in journal.pending()]


def test_journal_entry_exists_while_the_agent_runs_and_is_gone_after_every_ending(provider_env, tmp_path):  # noqa: F811
    journal = SpawnJournal(tmp_path / "j")
    mid = []

    async def watch(prompt, cancel):
        prov = ClaudeCliProvider(profile(provider_env), spawn_journal=journal)
        handle = await prov.start(request(provider_env, prompt))
        events = []

        async def consume():
            async for e in prov.stream(handle):
                events.append(e)
        task = asyncio.create_task(consume())
        if cancel:
            await asyncio.sleep(0.6)
            mid.append(pending_pids(journal))
            await prov.cancel(handle)
        await asyncio.wait_for(task, 30)
        return events

    for prompt, cancel in (("ok", False), ("hang", True)):
        events = asyncio.run(watch(prompt, cancel))
        assert events[-1].kind in ("succeeded", "cancelled")
        assert journal.pending() == []  # removed once the process was reaped
    assert len(mid) == 1 and len(mid[0]) == 1 and isinstance(mid[0][0], int)  # a real pid was recorded mid-run


def test_a_failed_spawn_leaves_no_entry_and_an_unjournalable_spawn_never_happens(provider_env, tmp_path):  # noqa: F811
    journal = SpawnJournal(tmp_path / "j")
    prov = ClaudeCliProvider(profile(provider_env, executable=str(provider_env[0] / "nope")), spawn_journal=journal)
    _, ev = asyncio.run(run(prov, request(provider_env, "ok")))
    assert ev[-1].text == "spawn_failed" and journal.pending() == []

    class Broken(SpawnJournal):
        def begin(self, key):
            raise OSError(28, "no space")
    prov = ClaudeCliProvider(profile(provider_env), spawn_journal=Broken(tmp_path / "j2"))
    handle, ev = asyncio.run(run(prov, request(provider_env, "ok")))
    assert ev[-1].kind == "failed" and ev[-1].text == "spawn_failed"
    assert prov._execution(handle).process is None  # cannot journal, so nothing was started
