"""M5.2 wiring: the agent engine checks budgets before scheduling, a refusal fails the task with a distinct reason,
is never retried, strands its dependents, and the bound policy version is recorded with every spawn."""
import asyncio
import json
import sqlite3

import pytest
from pydantic import ValidationError

from synapse.api.agent_commands import AgentRunRequest
from synapse.application.budget_policy import BudgetPolicy
from synapse.daemon.agent_runtime import AgentRuntime
from synapse.domain.workflow import TRANSITIONS, State
from synapse.orchestration import agent_engine, budget_enforcement
from synapse.orchestration.agent_engine import AgentWorkflowEngine
from synapse.persistence import agent_tasks as at
from synapse.persistence.agent_tasks import AgentSpecError
from tests.test_agent_runtime import GATE, Env, finish

P = at.AGENT_PROFILE


def budgeted(env, **kw):
    """Operator config: the policy is passed to the runtime constructor (agentd --budget-policy), never the request."""
    policy = None if not kw else BudgetPolicy.from_dict({"version": "p1", **kw})
    runtime = AgentRuntime(env.db, env.blobs, profile=env.profile, budget_policy=policy,
                           worktree_check=lambda run_id, task_id, path: ("a" * 40, True))
    runtime._park_poll = 0.01
    runtime._cancel_timeout = 10
    env.runtime = runtime


def run(env, prompts):
    async def go():
        env.runtime.start(env.run_id, "s", {n: env.spec(n, prompt=p) for n, p in prompts.items()})
        await finish(env)
    asyncio.run(go())


def tid(env, node):
    return env.db.execute("SELECT id FROM tasks WHERE run_id=? AND node_id=?", (env.run_id, node)).fetchone()[0]


def denials(env):
    return [json.loads(r[0]) for r in env.db.execute("SELECT payload_json FROM events WHERE type='budget.exceeded' ORDER BY rowid")]


def audits(env):
    return [json.loads(r[0]) for r in env.db.execute("SELECT details_json FROM audit_entries WHERE action='budget.denied' ORDER BY rowid")]


def node_attempts(env, node):
    return [r[0] for r in env.db.execute("SELECT state FROM task_attempts WHERE task_id=? ORDER BY attempt_no", (tid(env, node),))]


def applied(env):
    return [json.loads(r[0]) for r in env.db.execute("SELECT payload_json FROM events WHERE type='budget.policy_applied' ORDER BY rowid")]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


@pytest.fixture
def chain(tmp_path):
    e = Env(tmp_path, nodes=("a", "b"))
    yield e
    e.close()


@pytest.fixture
def chain3(tmp_path):
    e = Env(tmp_path, nodes=("a", "b", "c"))
    yield e
    e.close()


def assert_refused(env, node, reason):
    """The refused task never spawned, has no attempt, ended failed, and the refusal names the reason only."""
    assert env.spawns(node) == []
    assert node_attempts(env, node) == []
    assert env.states()[1][node] == "failed"
    (event,) = denials(env)
    assert event["task_id"] == tid(env, node) and event["reason"] == reason and event["outcome"] == "failed"
    (audit,) = audits(env)
    assert audit["reason"] == reason and audit["outcome"] == "failed"
    # Only the decision is recorded: no prompt, tool, path or spec field.
    assert set(event) == {"task_id", "reason", "message", "policy_version", "outcome"}
    assert set(audit) == {"allowed", "reason", "policy_version", "scope", "breached", "unknown", "exhausted",
                          "task_id", "profile_id", "message", "outcome"}
    return event, audit


# --- under, at and over a limit -----------------------------------------------------------------------------------

def test_under_budget_runs_everything_and_records_the_policy_version(chain):
    budgeted(chain, per_run={"max_total_tokens": 100, "max_attempts": 5})
    run(chain, {"a": "ok", "b": "ok"})
    assert chain.states() == ("succeeded", {"a": "completed", "b": "completed"})
    assert denials(chain) == [] and audits(chain) == []
    sha = chain.db.execute("SELECT policy_sha256 FROM run_budget_bindings WHERE run_id=?", (chain.run_id,)).fetchone()[0]
    rows = applied(chain)
    assert [r["task_id"] for r in rows] == [tid(chain, "a"), tid(chain, "b")]
    assert all(r["policy_version"] == "p1" and r["policy_sha256"] == sha for r in rows)
    attempts = {r[0] for r in chain.db.execute("SELECT aggregate_id FROM events WHERE type='budget.policy_applied'")}
    assert attempts == {r[0] for r in chain.db.execute("SELECT id FROM task_attempts")}


