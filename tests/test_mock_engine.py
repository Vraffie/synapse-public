import asyncio
from pathlib import Path

import pytest

from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.orchestration.mock_engine import MockWorkflowEngine
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.providers.mock import MockOutcome, MockProvider, MockScenario

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "state.db")
    apply_migrations(conn, ROOT / "migrations")
    conn.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
    conn.execute("INSERT INTO runs VALUES('run','wf','1','running',0,'now','now')")
    conn.execute("INSERT INTO profiles(id,provider,display_name,config_ref,max_concurrency,created_at) VALUES('mock','mock','Mock','mock',6,'now')")
    yield conn
    conn.close()


def insert_task(db, task):
    db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES(?,'run',?,'created','now','now')", (task, task))


def test_dependency_review_retry_and_durable_verification(db, tmp_path):
    insert_task(db, "a")
    insert_task(db, "b")
    db.execute("INSERT INTO task_dependencies VALUES('b','a')")
    provider = MockProvider({"bad": MockScenario(("bad",), MockOutcome.SUCCEEDED), "good": MockScenario(("good",), MockOutcome.SUCCEEDED)})
    reviewed = []
    async def verifier(task, candidate):
        reviewed.append((task, candidate))
        return candidate == b"good"
    engine = MockWorkflowEngine(db, provider, ArtifactBlobStore(tmp_path, max_bytes=100), verifier, verifier_id="reviewer")
    result = asyncio.run(engine.run("run", "mock", {"a": ("bad", "good"), "b": ("good",)}))
    assert result == "succeeded"
    assert reviewed == [("a", b"bad"), ("a", b"good"), ("b", b"good")]
    assert db.execute("SELECT id,state,attempt_count FROM tasks ORDER BY id").fetchall() == [("a", "completed", 2), ("b", "completed", 1)]
    assert db.execute("SELECT COUNT(*) FROM verification_results").fetchone() == (3,)
    assert db.execute("SELECT COUNT(*) FROM events").fetchone() == db.execute("SELECT COUNT(*) FROM outbox").fetchone()
    assert db.execute("SELECT state FROM runs WHERE id='run'").fetchone() == ("succeeded",)


def test_bounded_provider_retry(db, tmp_path):
    insert_task(db, "a")
    provider = MockProvider({"fail": MockScenario((), MockOutcome.FAILED), "ok": MockScenario(("ok",), MockOutcome.SUCCEEDED)})
    async def verifier(task, candidate):
        return True
    engine = MockWorkflowEngine(db, provider, ArtifactBlobStore(tmp_path, max_bytes=100), verifier, verifier_id="reviewer")
    assert asyncio.run(engine.run("run", "mock", {"a": ("fail", "ok")})) == "succeeded"
    assert db.execute("SELECT state FROM task_attempts ORDER BY attempt_no").fetchall() == [("failed",), ("completed",)]


def test_unexpected_provider_failure_blocks_without_retry(db, tmp_path):
    insert_task(db, "a")
    provider = MockProvider({"ok": MockScenario(("too big",), MockOutcome.SUCCEEDED)})
    async def verifier(task, candidate):
        return True
    engine = MockWorkflowEngine(db, provider, ArtifactBlobStore(tmp_path, max_bytes=100), verifier, verifier_id="reviewer", max_output_bytes=1)
    with pytest.raises(ValueError, match="exceeds"):
        asyncio.run(engine.run("run", "mock", {"a": ("ok",)}))
    assert db.execute("SELECT state FROM tasks").fetchone() == ("blocked",)
    assert db.execute("SELECT state FROM task_attempts").fetchone() == ("unknown",)
    assert db.execute("SELECT state FROM runs").fetchone() == ("running",)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    reopened = open_database(path)
    try:
        recovered = MockWorkflowEngine(reopened, provider, ArtifactBlobStore(tmp_path, max_bytes=100), verifier, verifier_id="reviewer")
        assert asyncio.run(recovered.run("run", "mock", {"a": ("ok",)})) == "blocked"
        assert reopened.execute("SELECT COUNT(*) FROM task_attempts").fetchone() == (1,)
    finally:
        reopened.close()


def test_parallel_workers_and_cancellation(db, tmp_path):
    insert_task(db, "a")
    insert_task(db, "b")
    provider = MockProvider({"slow": MockScenario(tuple("x" for _ in range(500)), MockOutcome.SUCCEEDED)})
    async def verifier(task, candidate):
        return True
    engine = MockWorkflowEngine(db, provider, ArtifactBlobStore(tmp_path, max_bytes=1024), verifier, verifier_id="reviewer", max_concurrency=2)
    async def exercise():
        running = asyncio.create_task(engine.run("run", "mock", {"a": ("slow",), "b": ("slow",)}))
        for _ in range(100):
            if len(engine._active) == 2 and len(engine.provider._active) == 2:
                break
            await asyncio.sleep(0)
        assert len(engine._active) == len(engine.provider._active) == 2
        assert await engine.cancel("a")
        return await running
    assert asyncio.run(exercise()) == "failed"
    assert db.execute("SELECT id,state FROM tasks ORDER BY id").fetchall() == [("a", "cancelled"), ("b", "completed")]
    assert db.execute("SELECT state FROM task_attempts WHERE task_id='a'").fetchone() == ("cancelled",)


