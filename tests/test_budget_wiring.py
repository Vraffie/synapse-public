"""ADR 020 slice: policy loading, migration 0016 binding table, and the claim-time budget check."""
from contextlib import closing
from pathlib import Path
import json
import os

import pytest

from synapse.application.budget_policy import BudgetPolicy
from synapse.persistence.budget_binding import (
    BudgetDenied, BudgetPolicyError, bind_run, bound_policy, check_claim_budget, load_budget_policy)
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import StateConflict
from synapse.persistence.task_claim import claim_task

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path):
    with closing(open_database(tmp_path / "b.db")) as conn:
        apply_migrations(conn, ROOT / "migrations")
        conn.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        conn.execute("INSERT INTO runs VALUES('run','wf','1','running',0,'now','now')")
        conn.execute("INSERT INTO profiles(id,provider,display_name,config_ref,max_concurrency,created_at) VALUES('profile','mock','Mock','mock',4,'now')")
        for task in ("one", "two", "three"):
            conn.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES(?,'run',?,'ready','now','now')", (task, task))
        yield conn


def policy(**kw):
    return BudgetPolicy.from_dict({"version": "p1", **kw})


def attempt(db, aid, task, state, **usage):
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES(?,?,?, 'profile',?)", (aid, task, 1, state))
    if usage:
        db.execute("INSERT INTO usage_observations(id,attempt_id,profile_id,source,input_tokens,output_tokens,confidence,observed_at) "
                   "VALUES(?,?,'profile','t',?,?,'reported','now')", ("u" + aid, aid, usage["i"], usage["o"]))


def claim(db, task="one"):
    db.execute("BEGIN IMMEDIATE")
    try:
        v = claim_task(db, task_id=task, expected_version=0, profile_id="profile", principal="daemon",
                       budget_check=check_claim_budget)
        db.execute("COMMIT")
        return v
    except BaseException:
        db.execute("ROLLBACK")
        raise


def bind(db, **kw):
    db.execute("BEGIN IMMEDIATE")
    bind_run(db, "run", policy(**kw))
    db.execute("COMMIT")


def test_unbound_run_is_unbudgeted(db):
    attempt(db, "a0", "two", "completed")
    assert claim(db) == 1


def test_attempt_cap_denies_with_budget_exceeded_and_leaves_no_residue(db):
    bind(db, per_run={"max_attempts": 1})
    attempt(db, "a1", "two", "completed")
    with pytest.raises(BudgetDenied) as exc:
        claim(db)
    assert exc.value.reason == "budget_exceeded"
    assert "max_attempts limit 1 observed 2" in str(exc.value)
    assert db.execute("SELECT state FROM tasks WHERE id='one'").fetchone() == ("ready",)
    assert db.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)


def test_in_flight_attempt_counts_as_reservation(db):
    bind(db, per_run={"max_attempts": 2})
    attempt(db, "a1", "two", "running")
    assert claim(db) == 1        # 1 in flight + this one = 2 <= cap
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('a2','one',1,'profile','running')")  # the engine reserves the attempt in the same transaction as the claim
    with pytest.raises(BudgetDenied):
        claim(db, "three")


def test_missing_usage_is_unknown_never_zero(db):
    bind(db, per_run={"max_total_tokens": 1000})
    attempt(db, "a1", "two", "completed")           # terminal, reported nothing
    with pytest.raises(BudgetDenied) as exc:
        claim(db)
    assert exc.value.reason == "usage_unknown"


def test_unknown_allowed_only_when_policy_says_so(db):
    bind(db, per_run={"max_total_tokens": 1000}, on_unknown="allow")
    attempt(db, "a1", "two", "completed")
    assert claim(db) == 1


def test_token_cap_breach_and_within(db):
    bind(db, per_run={"max_total_tokens": 100})
    attempt(db, "a1", "two", "completed", i=60, o=30)
    assert claim(db) == 1                                   # 90 <= 100
    db.execute("UPDATE tasks SET state='ready',state_version=0 WHERE id='one'")
    attempt(db, "a2", "three", "failed", i=10, o=10)        # now 110
    with pytest.raises(BudgetDenied) as exc:
        claim(db)
    assert exc.value.reason == "budget_exceeded"


def test_token_capped_scope_is_serial(db):
    bind(db, per_run={"max_total_tokens": 10**6})
    attempt(db, "a1", "two", "running")
    with pytest.raises(StateConflict, match="unresolved"):
        claim(db)       # retry later: not a budget denial


def test_per_profile_cap_applies(db):
    bind(db, per_profile={"profile": {"max_attempts": 1}})
    attempt(db, "a1", "two", "completed")
    with pytest.raises(BudgetDenied) as exc:
        claim(db)
    assert exc.value.decision.scope.kind == "profile"


