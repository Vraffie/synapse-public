import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3

import pytest

from synapse.application.approval_requests import DaemonApprovalRequester
from synapse.application.approval_service import ApprovalAccessDenied, ApprovalService
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.infrastructure.daemon_requester import DaemonRequesterAuthority
from synapse.infrastructure.local_approver import LocalApproverAuthority, provision_approver_token
from synapse.orchestration.approval_gates import ApprovalGateCoordinator, ApprovalStepSpec
from synapse.orchestration.mock_engine import MockWorkflowEngine
from synapse.persistence.approval_binding import ApprovalGrant
from synapse.persistence.gated_attempt import reserve_attempt_after_approval
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.mock_recovery import recover_mock_run
from synapse.persistence.state_store import StateConflict
from synapse.providers.mock import MockOutcome, MockProvider, MockScenario

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "a" * 64
SPEC = ApprovalStepSpec("push", "repo", DIGEST, "1")
SCENARIOS = {"free": ("ok",), "gated": ("ok",)}


class Env:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        directory = tmp_path / "state"
        directory.mkdir(mode=0o700)
        self.fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        self.token = provision_approver_token(self.fd)
        self.authority = LocalApproverAuthority(self.fd)
        self.db = open_database(tmp_path / "wf.db")
        apply_migrations(self.db, ROOT / "migrations")
        self.db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        self.db.execute("INSERT INTO runs VALUES('run','wf','1','running',0,'now','now')")
        self.db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,max_concurrency,created_at) VALUES('mock','mock','Mock','mock',6,'now')")
        for task in ("free", "gated"):
            self.db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES(?,'run',?,'created','now','now')", (task, task))
        (tmp_path / "blobs").mkdir(mode=0o700)
        (tmp_path / "b2").mkdir(mode=0o700)
        self.service = ApprovalService(self.db, self.authority)

    def engine(self, **kwargs):
        provider = MockProvider({"ok": MockScenario(("ok",), MockOutcome.SUCCEEDED)})

        async def verifier(task_id, candidate):
            return True
        return MockWorkflowEngine(self.db, provider, ArtifactBlobStore(self.tmp_path / "blobs", max_bytes=100), verifier,
                                  verifier_id="reviewer", approval_requester=DaemonApprovalRequester(self.db), **kwargs)

    def run(self, engine=None, spec=SPEC):
        return asyncio.run((engine or self.engine()).run("run", "mock", SCENARIOS, {"gated": spec}))

    def approval_id(self):
        return self.db.execute("SELECT id FROM approval_requests").fetchone()[0]

    def decide(self, decision="approved", token=None, **kw):
        params = dict(approval_id=self.approval_id(), expected_version=0, decision=decision, operation="push",
                      target="repo", candidate_sha256=DIGEST, policy_version="1",
                      idempotency_key="d-" + decision, correlation_id="trace")
        params.update(kw)
        return self.service.decide(token or self.token, **params)

    def task(self, name="gated"):
        return self.db.execute("SELECT state,state_version,attempt_count FROM tasks WHERE id=?", (name,)).fetchone()

    def counts(self):
        return tuple(self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in ("events", "outbox", "approval_requests", "task_attempts", "audit_entries"))

    def expire(self):
        self.db.execute("DROP TRIGGER approval_binding_immutable")
        self.db.execute("UPDATE approval_requests SET expires_at=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))

    def close(self):
        self.authority.close()
        os.close(self.fd)
        self.db.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_step_requests_approval_parks_then_resumes_with_new_attempt(env):
    assert env.run() == "blocked"
    assert env.task() == ("awaiting_approval", 5, 1)
    assert env.task("free")[0] == "completed"
    requested = env.db.execute("SELECT state,requester_subject,requester_kind,operation,target,candidate_manifest_sha256 FROM approval_requests").fetchall()
    assert requested == [("requested", "daemon:workflow", "agent", "push", "repo", DIGEST)]
    assert env.db.execute("SELECT approval_id,resolved_at FROM task_approval_gates WHERE task_id='gated'").fetchone() == (env.approval_id(), None)
    # The wait attempt is bookkeeping only; no provider output exists for the gated task.
    assert env.db.execute("SELECT COUNT(*) FROM artifacts WHERE provenance_json LIKE '%gated%'").fetchone() == (0,)
    env.decide()
    assert env.run() == "succeeded"
    assert env.task()[0] == "completed" and env.task()[2] == 2
    assert env.db.execute("SELECT attempt_no,state FROM task_attempts WHERE task_id='gated' ORDER BY attempt_no").fetchall() == [(1, "completed"), (2, "completed")]
    assert env.db.execute("SELECT resolved_at IS NOT NULL FROM task_approval_gates").fetchone() == (1,)
    assert env.db.execute("SELECT COUNT(*) FROM approval_requests").fetchone() == (1,)
    types = [r[0] for r in env.db.execute("SELECT type FROM events WHERE aggregate_id='gated' ORDER BY rowid")]
    assert "task.approval_gated" in types and "task.approval_resolved" in types
    assert types.index("task.approval_gated") < types.index("task.approval_resolved")


def test_pending_replay_is_idempotent(env):
    assert env.run() == "blocked"
    before = env.counts()
    for _ in range(3):
        assert env.run() == "blocked"
    assert env.counts() == before
    assert env.task() == ("awaiting_approval", 5, 1)


def test_completed_run_replay_adds_nothing(env):
    env.run()
    env.decide()
    assert env.run() == "succeeded"
    before = env.counts()
    with pytest.raises(StateConflict):  # terminal run: the engine refuses to run again
        env.run()
    assert env.counts() == before


def test_restart_between_park_and_decision_and_after_decision(env, tmp_path):
    env.run()
    env.db.close()
    # New process: new connection, new engine, new requester object.
    env.db = open_database(tmp_path / "wf.db")
    env.service = ApprovalService(env.db, env.authority)
    unknown, blocked = recover_mock_run(env.db, run_id="run", principal="daemon", no_workers_survive=True)
    assert (unknown, blocked) == ((), ())
    assert env.task() == ("awaiting_approval", 5, 1)
    assert env.run() == "blocked"
    env.decide()
    env.db.close()
    env.db = open_database(tmp_path / "wf.db")
    env.service = ApprovalService(env.db, env.authority)
    assert env.run() == "succeeded"
    assert env.task()[0] == "completed"


def test_rejected_approval_fails_task_and_run_without_execution(env):
    env.run()
    env.decide("rejected")
    assert env.run() == "failed"
    assert env.task() == ("failed", 6, 1)
    assert env.task("free")[0] == "completed"
    assert env.db.execute("SELECT COUNT(*) FROM task_attempts WHERE task_id='gated'").fetchone() == (1,)
    assert env.db.execute("SELECT resolved_at IS NOT NULL FROM task_approval_gates").fetchone() == (1,)


def test_expired_approval_fails_task(env):
    env.run()
    env.decide()
    env.expire()
    assert env.run() == "failed"
    assert env.task()[0] == "failed"
    assert env.task()[2] == 1


def test_lapsed_pending_approval_fails_task(env):
    env.run()
    env.expire()
    assert env.run() == "failed"
    assert env.task()[0] == "failed"


def test_revoked_approval_fails_task(env):
    env.run()
    env.decide()
    env.db.execute("UPDATE approval_requests SET state='revoked'")
    assert env.run() == "failed"
    assert env.task()[0] == "failed"


def test_f13_human_can_revoke_approved_approval_and_task_fails(env):
    env.run()
    env.decide()
    result = env.service.revoke(env.token, approval_id=env.approval_id(), expected_version=1,
                                idempotency_key="rv", correlation_id="trace", reason="changed my mind")
    assert result.response["state"] == "revoked"
    assert env.db.execute("SELECT state FROM approval_requests").fetchone() == ("revoked",)
    assert env.db.execute("SELECT COUNT(*) FROM audit_entries WHERE action='approval.revoke'").fetchone() == (1,)
    assert env.run() == "failed"
    assert env.db.execute("SELECT COUNT(*) FROM audit_entries WHERE action='approval.executed'").fetchone() == (0,)


def test_f13_use_of_approval_is_audited(env):
    env.run()
    env.decide()
    assert env.run() == "succeeded"
    rows = env.db.execute("SELECT resource,decision FROM audit_entries WHERE action='approval.executed'").fetchall()
    assert rows == [(env.approval_id(), "allowed")]


def test_binding_mismatch_fails_task_via_gate(env):
    env.run()
    env.decide()
    assert env.run(spec=ApprovalStepSpec("push", "repo", "b" * 64, "1")) == "failed"
    assert env.task() == ("failed", 6, 1)
    assert env.db.execute("SELECT reason FROM task_gate_binding_denials").fetchall() == [("binding_mismatch",)]
    assert env.db.execute("SELECT COUNT(*) FROM task_attempts WHERE task_id='gated'").fetchone() == (1,)
    assert env.db.execute("SELECT resolved_at IS NOT NULL FROM task_approval_gates").fetchone() == (1,)


@pytest.mark.parametrize("spec", [ApprovalStepSpec("push", "other-repo", DIGEST, "1"),
                                  ApprovalStepSpec("deploy", "repo", DIGEST, "1"),
                                  ApprovalStepSpec("push", "repo", DIGEST, "2")])
def test_each_binding_field_is_rechecked(env, spec):
    env.run()
    env.decide()
    assert env.run(spec=spec) == "failed"
    assert env.db.execute("SELECT COUNT(*) FROM task_gate_binding_denials").fetchone() == (1,)


def test_binding_denial_row_requires_parked_unresolved_gate(env):
    env.run()
    approval_id = env.approval_id()
    with pytest.raises(sqlite3.IntegrityError):
        env.db.execute("INSERT INTO task_gate_binding_denials VALUES('free',?,'binding_mismatch','now')", (approval_id,))
    # A denial cannot be written then used while the approval is merely pending and valid.
    env.db.execute("INSERT INTO task_gate_binding_denials VALUES('gated',?,'binding_mismatch','now')", (approval_id,))
    with pytest.raises(sqlite3.IntegrityError):
        env.db.execute("UPDATE task_gate_binding_denials SET reason='malformed'")
    with pytest.raises(sqlite3.IntegrityError):
        env.db.execute("DELETE FROM task_gate_binding_denials")


def test_fail_trigger_still_refuses_failure_without_denial(env):
    env.run()
    with pytest.raises(sqlite3.IntegrityError):
        env.db.execute("UPDATE tasks SET state='failed' WHERE id='gated'")


def test_requester_cannot_self_grant(env):
    env.run()
    requester = DaemonApprovalRequester(env.db)
    authority = requester._authority
    service = ApprovalService(env.db, authority)
    with pytest.raises(ApprovalAccessDenied):
        service.decide(authority.capability, approval_id=env.approval_id(), expected_version=0, decision="approved",
                       operation="push", target="repo", candidate_sha256=DIGEST, policy_version="1",
                       idempotency_key="x", correlation_id="t")
    assert not hasattr(ApprovalGateCoordinator, "decide")
    assert env.db.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)
    assert env.run() == "blocked"
    assert env.task()[0] == "awaiting_approval"


