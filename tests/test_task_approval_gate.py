import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from synapse.domain.approvals import ApprovalChange, ApprovalState
from synapse.domain.contracts import ApprovalId
from synapse.domain.workflow import InvalidTransition, State, StateChange, TRANSITIONS, validate_transition
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.orchestration.mock_engine import MockWorkflowEngine
from synapse.persistence.approval_store import write_approval_request, write_approval_transition
from synapse.persistence.attempt_store import reserve_attempt
from synapse.persistence.idempotency import execute_idempotent
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.mock_recovery import recover_mock_run
from synapse.persistence.state_store import StateConflict, transition_task, write_task_transition
from synapse.persistence.task_approval_gate import (
    BindingRecheckRequired, park_task_for_approval, resolve_task_approval_gate,
    write_task_approval_gate, write_task_approval_resolution,
)
from synapse.persistence.task_claim import claim_task
from synapse.providers.mock import MockOutcome, MockProvider, MockScenario

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "a" * 64


def _base(conn):
    conn.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
    conn.execute("INSERT INTO runs VALUES('run','wf','1','running',0,'now','now')")
    conn.execute("INSERT INTO profiles(id,provider,display_name,config_ref,max_concurrency,created_at) VALUES('mock','mock','Mock','mock',6,'now')")
    for task, state in (("gated", "running"), ("sibling", "running"), ("other", "running")):
        conn.execute("INSERT INTO tasks(id,run_id,node_id,state,state_version,assigned_profile_id,attempt_count,created_at,updated_at) VALUES(?,'run',?,?,3,'mock',1,'now','now')", (task, task, state))
        conn.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES(?,?,1,'mock','completed')", ("att-" + task, task))


