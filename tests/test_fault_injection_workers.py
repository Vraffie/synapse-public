"""Deterministic fault injection against the mock engine/runtime (ARCHITECTURE.md §13).

Every scenario ends by running the database invariant checker (tests/invariants.py). Scenarios that
are already covered thoroughly elsewhere are listed in docs/operations/fault-injection.md instead of
being duplicated here. Synchronisation is by polling state, never by fixed sleeps.
"""
import asyncio
import errno
import shutil
import sqlite3

import pytest

from fault_fixtures import starts, world  # noqa: F401  (fixtures)
from invariants import assert_invariants
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.persistence import artifact_store
from synapse.persistence.migrations import open_database
from synapse.providers.contracts import ProviderEvent
from synapse.providers.mock import MockProvider


def run_scenarios(world, scenarios, key="start"):
    async def go():
        world.runtime.start(world.run_id, key, scenarios)
        await world.runtime._active
    asyncio.run(go())


# ------------------------------------------------------------------ worker kill mid-attempt
def test_worker_killed_mid_attempt_becomes_unknown_and_is_never_respawned(world, starts):
    async def go():
        world.runtime.start(world.run_id, "start", {"build": "slow", "review": "succeed"})
        await world.until(lambda: world.states()[1]["build"] == "running")
        assert_invariants(world.db)  # a live snapshot mid-attempt is consistent
        await world.stop()
    asyncio.run(go())
    assert_invariants(world.db, quiescent=True)  # the crash image itself
    assert world.attempts() == [("build", 1, "unknown")]
    world.restart()
    assert world.states()[0] == "recovering"
    assert_invariants(world.db, quiescent=True)

    async def replay():
        assert world.runtime.start(world.run_id, "start", {"build": "slow", "review": "succeed"}).replayed
    asyncio.run(replay())
    assert world.attempts() == [("build", 1, "unknown")] and starts == [world.db.execute(
        "SELECT id FROM tasks WHERE node_id='build'").fetchone()[0]]  # exactly one launch ever


@pytest.mark.parametrize("moment", ["first_task_running", "second_task_running"])
def test_crash_image_at_each_stage_recovers_without_a_new_attempt(world, starts, tmp_path, moment):
    """Take the committed database as a SIGKILL would leave it, then recover that copy."""
    target = "build" if moment == "first_task_running" else "review"
    image = tmp_path / "crash.db"

    async def go():
        world.runtime.start(world.run_id, "start", {"build": "slow", "review": "slow"})
        await world.until(lambda: world.states()[1][target] == "running")
        world.snapshot_to(image)
        assert_invariants(world.db)
        await world.stop()
    asyncio.run(go())
    launched_before = len(starts)
    world.db.close()
    shutil.copy(image, world.path)
    for suffix in ("-wal", "-shm"):
        (world.path.parent / ("db" + suffix)).unlink(missing_ok=True)
    world.restart()
    assert world.states()[0] == "recovering"
    assert_invariants(world.db, quiescent=True)
    assert not [a for a in world.attempts() if a[2] in ("created", "running")]
    assert len(starts) == launched_before  # recovery launches nothing


# ------------------------------------------------------------------ corrupted provider output
def _stream(events, raises=None):
    async def stream(self, handle):
        for event in events:
            yield event
        if raises is not None:
            raise raises
    return stream


BAD_STREAMS = {
    "unexpected_event_kind": ([ProviderEvent("started"), ProviderEvent("bogus")], None),
    "output_after_terminal": ([ProviderEvent("started"), ProviderEvent("succeeded"), ProviderEvent("output", "late")], None),
    "no_terminal_event": ([ProviderEvent("started"), ProviderEvent("output", "partial")], None),
    "non_text_output": ([ProviderEvent("started"), ProviderEvent("output", 123)], None),
    "stream_raises_mid_run": ([ProviderEvent("started"), ProviderEvent("output", "x")], RuntimeError("worker died")),
}


@pytest.mark.parametrize("name", sorted(BAD_STREAMS))
def test_corrupted_provider_output_blocks_the_task_without_retry(world, starts, monkeypatch, name):
    events, raises = BAD_STREAMS[name]
    monkeypatch.setattr(MockProvider, "stream", _stream(events, raises))
    run_scenarios(world, {"build": ["succeed", "succeed"], "review": "succeed"})
    run, tasks = world.states()
    assert run == "recovering" and tasks["build"] == "blocked"
    assert world.attempts() == [("build", 1, "unknown")]  # ambiguous outcome: recorded, not retried
    assert len(starts) == 1
    assert_invariants(world.db, quiescent=True, blob_dir=world.blobs)
    world.restart()
    assert_invariants(world.db, quiescent=True)
    assert len(starts) == 1 and world.attempts() == [("build", 1, "unknown")]


# ------------------------------------------------------------------ disk full / crash around the blob write
def test_disk_full_on_artifact_write_leaves_no_partial_state(world, starts, monkeypatch):
    def full(self, chunks):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(ArtifactBlobStore, "put", full)
    run_scenarios(world, {"build": "succeed", "review": "succeed"})
    assert world.states()[1]["build"] == "blocked" and world.states()[0] == "recovering"
    assert world.db.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (0,)
    assert world.db.execute("SELECT COUNT(*) FROM verification_results").fetchone() == (0,)
    assert world.attempts() == [("build", 1, "completed")]  # the provider finished; only the evidence is missing
    assert_invariants(world.db, quiescent=True, blob_dir=world.blobs)
    monkeypatch.undo()  # the disk has room again; the run is still not retried automatically
    world.restart()
    assert world.states()[0] == "recovering" and len(starts) == 1
    assert_invariants(world.db, quiescent=True, blob_dir=world.blobs)