def test_exactly_at_the_limit_is_allowed(chain):
    budgeted(chain, per_run={"max_total_tokens": 7})        # the fake reports 3 in + 4 out for a
    run(chain, {"a": "ok", "b": "ok"})
    assert chain.states() == ("succeeded", {"a": "completed", "b": "completed"})
    assert denials(chain) == []


def test_over_the_run_limit_fails_the_next_task_without_spawning(chain):
    budgeted(chain, per_run={"max_total_tokens": 6})
    run(chain, {"a": "ok", "b": "ok"})
    assert chain.states() == ("failed", {"a": "completed", "b": "failed"})
    event, audit = assert_refused(chain, "b", "budget_exceeded")
    assert event["policy_version"] == "p1"
    assert audit["scope"] == {"kind": "run", "id": None}
    assert audit["breached"] == [{"limit": "max_total_tokens", "limit_value": 6, "observed": 7}]
    assert "max_total_tokens limit 6 observed 7" in event["message"]
    assert [r["task_id"] for r in applied(chain)] == [tid(chain, "a")]    # only the spawned attempt


def test_attempt_limit_counts_attempts_used(chain):
    budgeted(chain, per_run={"max_attempts": 1})
    run(chain, {"a": "ok", "b": "ok"})
    assert chain.states() == ("failed", {"a": "completed", "b": "failed"})
    assert_refused(chain, "b", "budget_exceeded")


# --- unknown usage ------------------------------------------------------------------------------------------------

def test_unknown_usage_under_a_token_limit_is_never_zero(chain):
    budgeted(chain, per_run={"max_total_tokens": 1000})
    run(chain, {"a": "usage-none", "b": "ok"})
    assert chain.states() == ("failed", {"a": "completed", "b": "failed"})
    event, audit = assert_refused(chain, "b", "usage_unknown")
    assert audit["unknown"] == ["max_total_tokens"]


def test_unknown_usage_allowed_only_when_the_operator_says_so(chain):
    budgeted(chain, per_run={"max_total_tokens": 1000}, on_unknown="allow")
    run(chain, {"a": "usage-none", "b": "ok"})
    assert chain.states() == ("succeeded", {"a": "completed", "b": "completed"})


def test_unknown_usage_without_a_token_limit_does_not_matter(chain):
    budgeted(chain, per_run={"max_attempts": 5})
    run(chain, {"a": "usage-none", "b": "ok"})
    assert chain.states() == ("succeeded", {"a": "completed", "b": "completed"})
    assert denials(chain) == []


# --- run, task and profile scopes ---------------------------------------------------------------------------------

def test_task_scope(env):
    budgeted(env, per_task={"max_attempts": 0})
    run(env, {"work": "ok"})
    assert env.states() == ("failed", {"work": "failed"})
    _, audit = assert_refused(env, "work", "budget_exceeded")
    assert audit["scope"] == {"kind": "task", "id": None}


def test_profile_scope(chain):
    budgeted(chain, per_profile={P: {"max_output_tokens": 3}})
    run(chain, {"a": "ok", "b": "ok"})
    assert chain.states() == ("failed", {"a": "completed", "b": "failed"})
    _, audit = assert_refused(chain, "b", "budget_exceeded")
    assert audit["scope"] == {"kind": "profile", "id": P}
    assert audit["breached"] == [{"limit": "max_output_tokens", "limit_value": 3, "observed": 4}]


def test_other_scopes_do_not_trip_a_profile_without_limits(chain):
    budgeted(chain, per_profile={P: {"max_output_tokens": 100}}, per_task={"max_total_tokens": 1})
    run(chain, {"a": "ok", "b": "ok"})   # per task: b's own earlier attempts only, of which there are none
    assert chain.states() == ("succeeded", {"a": "completed", "b": "completed"})


# --- never retried, dependents stranded ---------------------------------------------------------------------------

def test_breach_is_never_retried_and_dependents_are_stranded(chain3):
    budgeted(chain3, per_run={"max_total_tokens": 6})
    run(chain3, {"a": "ok", "b": "ok", "c": "ok"})
    assert chain3.states() == ("failed", {"a": "completed", "b": "failed", "c": "cancelled"})
    assert chain3.spawns("b") == [] and chain3.spawns("c") == []
    assert len(denials(chain3)) == 1 and len(audits(chain3)) == 1   # c was cancelled, not budget-checked
    assert AgentWorkflowEngine._retryable(None, 0) is False and TRANSITIONS[State.FAILED] == frozenset()
    assert chain3.db.execute("SELECT COUNT(*) FROM task_attempts").fetchone() == (1,)


def test_gated_task_refused_at_park_fails_without_a_gate(env):
    budgeted(env, per_run={"max_attempts": 0})
    async def go():
        env.runtime.start(env.run_id, "s", {"work": env.spec(approval_step=GATE)})
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("failed", {"work": "failed"})
    assert env.spawns() == [] and node_attempts(env, "work") == []
    assert env.db.execute("SELECT COUNT(*) FROM task_approval_gates").fetchone() == (0,)
    assert [d["reason"] for d in denials(env)] == ["budget_exceeded"]