def test_capacity_is_checked_before_budget(db):
    bind(db, per_run={"max_attempts": 1})
    attempt(db, "a1", "two", "completed")
    db.execute("UPDATE profiles SET max_concurrency=1")
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('a2','three',1,'profile','running')")
    with pytest.raises(StateConflict, match="capacity"):
        claim(db)


def test_binding_is_immutable_and_roundtrips(db):
    bind(db, per_run={"max_attempts": 3})
    assert bound_policy(db, "run").per_run.max_attempts == 3
    with pytest.raises(Exception, match="append-only"):
        db.execute("UPDATE run_budget_bindings SET policy_version='x'")
    with pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM run_budget_bindings")
    row = db.execute("SELECT policy_sha256 FROM run_budget_bindings").fetchone()
    assert len(row[0]) == 64


def write(tmp_path, text, mode=0o600):
    path = tmp_path / "policy.json"
    path.write_text(text)
    os.chmod(path, mode)
    return path


def test_loader_accepts_valid_file(tmp_path):
    assert load_budget_policy(write(tmp_path, json.dumps({"version": "v", "per_run": {"max_attempts": 2}}))).per_run.max_attempts == 2


@pytest.mark.parametrize("text", [
    "{", '{"version":"v","bogus":1}', '{"version":"v","per_run":{"max_attemptz":1}}',
    '{"version":"v","per_run":{"max_attempts":NaN}}', '{"version":"v","version":"w"}',
    '{"version":"v","per_run":{"max_cost_usd":1}}', '{"version":"v","per_task":{"max_wall_seconds":10}}',
    '{"version":"v","per_profile":{"typo":{"max_attempts":1}}}', '{"version":"v","per_run":{"max_attempts":true}}'])
def test_loader_refuses_bad_policy(tmp_path, text):
    with pytest.raises(BudgetPolicyError):
        load_budget_policy(write(tmp_path, text), profile_ids=frozenset({"real"}))


def test_loader_refuses_unsafe_files(tmp_path):
    with pytest.raises(BudgetPolicyError, match="writable"):
        load_budget_policy(write(tmp_path, '{"version":"v"}', 0o666))
    link = tmp_path / "link.json"
    link.symlink_to(write(tmp_path, '{"version":"v"}'))
    with pytest.raises(BudgetPolicyError):
        load_budget_policy(link)
    with pytest.raises(BudgetPolicyError):
        load_budget_policy(tmp_path / "missing.json")


def test_engine_blocks_denied_task_with_event_and_audit(tmp_path):
    import asyncio
    from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
    from synapse.orchestration.mock_engine import MockWorkflowEngine
    from synapse.providers.mock import MockOutcome, MockProvider, MockScenario
    with closing(open_database(tmp_path / "e.db")) as conn:
        apply_migrations(conn, ROOT / "migrations")
        conn.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        conn.execute("INSERT INTO runs VALUES('run','wf','1','running',0,'now','now')")
        conn.execute("INSERT INTO profiles(id,provider,display_name,config_ref,max_concurrency,created_at) VALUES('mock','mock','Mock','mock',6,'now')")
        for t in ("a", "b"):
            conn.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES(?,'run',?,'created','now','now')", (t, t))
        conn.execute("INSERT INTO task_dependencies VALUES('b','a')")
        conn.execute("BEGIN IMMEDIATE")
        bind_run(conn, "run", policy(per_run={"max_attempts": 1}))
        conn.execute("COMMIT")

        async def verifier(task, candidate):
            return True
        engine = MockWorkflowEngine(conn, MockProvider({"ok": MockScenario(("ok",), MockOutcome.SUCCEEDED)}),
                                    ArtifactBlobStore(tmp_path, max_bytes=100), verifier, verifier_id="reviewer")
        engine._budget_check = check_claim_budget
        assert asyncio.run(engine.run("run", "mock", {"a": ("ok",), "b": ("ok",)})) == "blocked"
        assert conn.execute("SELECT id,state FROM tasks ORDER BY id").fetchall() == [("a", "completed"), ("b", "blocked")]
        assert conn.execute("SELECT COUNT(*) FROM task_attempts").fetchone() == (1,)   # nothing was spawned for b
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type='budget.exceeded' AND aggregate_id='b'").fetchone() == (1,)
        action, decision, details = conn.execute("SELECT action,decision,details_json FROM audit_entries WHERE action='budget.denied'").fetchone()
        assert decision == "denied" and json.loads(details)["reason"] == "budget_exceeded"


def test_daemon_refuses_to_start_on_malformed_policy(tmp_path, capsys):
    from synapse.daemon.__main__ import main
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    with pytest.raises(SystemExit) as exc:
        main(["--state-dir", str(state), "--budget-policy", str(write(tmp_path, '{"version":"v","per_run":{"nope":1}}'))])
    assert exc.value.code == 2
    assert "invalid budget policy" in capsys.readouterr().err
    assert not (state / "synapse.sqlite").exists()   # failed before touching state