def test_token_never_persisted(env):
    env.run()
    env.decide()
    env.run()
    for table in ("events", "audit_entries", "outbox", "approval_requests", "idempotency_keys"):
        try:
            rows = env.db.execute(f"SELECT * FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue
        assert env.token not in repr(rows)


def test_existing_request_is_reused_after_crash_before_park(env):
    # Simulate a crash after the request committed but before the park.
    coordinator = ApprovalGateCoordinator(env.db, DaemonApprovalRequester(env.db))
    coordinator._requester.request_approval(
        run_id="run", operation="push", target="repo", candidate_sha256=DIGEST, policy_version="1",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        idempotency_key=coordinator.request_key("gated"), correlation_id="c")
    assert env.run() == "blocked"
    assert env.db.execute("SELECT COUNT(*) FROM approval_requests").fetchone() == (1,)
    assert env.task()[0] == "awaiting_approval"
    assert env.approval_id() == coordinator.approval_id("run", "gated")


def test_park_is_atomic_when_gate_insert_fails(env):
    # Expired-before-park request: park raises, nothing about the task changes.
    coordinator = ApprovalGateCoordinator(env.db, DaemonApprovalRequester(env.db))
    coordinator._requester.request_approval(
        run_id="run", operation="push", target="repo", candidate_sha256=DIGEST, policy_version="1",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        idempotency_key=coordinator.request_key("gated"), correlation_id="c")
    env.expire()
    env.db.execute("UPDATE tasks SET state='ready' WHERE id='gated'")
    before = env.counts()
    with pytest.raises(StateConflict):
        coordinator.park(run_id="run", task_id="gated", profile_id="mock", spec=SPEC, max_running=6)
    assert env.counts() == before
    assert env.task() == ("ready", 0, 0)


def test_reserve_after_approval_requires_grant_from_own_gate(env):
    env.run()
    env.decide()
    env.db.execute("BEGIN IMMEDIATE")
    with pytest.raises(TypeError):
        reserve_attempt_after_approval(env.db, task_id="gated", attempt_id="a", expected_task_version=4,
                                       principal="p", grant=object())
    with pytest.raises(TypeError):
        ApprovalGrant()
    env.db.execute("ROLLBACK")


def test_approval_steps_validation(env):
    engine = env.engine()
    plain = MockWorkflowEngine(env.db, MockProvider({"ok": MockScenario(("ok",), MockOutcome.SUCCEEDED)}),
                               ArtifactBlobStore(env.tmp_path / "b2", max_bytes=100), None, verifier_id="r")
    with pytest.raises(ValueError):
        asyncio.run(plain.run("run", "mock", SCENARIOS, {"gated": SPEC}))
    with pytest.raises(ValueError):
        asyncio.run(env.engine(max_attempts=1).run("run", "mock", SCENARIOS, {"gated": SPEC}))
    with pytest.raises(ValueError):
        asyncio.run(engine.run("run", "mock", SCENARIOS, {"nope": SPEC}))
    with pytest.raises(ValueError):
        ApprovalStepSpec("push", "repo", DIGEST, "1", ttl=timedelta(days=2))


def test_runs_without_approval_steps_are_unchanged(env):
    engine = env.engine()
    assert asyncio.run(engine.run("run", "mock", SCENARIOS)) == "succeeded"
    assert env.db.execute("SELECT COUNT(*) FROM approval_requests").fetchone() == (0,)


def _budgeted_engine(env, **policy):
    from synapse.application.budget_policy import BudgetPolicy
    from synapse.persistence.budget_binding import bind_run, check_claim_budget
    env.db.execute("BEGIN IMMEDIATE")
    bind_run(env.db, "run", BudgetPolicy.from_dict({"version": "p1", **policy}))
    env.db.execute("COMMIT")
    engine = env.engine()
    engine._budget_check = check_claim_budget
    return engine


def test_gate_park_attempt_is_exempt_from_token_cap_budget(env):
    # The park attempt makes no model call and reports no usage: it must not read as usage_unknown.
    # "other" is claimed after the gate parks; with a token cap the park attempt would otherwise read usage_unknown.
    env.db.execute("DELETE FROM tasks WHERE id='free'")
    env.db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES('other','run','other','created','now','now')")
    engine = _budgeted_engine(env, per_run={"max_total_tokens": 1000, "max_attempts": 2})
    scenarios = {"gated": ("ok",), "other": ("ok",)}
    assert asyncio.run(engine.run("run", "mock", scenarios, {"gated": SPEC})) == "blocked"
    assert env.task() == ("awaiting_approval", 5, 1)
    assert env.task("other")[0] == "completed"
    assert env.db.execute("SELECT COUNT(*) FROM events WHERE type='budget.exceeded'").fetchone() == (0,)
    assert env.db.execute("SELECT COUNT(*) FROM events WHERE type='budget.exceeded'").fetchone() == (0,)


def test_real_claim_is_still_budget_checked_beside_a_parked_gate(env):
    # "free" is a real attempt that reports no usage, so the real claim of "later" reads usage_unknown and is
    # blocked, even though the parked gate's bookkeeping attempt is exempt.
    env.db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES('later','run','later','created','now','now')")
    env.db.execute("INSERT INTO task_dependencies VALUES('later','free')")
    engine = _budgeted_engine(env, per_run={"max_total_tokens": 1000})
    scenarios = {**SCENARIOS, "later": ("ok",)}
    assert asyncio.run(engine.run("run", "mock", scenarios, {"gated": SPEC})) == "blocked"
    # Park is budget-checked too (a2d6170): "free" reported nothing, so the gated park and "later" both read usage_unknown.
    assert env.task("free")[0] == "completed"
    assert env.task("gated")[0] == "blocked" and env.task("later")[0] == "blocked"
    assert sorted(r[0] for r in env.db.execute("SELECT aggregate_id FROM events WHERE type='budget.exceeded'")) == ["gated", "later"]