def test_gated_task_refused_at_resume_fails_and_never_spawns(tmp_path):
    """Resume path (approval decided after the budget ran out), through the gated-workflow harness."""
    from tests.test_budget_wiring import GatedEnv
    from tests.test_workflow_approval_gates import SPEC
    g = GatedEnv(tmp_path)
    try:
        env = g.env
        assert g.run(budgeted=False) == "blocked" and env.task()[0] == "awaiting_approval"
        g.bind(per_run={"max_attempts": 1})        # 'free' already used the one attempt
        env.decide()
        engine = env.engine()
        # the agent engine's own hooks, on the mock-provider gate harness (same resume code path)
        engine._budget_check = AgentWorkflowEngine._budget_check
        engine._budget_block_writer = AgentWorkflowEngine._budget_block_writer
        assert env.run(engine, SPEC) == "failed"
        assert env.task()[0] == "failed"
        assert g.gated_attempts() == [(1, "completed")]     # only the gate bookkeeping attempt: nothing reserved
        assert g.denial() == ["budget_exceeded"]
        assert env.db.execute("SELECT COUNT(*) FROM audit_entries WHERE action='approval.executed'").fetchone() == (0,)
    finally:
        g.env.close()


# --- no config, read failure, request payload ---------------------------------------------------------------------

def test_no_policy_means_no_budget_rows_and_unchanged_behaviour(chain):
    budgeted(chain)   # budget_policy=None
    run(chain, {"a": "usage-none", "b": "ok"})
    assert chain.states() == ("succeeded", {"a": "completed", "b": "completed"})
    assert chain.db.execute("SELECT COUNT(*) FROM run_budget_bindings").fetchone() == (0,)
    assert chain.db.execute("SELECT COUNT(*) FROM events WHERE type LIKE 'budget.%'").fetchone() == (0,)
    assert chain.db.execute("SELECT COUNT(*) FROM audit_entries WHERE action LIKE 'budget.%'").fetchone() == (0,)


def test_usage_read_failure_fails_closed_with_a_distinct_reason(chain, monkeypatch):
    budgeted(chain, per_run={"max_total_tokens": 1000})

    def broken(db, task_id, profile_id):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(budget_enforcement, "check_claim_budget", broken)
    run(chain, {"a": "ok", "b": "ok"})
    assert chain.states() == ("failed", {"a": "failed", "b": "cancelled"})
    assert chain.spawns("a") == [] and chain.spawns("b") == []
    event, audit = assert_refused(chain, "a", "budget_check_failed")
    assert event["policy_version"] == "p1"


def test_unreadable_binding_fails_closed_not_unbudgeted(env):
    """``bound_policy`` would read a failed binding lookup as "no policy"; the agent check refuses instead."""
    budgeted(env, per_run={"max_total_tokens": 1000})

    async def go():
        env.runtime.start(env.run_id, "s", {"work": env.spec()})   # binds the policy, then the engine starts
        env.db.execute("ALTER TABLE run_budget_bindings RENAME TO gone")   # before the first claim
        await finish(env)
    asyncio.run(go())
    assert env.states() == ("failed", {"work": "failed"})
    event, _ = assert_refused(env, "work", "budget_check_failed")
    assert event["policy_version"] is None


def test_budget_cannot_come_from_the_request_or_the_task_spec(env):
    with pytest.raises(ValidationError):
        AgentRunRequest(repo="/r", base_ref="main", prompt="ok", allowed_tools=["Read"],
                        budget={"per_run": {"max_attempts": 1000}})
    budgeted(env, per_task={"max_attempts": 0})
    with pytest.raises(AgentSpecError):
        env.runtime.start(env.run_id, "s", {"work": {**env.spec(), "budget": {"per_task": {"max_attempts": 9}}}})
    assert env.db.execute("SELECT state FROM runs WHERE id=?", (env.run_id,)).fetchone() == ("queued",)
    run(env, {"work": "ok"})                                  # the operator's policy is what applies
    assert_refused(env, "work", "budget_exceeded")


def test_mutation_disabling_the_check_lets_the_over_budget_task_spawn(chain, monkeypatch):
    """Guards the tests above: with the claim-time check removed, the over-budget scenario does spawn b."""
    monkeypatch.setattr(agent_engine.AgentWorkflowEngine, "_budget_check", staticmethod(lambda db, t, p: None))
    budgeted(chain, per_run={"max_total_tokens": 6})
    run(chain, {"a": "ok", "b": "ok"})
    assert chain.spawns("b") == ["ok"] and denials(chain) == []
