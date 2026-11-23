"""Pure budget policy: strict validation, fail-closed breaches, unknown usage is never zero."""
import json
import math
import random

import pytest

from synapse.application.budget_policy import (
    BudgetLimits, BudgetPolicy, Scope, evaluate, evaluate_scopes,
)

RUN, TASK = Scope("run"), Scope("task")


def policy(**kw):
    return BudgetPolicy(version=kw.pop("version", "v1"), **kw)


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("field,bad", [
    ("max_input_tokens", True), ("max_input_tokens", 1.5), ("max_input_tokens", -1), ("max_input_tokens", "5"),
    ("max_input_tokens", 10**13), ("max_cost_usd", float("nan")), ("max_cost_usd", float("inf")),
    ("max_cost_usd", -0.01), ("max_cost_usd", False), ("max_cost_usd", "1"), ("max_wall_seconds", float("-inf")),
    ("max_wall_seconds", 10**8), ("max_attempts", 1001), ("max_attempts", 2.0), ("max_total_tokens", [1]),
])
def test_limits_are_validated_strictly(field, bad):
    with pytest.raises(ValueError):
        BudgetLimits(**{field: bad})


def test_zero_and_none_limits_are_distinct():
    zero, none = BudgetLimits(max_input_tokens=0), BudgetLimits()
    assert zero.active() == {"max_input_tokens": 0} and none.active() == {}


@pytest.mark.parametrize("kwargs", [
    {"version": ""}, {"version": "has space"}, {"version": "x" * 65}, {"version": 3},
    {"version": "v", "on_unknown": "maybe"}, {"version": "v", "per_run": {"max_attempts": 1}},
    {"version": "v", "per_profile": {"bad id": BudgetLimits()}}, {"version": "v", "per_profile": {"p": {"x": 1}}},
    {"version": "v", "per_profile": {f"p{i}": BudgetLimits() for i in range(17)}},
])
def test_policy_is_validated(kwargs):
    with pytest.raises(ValueError):
        BudgetPolicy(**kwargs)


def test_policy_is_immutable_and_profile_map_is_copied():
    source = {"p1": BudgetLimits(max_attempts=2)}
    p = policy(per_profile=source)
    source["p2"] = BudgetLimits(max_attempts=9)
    assert set(p.per_profile) == {"p1"}
    with pytest.raises(TypeError):
        p.per_profile["p3"] = BudgetLimits()
    with pytest.raises(Exception):
        p.version = "v2"


def test_json_round_trip_and_strict_parsing():
    p = policy(per_run=BudgetLimits(max_total_tokens=1000, max_cost_usd=2.5, max_wall_seconds=60),
               per_task=BudgetLimits(max_attempts=2), per_profile={"claude-default": BudgetLimits(max_input_tokens=50)},
               on_unknown="allow")
    assert BudgetPolicy.from_json(p.to_json()) == p and BudgetPolicy.from_dict(json.loads(p.to_json())) == p
    assert p.to_json() == BudgetPolicy.from_json(p.to_json()).to_json()  # canonical
    for bad in ('{"version":"v","version":"w"}', '{"version":"v","per_run":{"max_cost_usd":NaN}}',
                '{"version":"v","per_run":{"max_cost_usd":Infinity}}', '{"version":"v","extra":1}',
                '{"per_run":{}}', '{"version":"v","per_run":{"max_nope":1}}', '[]', 'not json',
                '{"version":"v","per_run":{"max_attempts":true}}'):
        with pytest.raises(ValueError):
            BudgetPolicy.from_json(bad)


# ------------------------------------------------------------------ evaluate
def test_within_budget_and_exhausted_is_still_allowed():
    p = policy(per_run=BudgetLimits(max_total_tokens=100, max_attempts=3))
    d = evaluate(p, {"input_tokens": 40, "output_tokens": 60, "wall_seconds": 1.0, "attempts": 3}, scope=RUN)
    assert d.allowed and d.reason == "within_budget" and d.breached == ()
    assert d.exhausted == ("max_attempts", "max_total_tokens")
    assert d.policy_version == "v1" and d.scope == RUN