def test_crash_between_blob_write_and_metadata_commit_leaves_only_an_orphan_blob(world, starts, monkeypatch):
    real = artifact_store.execute_idempotent
    seen = []

    def crash_once(*args, **kwargs):
        if not seen:
            seen.append(1)
            raise RuntimeError("process died after the blob was written")
        return real(*args, **kwargs)
    monkeypatch.setattr(artifact_store, "execute_idempotent", crash_once)
    run_scenarios(world, {"build": "succeed", "review": "succeed"})
    assert world.db.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (0,)
    assert any(p.is_file() for p in world.blobs.rglob("*"))  # the blob exists without metadata
    assert world.states()[1]["build"] == "blocked"
    assert_invariants(world.db, quiescent=True, blob_dir=world.blobs)  # an orphan is allowed, a dangling row is not
    # Re-publishing the same bytes is idempotent and content-addressed: metadata appears, no second blob.
    blobs_before = sorted(p.name for p in world.blobs.rglob("*") if p.is_file())
    artifact_store.publish_artifact(world.db, ArtifactBlobStore(world.blobs, max_bytes=65536), chunks=[b"synapse mock candidate v1\n"],
                                    artifact_id="late", media_type="text/plain", provenance={"run_id": world.run_id},
                                    principal="daemon", idempotency_key="late")
    assert sorted(p.name for p in world.blobs.rglob("*") if p.is_file()) == blobs_before
    assert_invariants(world.db, quiescent=True, blob_dir=world.blobs)


# ------------------------------------------------------------------ SQLite lock contention
def test_a_second_writer_holding_the_lock_fails_a_start_cleanly(world):
    world.db.execute("PRAGMA busy_timeout=20")
    other = sqlite3.connect(world.path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        before = world.states()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            asyncio.run(_start(world))
        assert world.states() == before and not world.db.in_transaction  # nothing half-applied
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert_invariants(world.db)
    run_scenarios(world, {"build": "succeed", "review": "succeed"})
    assert world.states()[0] == "succeeded"
    assert_invariants(world.db, quiescent=True, blob_dir=world.blobs)


async def _start(world):
    world.runtime.start(world.run_id, "start", {"build": "succeed", "review": "succeed"})
    await world.runtime._active


def test_write_lock_taken_mid_run_never_corrupts_and_recovery_is_clean(world, starts):
    other = sqlite3.connect(world.path, isolation_level=None)

    async def go():
        world.runtime.start(world.run_id, "start", {"build": "slow", "review": "succeed"})
        await world.until(lambda: world.states()[1]["build"] == "running")
        world.db.execute("PRAGMA busy_timeout=10")
        other.execute("BEGIN IMMEDIATE")  # an external writer wedges the database
        try:
            await asyncio.wait_for(asyncio.shield(world.runtime._active), 5)
        finally:
            other.execute("ROLLBACK")
    asyncio.run(go())
    other.close()
    world.db.execute("PRAGMA busy_timeout=5000")
    # The engine could not write its transition: the database is a crash image (attempt still "running").
    assert world.states() == ("running", {"build": "running", "review": "waiting_dependencies"})
    assert world.attempts() == [("build", 1, "running")]
    assert_invariants(world.db)  # whatever state the failed writes left is still consistent
    world.restart()
    assert_invariants(world.db, quiescent=True, blob_dir=world.blobs)
    assert not [a for a in world.attempts() if a[2] in ("created", "running")]
    assert len(starts) == 1  # the wedged engine never launched anything else


def test_a_short_lock_wait_is_absorbed_by_the_busy_timeout(world):
    import threading
    other = sqlite3.connect(world.path, isolation_level=None, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")
    threading.Timer(0.2, lambda: (other.execute("ROLLBACK"), other.close())).start()
    run_scenarios(world, {"build": "succeed", "review": "succeed"})  # waits (busy_timeout 5 s) for the writer
    assert world.states()[0] == "succeeded"
    assert_invariants(world.db, quiescent=True, blob_dir=world.blobs)


# ------------------------------------------------------------------ explicit resume after a crash
def test_resume_after_a_crash_launches_exactly_one_new_attempt_and_keeps_every_invariant(world, starts):
    from synapse.persistence.mock_resume import ASSERTION

    async def crash():
        world.runtime.start(world.run_id, "start")  # the fixed fixture is the only resumable kind
        await world.until(lambda: world.states()[1]["build"] == "running")
        await world.stop()
    asyncio.run(crash())
    world.restart()
    assert world.states()[0] == "recovering" and world.attempts() == [("build", 1, "unknown")]
    assert_invariants(world.db, quiescent=True)
    build, version, attempt = world.db.execute(
        "SELECT t.id,t.state_version,a.id FROM tasks t JOIN task_attempts a ON a.task_id=t.id WHERE t.node_id='build'").fetchone()
    run_version = world.db.execute("SELECT state_version FROM runs").fetchone()[0]
    launched = len(starts)

    async def resume():
        world.runtime.resume(world.run_id, "resume", "trace", run_version,
                             [{"task_id": build, "expected_version": version, "unknown_attempt_id": attempt}], ASSERTION)
        assert_invariants(world.db)  # right after the operator's reconciliation commit
        await asyncio.wait_for(world.runtime._active, 10)
    asyncio.run(resume())
    assert world.states() == ("succeeded", {"build": "completed", "review": "completed"})
    assert world.attempts() == [("build", 1, "failed"), ("build", 2, "completed"), ("review", 1, "completed")]
    assert len(starts) - launched == 2  # one retry of build plus review: never a duplicate of the unknown attempt
    assert_invariants(world.db, quiescent=True, blob_dir=world.blobs)