# --- approval-gated steps (review F1/F2): the budget is enforced at park and at resume ---------------------------

class GatedEnv:
    """Reuses the gated-workflow harness: tasks 'free' (ungated) and 'gated' (approval step)."""

    def __init__(self, tmp_path):
        from tests.test_workflow_approval_gates import Env
        self.env = Env(tmp_path)

    def run(self, budgeted=True):
        from tests.test_workflow_approval_gates import SPEC
        engine = self.env.engine()
        if budgeted:
            engine._budget_check = check_claim_budget
        return self.env.run(engine, SPEC)

    def bind(self, **kw):
        self.env.db.execute("BEGIN IMMEDIATE")
        bind_run(self.env.db, "run", policy(**kw))
        self.env.db.execute("COMMIT")

    def gated_attempts(self):
        return self.env.db.execute("SELECT attempt_no,state FROM task_attempts WHERE task_id='gated' ORDER BY attempt_no").fetchall()

    def denial(self):
        rows = self.env.db.execute("SELECT payload_json FROM events WHERE aggregate_id='gated' AND type='budget.exceeded'").fetchall()
        return [json.loads(r[0])["reason"] for r in rows]


@pytest.fixture
def genv(tmp_path):
    g = GatedEnv(tmp_path)
    yield g
    g.env.close()


LIMITS = [({"max_attempts": 1}, "budget_exceeded"), ({"max_total_tokens": 1000}, "usage_unknown")]


@pytest.mark.parametrize("limits,reason", LIMITS)
def test_gated_step_is_blocked_at_park(genv, limits, reason):
    genv.bind(per_run=limits)
    assert genv.run() == "blocked"
    env = genv.env
    assert env.task()[0] == "blocked"
    assert genv.gated_attempts() == []                      # no bookkeeping attempt, nothing started
    assert env.db.execute("SELECT COUNT(*) FROM task_approval_gates").fetchone() == (0,)
    assert genv.denial() == [reason]
    assert env.db.execute("SELECT COUNT(*) FROM audit_entries WHERE action='budget.denied'").fetchone() == (1,)
    assert env.db.execute("SELECT COUNT(*) FROM artifacts WHERE provenance_json LIKE '%gated%'").fetchone() == (0,)


@pytest.mark.parametrize("limits,reason", LIMITS)
def test_approved_gated_step_is_blocked_at_resume_and_never_runs(genv, limits, reason):
    env = genv.env
    assert genv.run(budgeted=False) == "blocked"            # parked while unbudgeted
    assert env.task()[0] == "awaiting_approval"
    genv.bind(per_run=limits)                                     # the cap is breached / usage unknown by approval time
    env.decide()
    assert genv.run() == "blocked"
    assert env.task()[0] == "blocked"
    assert genv.gated_attempts() == [(1, "completed")]      # only the gate bookkeeping attempt: no real attempt reserved
    assert env.task()[2] == 1
    assert genv.denial() == [reason]
    assert env.db.execute("SELECT COUNT(*) FROM audit_entries WHERE action='approval.executed'").fetchone() == (0,)
    assert env.db.execute("SELECT COUNT(*) FROM artifacts WHERE provenance_json LIKE '%gated%'").fetchone() == (0,)
    assert env.db.execute("SELECT resolved_at IS NOT NULL FROM task_approval_gates").fetchone() == (1,)
    assert genv.run() == "blocked" and genv.gated_attempts() == [(1, "completed")]   # no retry


def test_approved_gated_step_runs_when_budget_allows(genv):
    env = genv.env
    assert genv.run(budgeted=False) == "blocked"
    genv.bind(per_run={"max_attempts": 2})   # free(1) + real gated attempt(1); bookkeeping attempt not counted (F2)
    env.decide()
    assert genv.run() == "succeeded"
    assert genv.gated_attempts() == [(1, "completed"), (2, "completed")]


def _seed_real_attempt_one(genv, tokens=None):
    """The gated task already ran a REAL attempt 1 (ungated) and awaits retry; a later run() adds the gate."""
    db = genv.env.db
    db.execute("UPDATE tasks SET state='retry_wait',attempt_count=1 WHERE id='gated'")
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('real1','gated',1,'mock','failed')")
    if tokens is not None:
        db.execute("INSERT INTO usage_observations(id,attempt_id,profile_id,source,input_tokens,output_tokens,confidence,observed_at) "
                   "VALUES('ureal1','real1','mock','t',?,0,'reported','now')", (tokens,))