def test_breach_fails_closed_with_stable_reason_and_detail():
    p = policy(per_task=BudgetLimits(max_input_tokens=10, max_cost_usd=1.0, max_attempts=2))
    d = evaluate(p, {"input_tokens": 11, "cost_usd": 1.5, "attempts": 2}, scope=TASK)
    assert not d.allowed and d.reason == "budget_exceeded"
    assert [(b.limit, b.limit_value, b.observed) for b in d.breached] == [("max_cost_usd", 1.0, 1.5), ("max_input_tokens", 10, 11)]
    assert d.exhausted == ("max_attempts",)


def test_projected_attempt_count_is_how_callers_ask_whether_another_attempt_may_start():
    p = policy(per_task=BudgetLimits(max_attempts=2))
    assert evaluate(p, {"attempts": 2}, scope=TASK).allowed       # two used: nothing left
    assert not evaluate(p, {"attempts": 3}, scope=TASK).allowed   # a third would breach


@pytest.mark.parametrize("totals", [{}, {"input_tokens": None}, {"cost_usd": None, "input_tokens": 0}])
def test_unknown_usage_is_never_zero_and_is_denied_by_default(totals):
    # A zero limit would be "within budget" if unknown were treated as 0.
    p = policy(per_run=BudgetLimits(max_input_tokens=0, max_cost_usd=0.0))
    d = evaluate(p, totals, scope=RUN)
    assert not d.allowed and d.reason == "usage_unknown" and d.breached == ()
    assert "max_cost_usd" in d.unknown or "max_input_tokens" in d.unknown


def test_unknown_usage_can_be_allowed_explicitly_and_is_reported():
    p = policy(per_run=BudgetLimits(max_cost_usd=5.0), on_unknown="allow")
    d = evaluate(p, {}, scope=RUN)
    assert d.allowed and d.reason == "usage_unknown_allowed" and d.unknown == ("max_cost_usd",)
    # ...but a known breach still wins over an allowed unknown
    p2 = policy(per_run=BudgetLimits(max_cost_usd=5.0, max_input_tokens=1), on_unknown="allow")
    d2 = evaluate(p2, {"input_tokens": 2}, scope=RUN)
    assert not d2.allowed and d2.reason == "budget_exceeded" and d2.unknown == ("max_cost_usd",)


def test_unknown_metric_without_a_limit_is_irrelevant():
    p = policy(per_run=BudgetLimits(max_input_tokens=100))
    d = evaluate(p, {"input_tokens": 5}, scope=RUN)  # cost etc. unknown, but unlimited
    assert d.allowed and d.unknown == ()


def test_total_tokens_derivation_never_guesses():
    p = policy(per_run=BudgetLimits(max_total_tokens=10))
    assert evaluate(p, {"input_tokens": 6, "output_tokens": 5}, scope=RUN).breached[0].observed == 11
    assert evaluate(p, {"total_tokens": 3, "input_tokens": 99}, scope=RUN).allowed          # a reported total wins
    only_input = evaluate(p, {"input_tokens": 99}, scope=RUN)                                # output unknown: NOT 99+0
    assert only_input.reason == "usage_unknown" and only_input.unknown == ("max_total_tokens",)


@pytest.mark.parametrize("bad", [True, -1, float("nan"), float("inf"), "5", 1.5, [1]])
def test_garbage_observations_are_a_caller_error_not_unknown(bad):
    p = policy(per_run=BudgetLimits(max_input_tokens=10))
    with pytest.raises(ValueError):
        evaluate(p, {"input_tokens": bad}, scope=RUN)


@pytest.mark.parametrize("limits", [BudgetLimits(max_wall_seconds=5), BudgetLimits(max_attempts=1)])
def test_always_known_metrics_must_be_supplied(limits):
    with pytest.raises(ValueError):
        evaluate(policy(per_run=limits), {}, scope=RUN)