def _request(conn, approval_id="appr", run_id="run", expires=None):
    conn.execute("BEGIN IMMEDIATE")
    write_approval_request(conn, approval_id=approval_id, run_id=run_id, operation="push", target="repo",
                           candidate_sha256=DIGEST, policy_version="1",
                           expires_at=(expires or datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    conn.execute("COMMIT")


def _decide(conn, after, approval_id="appr", expire=False):
    if expire:
        conn.execute("DROP TRIGGER approval_binding_immutable")
        conn.execute("UPDATE approval_requests SET expires_at=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), approval_id))
    conn.execute("BEGIN IMMEDIATE")
    write_approval_transition(conn, ApprovalChange(ApprovalId(approval_id), ApprovalState.REQUESTED, after, 0),
                              principal="human", candidate_sha256=DIGEST, policy_version="1")
    conn.execute("COMMIT")


@pytest.fixture
def db(tmp_path):
    with closing(open_database(tmp_path / "gate.db")) as conn:
        apply_migrations(conn, ROOT / "migrations")
        _base(conn)
        _request(conn)
        yield conn


def park(db, task="gated", approval="appr", version=3):
    return park_task_for_approval(db, task_id=task, approval_id=approval, expected_task_version=version, principal="daemon")


def resolve(db, task="gated", version=4, **kw):
    return resolve_task_approval_gate(db, task_id=task, expected_task_version=version, principal="daemon", **kw)


def counts(db):
    return db.execute("SELECT COUNT(*) FROM events").fetchone()[0], db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]


def task(db, ident="gated"):
    return db.execute("SELECT state,state_version FROM tasks WHERE id=?", (ident,)).fetchone()


# --- domain -----------------------------------------------------------------

def test_transitions():
    validate_transition(State.RUNNING, State.AWAITING_APPROVAL)
    for after in (State.RUNNING, State.FAILED, State.CANCELLED):
        validate_transition(State.AWAITING_APPROVAL, after)
    # Pre-existing post-verification path is unchanged.
    validate_transition(State.VERIFYING, State.AWAITING_APPROVAL)
    validate_transition(State.AWAITING_APPROVAL, State.COMPLETED)
    for illegal in (State.READY, State.ASSIGNED, State.RETRY_WAIT, State.BLOCKED, State.VERIFYING, State.WAITING_DEPENDENCIES):
        assert illegal not in TRANSITIONS[State.AWAITING_APPROVAL]
    with pytest.raises(InvalidTransition):
        validate_transition(State.READY, State.AWAITING_APPROVAL)
    with pytest.raises(InvalidTransition):
        validate_transition(State.ASSIGNED, State.AWAITING_APPROVAL)


@pytest.mark.parametrize("before,after", [
    (State.RUNNING, State.AWAITING_APPROVAL), (State.AWAITING_APPROVAL, State.RUNNING), (State.AWAITING_APPROVAL, State.FAILED)])
def test_generic_store_refuses_gate_only_transitions(db, before, after):
    version = 3
    if before == State.AWAITING_APPROVAL:
        version = park(db)
    with pytest.raises(ValueError, match="task_approval_gate"):
        transition_task(db, StateChange("gated", before, after, version), principal="daemon")
    assert task(db) == (before.value, version)


def test_generic_cancel_of_parked_task_allowed(db):
    park(db)
    assert transition_task(db, StateChange("gated", State.AWAITING_APPROVAL, State.CANCELLED, 4), principal="daemon") == 5


# --- migration --------------------------------------------------------------

def test_migration_upgrade_from_0011_preserves_rows(tmp_path):
    directory = tmp_path / "m"
    directory.mkdir()
    for path in sorted((ROOT / "migrations").glob("*.sql")):
        if int(path.name[:4]) <= 11:
            (directory / path.name).write_bytes(path.read_bytes())
    with closing(open_database(tmp_path / "up.db")) as conn:
        assert apply_migrations(conn, directory)[-1] == 11
        _base(conn)
        conn.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES('verifier','run','verifier','awaiting_approval','now','now')")
        _request(conn)
        before = conn.execute("SELECT id,state,state_version FROM tasks ORDER BY id").fetchall()
        source = ROOT / "migrations" / "0012_task_approval_gates.sql"
        (directory / source.name).write_bytes(source.read_bytes())
        assert apply_migrations(conn, directory) == [12]
        assert conn.execute("SELECT id,state,state_version FROM tasks ORDER BY id").fetchall() == before
        assert conn.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)
        assert conn.execute("SELECT COUNT(*) FROM task_approval_gates").fetchone() == (0,)
        assert park(conn) == 4
        assert task(conn, "verifier") == ("awaiting_approval", 0)


def test_database_refuses_ungated_park_and_unauthorized_exit(db):
    with pytest.raises(sqlite3.IntegrityError, match="requires a requested approval gate"):
        db.execute("UPDATE tasks SET state='awaiting_approval' WHERE id='gated'")
    park(db)
    with pytest.raises(sqlite3.IntegrityError, match="approved gate"):
        db.execute("UPDATE tasks SET state='running' WHERE id='gated'")
    with pytest.raises(sqlite3.IntegrityError, match="decided gate"):
        db.execute("UPDATE tasks SET state='failed' WHERE id='gated'")


# --- park -------------------------------------------------------------------

def test_park_is_atomic_with_event_and_outbox(db):
    assert park(db) == 4
    assert task(db) == ("awaiting_approval", 4)
    assert db.execute("SELECT task_id,approval_id,run_id FROM task_approval_gates").fetchall() == [("gated", "appr", "run")]
    # approval.requested + state_changed + approval_gated, each with an outbox row.
    assert counts(db) == (3, 3)
    types = [r[0] for r in db.execute("SELECT type FROM events WHERE aggregate_type='task' ORDER BY rowid")]
    assert types == ["task.state_changed", "task.approval_gated"]
    payload = json.loads(db.execute("SELECT payload_json FROM events WHERE type='task.approval_gated'").fetchone()[0])
    assert payload["approval_id"] == "appr"
    # Siblings and run are untouched.
    assert task(db, "sibling") == ("running", 3)
    assert db.execute("SELECT state FROM runs").fetchone() == ("running",)


def test_stale_version_and_wrong_state_refused(db):
    with pytest.raises(StateConflict):
        park(db, version=2)
    db.execute("UPDATE tasks SET state='ready' WHERE id='other'")
    with pytest.raises(StateConflict):
        park(db, task="other", version=3)
    assert db.execute("SELECT COUNT(*) FROM task_approval_gates").fetchone() == (0,)
    assert counts(db) == (1, 1)


@pytest.mark.parametrize("decision", [ApprovalState.REJECTED, ApprovalState.APPROVED])
def test_decided_approval_cannot_gate(db, decision):
    _decide(db, decision)
    with pytest.raises(StateConflict, match="not pending"):
        park(db)
    assert task(db) == ("running", 3)


def test_approval_must_exist_in_same_run_and_be_unexpired(db):
    with pytest.raises(StateConflict, match="does not exist"):
        park(db, approval="missing")
    db.execute("INSERT INTO runs VALUES('run2','wf','1','running',0,'now','now')")
    _request(db, "foreign", run_id="run2")
    with pytest.raises(StateConflict, match="does not exist"):
        park(db, approval="foreign")
    db.execute("DROP TRIGGER approval_binding_immutable")
    db.execute("UPDATE approval_requests SET expires_at=? WHERE id='appr'", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
    with pytest.raises(StateConflict, match="expired"):
        park(db)
    assert task(db) == ("running", 3)


def test_run_must_be_running_and_attempt_resolved(db):
    db.execute("UPDATE task_attempts SET state='running' WHERE id='att-gated'")
    with pytest.raises(StateConflict, match="unresolved attempt"):
        park(db)
    db.execute("UPDATE task_attempts SET state='completed' WHERE id='att-gated'")
    db.execute("UPDATE runs SET state='paused'")
    with pytest.raises(StateConflict, match="Run is not running"):
        park(db)


def test_gate_is_unique_per_task_and_per_approval(db):
    park(db)
    _request(db, "appr2")
    with pytest.raises(StateConflict):
        park(db, approval="appr2", version=4)  # task no longer running
    with pytest.raises(StateConflict, match="already gated"):
        park(db, task="sibling", approval="appr")
    assert db.execute("SELECT COUNT(*) FROM task_approval_gates").fetchone() == (1,)
    assert task(db, "sibling") == ("running", 3)


def test_gate_is_immutable_and_validated_at_sql_level(db):
    park(db)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE task_approval_gates SET approval_id='x'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM task_approval_gates")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("INSERT OR REPLACE INTO task_approval_gates VALUES('gated','appr','run','now',NULL)")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("INSERT OR REPLACE INTO task_approval_gates VALUES('sibling','appr','run','now',NULL)")
    _request(db, "appr2")
    with pytest.raises(sqlite3.IntegrityError, match="invalid task approval gate"):
        db.execute("INSERT INTO task_approval_gates VALUES('ghost','appr2','run','now',NULL)")
    db.execute("UPDATE tasks SET state='ready' WHERE id='other'")
    with pytest.raises(sqlite3.IntegrityError, match="invalid task approval gate"):
        db.execute("INSERT INTO task_approval_gates VALUES('other','appr2','run','now',NULL)")


def test_outbox_failure_rolls_back_park(db):
    db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox WHEN (SELECT COUNT(*) FROM outbox)>=1 BEGIN SELECT RAISE(ABORT,'forced'); END")
    before = counts(db)
    with pytest.raises(sqlite3.IntegrityError, match="forced"):
        park(db)
    assert task(db) == ("running", 3)
    assert db.execute("SELECT COUNT(*) FROM task_approval_gates").fetchone() == (0,)
    assert counts(db) == before


def test_second_event_failure_rolls_back_everything(db):
    db.execute("CREATE TRIGGER fail_gated BEFORE INSERT ON events WHEN NEW.type='task.approval_gated' BEGIN SELECT RAISE(ABORT,'forced'); END")
    with pytest.raises(sqlite3.IntegrityError):
        park(db)
    assert task(db) == ("running", 3)
    assert db.execute("SELECT COUNT(*) FROM task_approval_gates").fetchone() == (0,)
    assert not db.in_transaction


def test_idempotent_wrapper_replays_without_duplicate_effects(db):
    def go(key):
        return execute_idempotent(db, principal="daemon", operation="park", key=key,
                                  request={"task": "gated"},
                                  execute=lambda c: write_task_approval_gate(c, task_id="gated", approval_id="appr", expected_task_version=3, principal="daemon"))
    assert go("k").response == 4
    assert go("k").replayed
    assert counts(db) == (3, 3)
    assert db.execute("SELECT COUNT(*) FROM task_approval_gates").fetchone() == (1,)


def test_park_requires_command_transaction(db):
    with pytest.raises(RuntimeError):
        write_task_approval_gate(db, task_id="gated", approval_id="appr", expected_task_version=3, principal="daemon")


def test_concurrent_park_one_winner(tmp_path):
    path = tmp_path / "race.db"
    with closing(open_database(path)) as conn:
        apply_migrations(conn, ROOT / "migrations")
        _base(conn)
        _request(conn)
        _request(conn, "appr2")
    barrier = Barrier(2)

    def attempt(approval):
        with closing(open_database(path)) as conn:
            barrier.wait()
            try:
                return park(conn, approval=approval)
            except StateConflict:
                return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, ["appr", "appr2"]))
    assert sorted(r for r in results if r is not None) == [4]
    with closing(open_database(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_approval_gates").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type='task.approval_gated'").fetchone() == (1,)


# --- resolution -------------------------------------------------------------

@pytest.mark.parametrize("decision,expire", [
    (ApprovalState.REJECTED, False), (ApprovalState.REVOKED, False), (ApprovalState.EXPIRED, True)])
def test_denied_approval_fails_task_without_retry(db, decision, expire):
    park(db)
    with pytest.raises(StateConflict, match="pending"):
        resolve(db)
    _decide(db, decision, expire=expire)
    before = counts(db)
    assert resolve(db) == State.FAILED
    assert task(db) == ("failed", 5)
    assert counts(db) == (before[0] + 2, before[1] + 2)
    payload = json.loads(db.execute("SELECT payload_json FROM events WHERE type='task.approval_resolved'").fetchone()[0])
    assert payload["outcome"] == decision.value and payload["after"] == "failed"
    # Terminal: no retry transition, no re-resolution, siblings untouched.
    with pytest.raises(InvalidTransition):
        transition_task(db, StateChange("gated", State.FAILED, State.READY, 5), principal="daemon")
    with pytest.raises(StateConflict):
        resolve(db, version=5)
    assert task(db, "sibling") == ("running", 3)
    assert db.execute("SELECT state FROM runs").fetchone() == ("running",)


def test_approved_without_evidence_refused(db):
    park(db)
    _decide(db, ApprovalState.APPROVED)
    before = counts(db)
    for kwargs in ({}, {"binding_recheck_passed": False}, {"binding_recheck_passed": None}, {"binding_recheck_passed": "yes"}, {"binding_recheck_passed": 1}):
        with pytest.raises(BindingRecheckRequired):
            resolve(db, **kwargs)
    assert task(db) == ("awaiting_approval", 4)
    assert counts(db) == before


def test_approved_with_evidence_resumes_running_only(db):
    park(db)
    _decide(db, ApprovalState.APPROVED)
    assert resolve(db, binding_recheck_passed=True) == State.RUNNING
    assert task(db) == ("running", 5)
    payload = json.loads(db.execute("SELECT payload_json FROM events WHERE type='task.approval_resolved'").fetchone()[0])
    assert payload["outcome"] == "approved"
    # No completion shortcut from AWAITING_APPROVAL for a gated task's replay.
    with pytest.raises(StateConflict):
        resolve(db, version=5, binding_recheck_passed=True)


def test_approved_but_lapsed_fails_closed(db):
    park(db)
    _decide(db, ApprovalState.APPROVED)
    db.execute("DROP TRIGGER approval_binding_immutable")
    db.execute("UPDATE approval_requests SET expires_at=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
    assert resolve(db, binding_recheck_passed=True) == State.FAILED


def test_resolve_stale_version_and_ungated_task(db):
    park(db)
    _decide(db, ApprovalState.REJECTED)
    with pytest.raises(StateConflict):
        resolve(db, version=3)
    with pytest.raises(StateConflict):
        resolve(db, task="sibling", version=3)
    assert task(db) == ("awaiting_approval", 4)


def test_resolution_rolls_back_on_failure(db):
    park(db)
    _decide(db, ApprovalState.REJECTED)
    before = counts(db)
    db.execute("CREATE TRIGGER fail_res BEFORE INSERT ON events WHEN NEW.type='task.approval_resolved' BEGIN SELECT RAISE(ABORT,'forced'); END")
    with pytest.raises(sqlite3.IntegrityError):
        resolve(db)
    assert task(db) == ("awaiting_approval", 4)
    assert counts(db) == before


def test_concurrent_resolve_one_winner(tmp_path):
    path = tmp_path / "race2.db"
    with closing(open_database(path)) as conn:
        apply_migrations(conn, ROOT / "migrations")
        _base(conn)
        _request(conn)
        park(conn)
        _decide(conn, ApprovalState.REJECTED)
    barrier = Barrier(4)

    def attempt(_):
        with closing(open_database(path)) as conn:
            barrier.wait()
            try:
                return resolve(conn)
            except StateConflict:
                return None

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert results.count(State.FAILED) == 1 and results.count(None) == 3
    with closing(open_database(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type='task.approval_resolved'").fetchone() == (1,)


# --- other components stay conservative -------------------------------------

def test_claim_and_reservation_refuse_parked_task(db):
    park(db)
    db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StateConflict):
            claim_task(db, task_id="gated", expected_version=4, profile_id="mock", principal="daemon")
        with pytest.raises(StateConflict):
            reserve_attempt(db, task_id="gated", attempt_id="new", expected_task_version=4, principal="daemon")
        with pytest.raises(ValueError):
            write_task_transition(db, StateChange("gated", State.AWAITING_APPROVAL, State.RUNNING, 4), principal="daemon")
    finally:
        db.execute("ROLLBACK")
    assert task(db) == ("awaiting_approval", 4)


def test_parked_task_holds_no_worker_slot(db):
    park(db)
    db.execute("UPDATE tasks SET state='ready' WHERE id='other'")
    db.execute("UPDATE profiles SET max_concurrency=1")
    db.execute("UPDATE tasks SET state='completed' WHERE id='sibling'")
    db.execute("BEGIN IMMEDIATE")
    try:
        claim_task(db, task_id="other", expected_version=3, profile_id="mock", principal="daemon")
    finally:
        db.execute("ROLLBACK")


def test_engine_neither_launches_nor_completes_parked_task(db, tmp_path):
    db.execute("DELETE FROM task_attempts")
    db.execute("UPDATE tasks SET state='completed' WHERE id IN ('sibling','other')")
    park(db)
    db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES('free','run','free','created','now','now')")
    provider = MockProvider({"ok": MockScenario(("ok",), MockOutcome.SUCCEEDED)})

    async def verifier(task_id, candidate):
        return True
    engine = MockWorkflowEngine(db, provider, ArtifactBlobStore(tmp_path, max_bytes=100), verifier, verifier_id="reviewer")
    result = asyncio.run(engine.run("run", "mock", {"free": ("ok",), "gated": ("ok",), "sibling": ("ok",), "other": ("ok",)}))
    assert result == "blocked"
    assert task(db) == ("awaiting_approval", 4)
    assert task(db, "free")[0] == "completed"
    assert db.execute("SELECT COUNT(*) FROM task_attempts WHERE task_id='gated'").fetchone() == (0,)
    assert db.execute("SELECT state FROM runs").fetchone() == ("running",)


def test_restart_recovery_leaves_parked_task_parked(db):
    park(db)
    db.execute("UPDATE task_attempts SET state='running' WHERE id='att-sibling'")
    unknown, blocked = recover_mock_run(db, run_id="run", principal="daemon", no_workers_survive=True)
    assert unknown == ("att-sibling",)
    assert blocked == ("other", "sibling")
    assert task(db) == ("awaiting_approval", 4)
    assert db.execute("SELECT COUNT(*) FROM task_approval_gates").fetchone() == (1,)
    # Idempotent second recovery changes nothing.
    assert recover_mock_run(db, run_id="run", principal="daemon", no_workers_survive=True) == ((), ())
    assert task(db) == ("awaiting_approval", 4)
    # Approval decision after restart still resolves normally.
    _decide(db, ApprovalState.REJECTED)
    assert resolve(db) == State.FAILED


def test_recovery_refuses_parked_task_with_unresolved_attempt(db):
    park(db)
    db.execute("UPDATE task_attempts SET state='running' WHERE id='att-gated'")
    with pytest.raises(StateConflict, match="inconsistent"):
        recover_mock_run(db, run_id="run", principal="daemon", no_workers_survive=True)
    assert task(db) == ("awaiting_approval", 4)
    assert db.execute("SELECT state FROM task_attempts WHERE id='att-gated'").fetchone() == ("running",)


def test_restart_recovery_does_not_resolve_even_decided_gate(db):
    park(db)
    _decide(db, ApprovalState.APPROVED)
    recover_mock_run(db, run_id="run", principal="daemon", no_workers_survive=True)
    assert task(db) == ("awaiting_approval", 4)


# --- review fixes -----------------------------------------------------------

def _lapse(db, approval_id="appr", value=None):
    db.execute("DROP TRIGGER approval_binding_immutable")
    db.execute("UPDATE approval_requests SET expires_at=? WHERE id=?",
               (value or (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), approval_id))


def test_resume_refused_in_non_running_run_but_fail_allowed(db):
    park(db)
    _decide(db, ApprovalState.APPROVED)
    db.execute("UPDATE runs SET state='paused'")
    with pytest.raises(StateConflict, match="not running"):
        resolve(db, binding_recheck_passed=True)
    assert task(db) == ("awaiting_approval", 4)
    _lapse(db)
    assert resolve(db, binding_recheck_passed=True) == State.FAILED  # fail closed, paused run


def test_denied_gate_fails_in_paused_run_but_terminal_run_refused(db):
    park(db)
    _decide(db, ApprovalState.REJECTED)
    db.execute("UPDATE runs SET state='cancelled'")
    with pytest.raises(StateConflict, match="terminal"):
        resolve(db)
    db.execute("UPDATE runs SET state='paused'")
    assert resolve(db) == State.FAILED


def test_consumed_gate_cannot_be_re_resolved(db):
    park(db)
    assert db.execute("SELECT resolved_at FROM task_approval_gates").fetchone() == (None,)
    _decide(db, ApprovalState.APPROVED)
    resolve(db, binding_recheck_passed=True)
    assert db.execute("SELECT resolved_at FROM task_approval_gates").fetchone()[0] is not None
    # The task later reaches the post-verification approval wait.
    db.execute("UPDATE tasks SET state='verifying' WHERE id='gated'")
    db.execute("UPDATE tasks SET state='awaiting_approval' WHERE id='gated'")
    version = task(db)[1]
    before = counts(db)
    with pytest.raises(StateConflict, match="already resolved"):
        resolve(db, version=version, binding_recheck_passed=True)
    assert task(db) == ("awaiting_approval", version)
    assert counts(db) == before
    # Raw SQL bypass attempts are blocked by the triggers too.
    with pytest.raises(sqlite3.IntegrityError, match="unresolved"):
        db.execute("UPDATE tasks SET state='running' WHERE id='gated'")
    with pytest.raises(sqlite3.IntegrityError, match="unresolved"):
        db.execute("UPDATE tasks SET state='failed' WHERE id='gated'")
    # The post-verification path is unaffected by the old gate.
    assert transition_task(db, StateChange("gated", State.AWAITING_APPROVAL, State.CANCELLED, version), principal="daemon") == version + 1


def test_gate_update_only_allows_one_way_consumption(db):
    park(db)
    for sql in ("UPDATE task_approval_gates SET approval_id='x'", "UPDATE task_approval_gates SET task_id='x'",
                "UPDATE task_approval_gates SET run_id='x'", "UPDATE task_approval_gates SET created_at='x'",
                "UPDATE task_approval_gates SET resolved_at=''", "UPDATE task_approval_gates SET resolved_at=NULL"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(sql)
    db.execute("UPDATE task_approval_gates SET resolved_at='t'")
    _request(db, 'appr2')
    for sql in ("UPDATE task_approval_gates SET resolved_at=NULL", "UPDATE task_approval_gates SET resolved_at='u'"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(sql)
    with pytest.raises(sqlite3.IntegrityError, match="invalid task approval gate"):
        db.execute("INSERT INTO task_approval_gates VALUES('sibling','appr2','run','now','t')")


def test_trigger_requires_expiry_for_failing_approved_or_requested(db):
    park(db)
    with pytest.raises(sqlite3.IntegrityError, match="decided gate"):
        db.execute("UPDATE tasks SET state='failed' WHERE id='gated'")  # requested, unexpired
    _decide(db, ApprovalState.APPROVED)
    with pytest.raises(sqlite3.IntegrityError, match="decided gate"):
        db.execute("UPDATE tasks SET state='failed' WHERE id='gated'")  # approved, unexpired
    _lapse(db)
    db.execute("UPDATE tasks SET state='failed' WHERE id='gated'")  # approved but expired


def test_trigger_blocks_resume_of_lapsed_approved_gate(db):
    park(db)
    _decide(db, ApprovalState.APPROVED)
    _lapse(db)
    with pytest.raises(sqlite3.IntegrityError, match="approved gate"):
        db.execute("UPDATE tasks SET state='running' WHERE id='gated'")


@pytest.mark.parametrize("value", ["2030-01-01T00:00:00", "not-a-date"])
def test_naive_or_invalid_expiry_is_a_conflict_not_typeerror(db, value):
    park(db)
    _lapse(db, value=value)
    with pytest.raises(StateConflict, match="expiry"):
        resolve(db)
    assert task(db) == ("awaiting_approval", 4)


def test_naive_expiry_refused_at_park(db):
    _lapse(db, value="2030-01-01T00:00:00")
    with pytest.raises(StateConflict, match="expiry"):
        park(db)
    assert task(db) == ("running", 3)


def test_lapsed_requested_approval_fails_task(db):
    park(db)
    with pytest.raises(StateConflict, match="pending"):
        resolve(db)
    _lapse(db)
    assert resolve(db) == State.FAILED
    assert task(db) == ("failed", 5)
    assert db.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)
    payload = json.loads(db.execute("SELECT payload_json FROM events WHERE type='task.approval_resolved'").fetchone()[0])
    assert payload["outcome"] == "requested_lapsed"


def test_gate_refuses_a_forged_bookkeeping_attempt_id(db):
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('att-gated2','gated',2,'mock','completed')")
    db.execute("UPDATE tasks SET attempt_count=2 WHERE id='gated'")

    def attempt_park(attempt_id):
        db.execute("BEGIN IMMEDIATE")
        try:
            write_task_approval_gate(db, task_id="gated", approval_id="appr", expected_task_version=3,
                                     principal="daemon", bookkeeping_attempt_id=attempt_id)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    for forged in ("att-other", "att-gated", "missing"):     # another task's, an earlier real one, nonexistent
        with pytest.raises(StateConflict, match="Bookkeeping attempt"):
            attempt_park(forged)
    db.execute("INSERT INTO usage_observations(id,attempt_id,profile_id,source,input_tokens,output_tokens,confidence,observed_at) "
               "VALUES('u','att-gated2','mock','t',1,1,'reported','now')")
    with pytest.raises(StateConflict, match="Bookkeeping attempt"):      # a provider reported usage: not a wait attempt
        attempt_park("att-gated2")
    assert db.execute("SELECT COUNT(*) FROM task_approval_gates").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM events WHERE type='task.approval_gated'").fetchone() == (0,)


def test_gate_accepts_the_genuine_bookkeeping_attempt_id(db):
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('att-gated2','gated',2,'mock','completed')")
    db.execute("UPDATE tasks SET attempt_count=2 WHERE id='gated'")
    park_task_for_approval(db, task_id="gated", approval_id="appr", expected_task_version=3, principal="daemon",
                           bookkeeping_attempt_id="att-gated2")
    payload = json.loads(db.execute("SELECT payload_json FROM events WHERE type='task.approval_gated'").fetchone()[0])
    assert payload["bookkeeping_attempt_id"] == "att-gated2"