def test_real_attempt_before_the_gate_still_counts_breached_cap(genv):
    env = genv.env
    _seed_real_attempt_one(genv, tokens=900)
    assert genv.run(budgeted=False) == "blocked"            # parks: bookkeeping is attempt 2
    assert env.task()[0] == "awaiting_approval"
    assert genv.gated_attempts()[0] == (1, "failed")
    genv.bind(per_task={"max_total_tokens": 500})           # the real 900 tokens breach the per-task cap
    env.decide()
    assert genv.run() == "blocked"
    assert env.task()[0] == "blocked" and genv.denial() == ["budget_exceeded"]
    assert genv.gated_attempts() == [(1, "failed"), (2, "completed")]   # no real attempt 3


def test_real_attempt_before_the_gate_still_counts_unknown_usage(genv):
    env = genv.env
    _seed_real_attempt_one(genv)                            # real attempt 1 reported nothing
    assert genv.run(budgeted=False) == "blocked"
    genv.bind(per_task={"max_total_tokens": 1000})
    env.decide()
    assert genv.run() == "blocked"
    assert env.task()[0] == "blocked" and genv.denial() == ["usage_unknown"]
    assert genv.gated_attempts() == [(1, "failed"), (2, "completed")]


def test_genuine_bookkeeping_attempt_is_still_excluded_under_token_cap(genv):
    env = genv.env
    assert genv.run(budgeted=False) == "blocked"            # gated task has only its wait attempt, no usage possible
    genv.bind(per_task={"max_total_tokens": 1000})
    env.decide()
    assert genv.run() == "succeeded"                        # not unknown usage: the wait attempt is skipped by id
    assert genv.gated_attempts() == [(1, "completed"), (2, "completed")]


def test_serial_token_cap_at_resume_leaves_task_parked(genv):
    env = genv.env
    assert genv.run(budgeted=False) == "blocked"
    genv.bind(per_run={"max_total_tokens": 10**6})
    env.db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('busy','free',2,'mock','running')")
    env.decide()
    assert genv.run() == "blocked"
    assert env.task()[0] == "awaiting_approval"             # retry later: not a denial, not resolved
    assert env.db.execute("SELECT resolved_at IS NULL FROM task_approval_gates").fetchone() == (1,)
    assert genv.gated_attempts() == [(1, "completed")] and genv.denial() == []


def test_unblocked_task_after_resume_denial_is_rebudgeted(genv):
    from synapse.domain.workflow import State, StateChange
    from synapse.persistence.state_store import write_task_transition
    env = genv.env
    assert genv.run(budgeted=False) == "blocked"
    genv.bind(per_run={"max_attempts": 1})
    env.decide()
    assert genv.run() == "blocked" and env.task()[0] == "blocked"
    types = [r[0] for r in env.db.execute("SELECT type FROM events WHERE aggregate_id='gated' ORDER BY rowid")]
    assert types.index("task.approval_resolved") < types.index("budget.exceeded")   # approved->running, then blocked
    version = env.task()[1]
    env.db.execute("BEGIN IMMEDIATE")
    write_task_transition(env.db, StateChange("gated", State.BLOCKED, State.READY, version), principal="operator")
    env.db.execute("COMMIT")
    assert genv.run() == "blocked"                          # the operator unblock does not bypass the budget
    assert env.task()[0] == "blocked" and genv.denial() == ["budget_exceeded", "budget_exceeded"]
    assert genv.gated_attempts() == [(1, "completed")]
    assert env.db.execute("SELECT COUNT(*) FROM audit_entries WHERE action='approval.executed'").fetchone() == (0,)


@pytest.mark.parametrize("limits,reason", [({"per_run": {"max_attempts": 2}}, "budget_exceeded"),
                                           ({"per_task": {"max_total_tokens": 1000}}, "usage_unknown")])
def test_gate_event_without_attempt_id_fails_closed(genv, monkeypatch, limits, reason):
    """An older-database gate (no bookkeeping_attempt_id in its event) leaves the wait attempt counted."""
    import synapse.orchestration.approval_gates as gates
    original = gates.write_task_approval_gate
    monkeypatch.setattr(gates, "write_task_approval_gate",
                        lambda db, **kw: original(db, **{k: v for k, v in kw.items() if k != "bookkeeping_attempt_id"}))
    env = genv.env
    assert genv.run(budgeted=False) == "blocked"
    payload = json.loads(env.db.execute("SELECT payload_json FROM events WHERE type='task.approval_gated'").fetchone()[0])
    assert "bookkeeping_attempt_id" not in payload
    genv.bind(**limits)     # max_attempts=2 would allow free(1)+real(1) only if the wait attempt were excluded
    env.decide()
    assert genv.run() == "blocked"
    assert env.task()[0] == "blocked" and genv.denial() == [reason]
    assert genv.gated_attempts() == [(1, "completed")]