def test_profile_scope_uses_its_own_limits_and_missing_profile_means_no_limits():
    p = policy(per_run=BudgetLimits(max_input_tokens=1), per_profile={"p1": BudgetLimits(max_input_tokens=10)})
    assert evaluate(p, {"input_tokens": 5}, scope=Scope("profile", "p1")).allowed
    assert not evaluate(p, {"input_tokens": 11}, scope=Scope("profile", "p1")).allowed
    assert evaluate(p, {"input_tokens": 10**9}, scope=Scope("profile", "other")).allowed
    for args in (("profile",), ("profile", "bad id"), ("galaxy",)):
        with pytest.raises(ValueError):
            Scope(*args)


def test_bad_arguments_raise():
    p = policy()
    for call in (lambda: evaluate({}, {}, scope=RUN), lambda: evaluate(p, [], scope=RUN), lambda: evaluate(p, {}, scope="run")):
        with pytest.raises(ValueError):
            call()


def test_evaluate_scopes_combines_and_never_allows_nothing():
    p = policy(per_run=BudgetLimits(max_input_tokens=100), per_task=BudgetLimits(max_input_tokens=10))
    both_ok = evaluate_scopes(p, [(RUN, {"input_tokens": 50}), (TASK, {"input_tokens": 5})])
    assert both_ok.allowed
    task_breach = evaluate_scopes(p, [(RUN, {"input_tokens": 50}), (TASK, {"input_tokens": 11})])
    assert not task_breach.allowed and task_breach.scope == TASK and task_breach.reason == "budget_exceeded"
    unknown = evaluate_scopes(p, [(RUN, {"input_tokens": 50}), (TASK, {})])
    assert not unknown.allowed and unknown.reason == "usage_unknown"
    with pytest.raises(ValueError):
        evaluate_scopes(p, [])


# ------------------------------------------------------------------ properties
def test_decision_matches_the_spec_over_random_inputs():
    rng = random.Random(20261120)
    pairs = (("max_input_tokens", "input_tokens"), ("max_output_tokens", "output_tokens"),
             ("max_cost_usd", "cost_usd"), ("max_attempts", "attempts"))
    for _ in range(4000):
        limits = BudgetLimits(
            max_input_tokens=rng.choice([None, 0, 5, 100]), max_output_tokens=rng.choice([None, 0, 5, 100]),
            max_cost_usd=rng.choice([None, 0.0, 0.5, 10.0]), max_attempts=rng.choice([None, 0, 2, 5]))
        pol = policy(per_run=limits, on_unknown=rng.choice(["deny", "allow"]))
        totals = {"attempts": rng.randint(0, 6)}
        for metric in ("input_tokens", "output_tokens", "cost_usd"):
            if rng.random() < 0.7:
                totals[metric] = (rng.choice([0, 1, 5, 6, 100, 101]) if metric != "cost_usd"
                                  else rng.choice([0.0, 0.5, 0.51, 10.0, 11.0]))
        d = evaluate(pol, totals, scope=RUN)
        assert d == evaluate(pol, dict(reversed(list(totals.items()))), scope=RUN)  # deterministic
        active = limits.active()
        want_breach = any(name in active and totals.get(metric) is not None and totals[metric] > active[name]
                          for name, metric in pairs)
        want_unknown = sorted(name for name, metric in pairs[:3] if name in active and metric not in totals)
        assert bool(d.breached) == want_breach and list(d.unknown) == want_unknown
        assert d.allowed == (not want_breach and (not want_unknown or pol.on_unknown == "allow"))
        assert (d.reason == "budget_exceeded") == want_breach
        assert d.reason in ("within_budget", "budget_exceeded", "usage_unknown", "usage_unknown_allowed")
        assert all(math.isfinite(b.observed) for b in d.breached)