def test_verifier_exception_does_not_certify_candidate(db, tmp_path):
    insert_task(db, "a")
    provider = MockProvider({"ok": MockScenario(("ok",), MockOutcome.SUCCEEDED)})
    async def verifier(task, candidate):
        raise RuntimeError("verifier unavailable")
    engine = MockWorkflowEngine(db, provider, ArtifactBlobStore(tmp_path, max_bytes=100), verifier, verifier_id="reviewer")
    with pytest.raises(RuntimeError, match="verifier unavailable"):
        asyncio.run(engine.run("run", "mock", {"a": ("ok",)}))
    assert db.execute("SELECT state FROM tasks").fetchone() == ("blocked",)
    assert db.execute("SELECT COUNT(*) FROM verification_results").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (1,)


def test_failed_verification_record_rolls_back_and_blocks(db, tmp_path):
    insert_task(db, "a")
    db.execute("CREATE TRIGGER deny_verification BEFORE INSERT ON verification_results BEGIN SELECT RAISE(ABORT, 'verification failed'); END")
    provider = MockProvider({"ok": MockScenario(("ok",), MockOutcome.SUCCEEDED)})
    async def verifier(task, candidate):
        return True
    engine = MockWorkflowEngine(db, provider, ArtifactBlobStore(tmp_path, max_bytes=100), verifier, verifier_id="reviewer")
    with pytest.raises(Exception, match="verification failed"):
        asyncio.run(engine.run("run", "mock", {"a": ("ok",)}))
    assert db.execute("SELECT state FROM tasks").fetchone() == ("blocked",)
    assert db.execute("SELECT COUNT(*) FROM verification_results").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM events WHERE type='verification.recorded'").fetchone() == (0,)


def test_cancel_before_worker_starts_reconciles_reservation(db, tmp_path, monkeypatch):
    insert_task(db, "a")
    provider = MockProvider({"ok": MockScenario(("ok",), MockOutcome.SUCCEEDED)})
    async def verifier(task, candidate):
        return True
    engine = MockWorkflowEngine(db, provider, ArtifactBlobStore(tmp_path, max_bytes=100), verifier, verifier_id="reviewer")
    async def exercise():
        original = asyncio.create_task
        async def drive():
            return await engine.run("run", "mock", {"a": ("ok",)})
        run = original(drive())
        def cancelled_before_start(coro):
            task = original(coro)
            task.cancel()
            return task
        monkeypatch.setattr(asyncio, "create_task", cancelled_before_start)
        with pytest.raises(asyncio.CancelledError):
            await run
    asyncio.run(exercise())
    assert db.execute("SELECT state FROM tasks").fetchone() == ("blocked",)
    assert db.execute("SELECT state FROM task_attempts").fetchone() == ("unknown",)


def test_pause_while_verifier_waits_blocks_task_without_certification(db, tmp_path):
    from synapse.domain.runs import RunChange, RunState
    from synapse.persistence.run_store import write_run_transition
    insert_task(db, "a")
    insert_task(db, "b")
    db.execute("INSERT INTO task_dependencies VALUES('b','a')")
    provider = MockProvider({"ok": MockScenario(("ok",), MockOutcome.SUCCEEDED)})
    async def exercise():
        verifying = asyncio.Event()
        async def verifier(task, candidate):
            verifying.set()
            await asyncio.Event().wait()
            return True
        engine = MockWorkflowEngine(db, provider, ArtifactBlobStore(tmp_path, max_bytes=100), verifier, verifier_id="reviewer")
        run = asyncio.create_task(engine.run("run", "mock", {"a": ("ok",), "b": ("ok",)}))
        await asyncio.wait_for(verifying.wait(), timeout=2)
        db.execute("BEGIN IMMEDIATE")
        write_run_transition(db, RunChange("run", RunState.RUNNING, RunState.PAUSED, 0), principal="operator")
        db.execute("COMMIT")
        assert await asyncio.wait_for(run, timeout=2) == "paused"
    asyncio.run(exercise())
    assert db.execute("SELECT id,state FROM tasks ORDER BY id").fetchall() == [("a", "blocked"), ("b", "waiting_dependencies")]
    assert db.execute("SELECT COUNT(*) FROM verification_results").fetchone() == (0,)


def test_persistence_rejects_cross_run_and_invalid_candidate(db):
    from synapse.persistence.mock_execution_store import record_verification, set_attempt
    from synapse.persistence.state_store import StateConflict
    insert_task(db, "a")
    db.execute("INSERT INTO runs VALUES('other','wf','1','running',0,'now','now')")
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('attempt','a',1,'mock','running')")
    db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StateConflict):
            set_attempt(db, attempt_id="attempt", before="running", after="completed", run_id="other", task_id="a")
        with pytest.raises(ValueError):
            record_verification(db, run_id="run", task_id="a", task_version=0, candidate_sha256="not-a-hash", artifact_id="artifact", verifier_id="reviewer", approved=True)
        with pytest.raises(ValueError):
            record_verification(db, run_id="run", task_id="a", task_version=0, candidate_sha256="a" * 64, artifact_id="artifact", verifier_id="reviewer", approved=1)
        with pytest.raises(StateConflict):
            record_verification(db, run_id="other", task_id="a", task_version=0, candidate_sha256="a" * 64, artifact_id="artifact", verifier_id="reviewer", approved=True)
    finally:
        db.execute("ROLLBACK")
    assert db.execute("SELECT state FROM task_attempts").fetchone() == ("running",)


def test_ready_task_at_retry_limit_is_not_claimed(db, tmp_path):
    insert_task(db, "a")
    db.execute("UPDATE tasks SET state='ready',attempt_count=2 WHERE id='a'")
    provider = MockProvider({"ok": MockScenario((), MockOutcome.SUCCEEDED)})
    async def verifier(task, candidate):
        return True
    engine = MockWorkflowEngine(db, provider, ArtifactBlobStore(tmp_path, max_bytes=100), verifier, verifier_id="reviewer", max_attempts=2)
    assert asyncio.run(engine.run("run", "mock", {"a": ("ok",)})) == "blocked"
    assert db.execute("SELECT state,attempt_count FROM tasks").fetchone() == ("ready", 2)
    assert db.execute("SELECT COUNT(*) FROM task_attempts").fetchone() == (0,)


def test_generic_completion_rejected_and_evidence_must_match_latest_attempt(db):
    from synapse.domain.workflow import State, StateChange
    from synapse.persistence.state_store import StateConflict, transition_task, write_verified_task_transition
    insert_task(db, "a")
    db.execute("UPDATE tasks SET state='verifying',attempt_count=2 WHERE id='a'")
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('old','a',1,'mock','completed')")
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('new','a',2,'mock','completed')")
    db.execute("INSERT INTO artifacts VALUES('artifact',?,'text/plain',1,'path',?, 'now')", ('a' * 64, '{"run_id":"run","task_id":"a","attempt_id":"old"}'))
    db.execute("INSERT INTO verification_results(id,run_id,task_id,candidate_sha256,verifier_id,outcome,created_at) VALUES('v','run','a',?,'reviewer','approved','now')", ('a' * 64,))
    change = StateChange("a", State.VERIFYING, State.COMPLETED, 0)
    with pytest.raises(ValueError, match="verified evidence"):
        transition_task(db, change, principal="reviewer")
    db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StateConflict, match="evidence"):
            write_verified_task_transition(db, change, principal="reviewer", verification_id="v", artifact_id="artifact")
    finally:
        db.execute("ROLLBACK")
    assert db.execute("SELECT state FROM tasks").fetchone() == ("verifying",)


def test_run_pause_before_finalization_is_not_overwritten(db, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from synapse.domain.runs import RunChange, RunState
    from synapse.persistence.run_store import write_run_transition
    insert_task(db, "a")
    provider = MockProvider({"ok": MockScenario(("ok",), MockOutcome.SUCCEEDED)})
    async def verifier(task, candidate):
        return True
    engine = MockWorkflowEngine(db, provider, ArtifactBlobStore(tmp_path, max_bytes=100), verifier, verifier_id="reviewer")
    transaction = engine._transaction
    paused = False
    @contextmanager
    def pause_before_finalization():
        nonlocal paused
        if not paused and db.execute("SELECT state FROM tasks WHERE id='a'").fetchone() == ("completed",):
            paused = True
            db.execute("BEGIN IMMEDIATE")
            write_run_transition(db, RunChange("run", RunState.RUNNING, RunState.PAUSED, 0), principal="operator")
            db.execute("COMMIT")
        with transaction():
            yield
    monkeypatch.setattr(engine, "_transaction", pause_before_finalization)
    assert asyncio.run(engine.run("run", "mock", {"a": ("ok",)})) == "paused"
    assert paused and db.execute("SELECT state FROM runs").fetchone() == ("paused",)


def test_generic_approval_completion_rejected(db):
    from synapse.domain.workflow import State, StateChange
    from synapse.persistence.state_store import transition_task
    insert_task(db, "a")
    db.execute("UPDATE tasks SET state='awaiting_approval' WHERE id='a'")
    with pytest.raises(ValueError, match="candidate-bound approval"):
        transition_task(db, StateChange("a", State.AWAITING_APPROVAL, State.COMPLETED, 0), principal="operator")
    assert db.execute("SELECT state,state_version FROM tasks").fetchone() == ("awaiting_approval", 0)
    assert db.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM outbox").fetchone() == (0,)
