"""Deterministic routing rules: hard guarantees checked exhaustively and over seeded random inputs."""
import itertools
import random

import pytest

from synapse.application.routing_policy import (
    TIERS, RoutingInput, decide, estimate_savings, ESTIMATE_LABEL,
)

RANK = {t: i for i, t in enumerate(TIERS)}
ALL = tuple(TIERS)


def inp(**kw):
    base = dict(role="editor", task_kind="feature", prompt_bytes=10_000, file_count=5, has_approval_gate=False,
                is_review=False, retry_no=0, requested_tier="balanced")
    base.update(kw)
    return RoutingInput(**base)


def route(**kw):
    allowed = kw.pop("allowed", ALL)
    opts = kw.pop("opts", {})
    return decide(inp(**kw), allowed_tiers=allowed, **opts)


# ------------------------------------------------------------------ examples
def test_no_signal_means_no_change():
    d = decide(RoutingInput(), allowed_tiers=ALL)
    assert d.tier == "balanced" and d.rules_fired == () and d.confidence == 0.0 and d.downgraded_from is None
    assert d.reason == "no routing rule applied" and d.applied is False
    d = decide(RoutingInput(requested_tier="deep"), allowed_tiers=ALL)
    assert d.tier == "deep"


def test_small_mechanical_task_downgrades_to_fast_with_confidence():
    d = route(task_kind="format", prompt_bytes=500, file_count=1)
    assert (d.tier, d.downgraded_from, d.confidence, d.rules_fired) == ("fast", "balanced", 0.8, ("small_mechanical",))
    assert "balanced -> fast" in d.reason and d.baseline_tier == "balanced"


def test_downgrade_needs_the_confidence_threshold():
    tiny = dict(task_kind="feature", prompt_bytes=500, file_count=1)  # 'tiny_task' rule: confidence 0.5
    assert route(**tiny).tier == "balanced"                                        # 0.5 < default 0.6
    assert route(**tiny, opts={"min_downgrade_confidence": 0.5}).tier == "fast"
    mech = dict(task_kind="rename", prompt_bytes=500, file_count=1)                 # 0.8
    assert route(**mech, opts={"min_downgrade_confidence": 0.9}).tier == "balanced"


def test_upgrade_signals():
    assert route(task_kind="architecture").tier == "deep"
    assert route(prompt_bytes=30_000).tier == "deep" and route(file_count=40).tier == "deep"
    d = route(retry_no=2)
    assert d.tier == "deep" and "retry_escalation" in d.rules_fired and d.downgraded_from is None
    assert route(task_kind="architecture", opts={"min_upgrade_confidence": 0.95}).tier == "balanced"


def test_reviews_gates_and_retries_never_downgrade():
    mech = dict(task_kind="format", prompt_bytes=100, file_count=1)
    assert route(**mech, is_review=True).tier == "balanced"
    assert route(**mech, has_approval_gate=True).tier == "balanced"
    assert route(**mech, retry_no=1).tier in ("balanced", "deep")
    assert route(**mech, is_review=True, requested_tier="fast").tier == "balanced"      # floored up
    assert "review_or_gate_floor" in route(**mech, is_review=True, requested_tier="fast").rules_fired


def test_unknown_safety_signal_blocks_downgrade_but_does_not_raise_tier():
    mech = dict(task_kind="format", prompt_bytes=100, file_count=1, requested_tier="fast")
    for field in ("is_review", "has_approval_gate", "retry_no"):
        d = route(**mech, **{field: None})
        assert d.tier == "fast" and d.protected and d.downgraded_from is None
    d = route(task_kind="format", prompt_bytes=100, file_count=1, is_review="yes")  # unparseable: protected, no downgrade
    assert d.tier == "balanced" and d.downgraded_from is None


def test_allowed_tiers_clamp_and_floors_degrade_gracefully():
    d = route(requested_tier="deep", allowed=("fast", "balanced"))
    assert d.tier == "balanced" and "clamped_to_allowed" in d.rules_fired and d.baseline_tier == "balanced"
    d = route(requested_tier="fast", allowed=("deep",))
    assert d.tier == "deep"
    d = route(is_review=True, requested_tier="fast", allowed=("fast",))
    assert d.tier == "fast" and "floor_unavailable" in d.rules_fired                   # cannot satisfy the floor
    d = route(task_kind="architecture", allowed=("fast", "balanced"))
    assert d.tier == "balanced"                                                         # deep unavailable, never lower


def test_dry_run_is_the_default_and_applied_only_when_asked():
    assert route(task_kind="format", prompt_bytes=1, file_count=0).applied is False
    live = decide(inp(task_kind="format", prompt_bytes=1, file_count=0), allowed_tiers=ALL, dry_run=False)
    assert live.applied is True and live.tier == "fast"


@pytest.mark.parametrize("bad", [(), [], "fast", ["fast", "huge"], [None], 5])
def test_allowed_tiers_are_validated(bad):
    with pytest.raises(ValueError):
        decide(inp(), allowed_tiers=bad)


@pytest.mark.parametrize("opt", [{"min_downgrade_confidence": 1.5}, {"min_upgrade_confidence": -0.1},
                                 {"min_downgrade_confidence": float("nan")}, {"min_upgrade_confidence": True},
                                 {"min_downgrade_confidence": "0.6"}])
def test_thresholds_are_validated(opt):
    with pytest.raises(ValueError):
        decide(inp(), allowed_tiers=ALL, **opt)


def test_input_must_be_a_routing_input():
    with pytest.raises(ValueError):
        decide({"role": "x"}, allowed_tiers=ALL)


@pytest.mark.parametrize("junk", [None, "x", -5, True, 3.5, [1], {}])
def test_unparseable_signals_mean_no_change(junk):
    d = decide(RoutingInput(role=junk, task_kind=junk, prompt_bytes=junk, file_count=junk, has_approval_gate=False,
                            is_review=False, retry_no=0, requested_tier=junk), allowed_tiers=ALL)
    assert d.tier == "balanced" and d.rules_fired == ()


# ------------------------------------------------------------------ properties
KINDS = (None, "format", "rename", "feature", "architecture", "weird", 7, [1], {})
SIZES = (None, 0, 1000, 3000, 5000, 30_000, -1, "x", True, [1])
FILES = (None, 0, 1, 3, 25, -2, 1.5)
FLAGS = (True, False, None, "yes")
RETRIES = (0, 1, 3, None, -1, True)
REQUESTED = (None, "fast", "balanced", "deep", "huge", {}, [1])
ALLOWED_SETS = [tuple(c) for n in (1, 2, 3) for c in itertools.combinations(ALL, n)]


def check(d, i, allowed, down_min, up_min, dry_run):
    assert d.tier in allowed                                                   # never outside the allowed tiers
    assert d.baseline_tier in allowed and 0.0 <= d.confidence <= 1.0
    assert d.applied is (not dry_run)
    explicit_gate = i.has_approval_gate is True or i.is_review is True
    safe = i.is_review is False and i.has_approval_gate is False and i.retry_no == 0 and type(i.retry_no) is int
    if not safe:
        assert d.protected and d.downgraded_from is None and RANK[d.tier] >= RANK[d.baseline_tier]   # never downgraded
    if explicit_gate and any(RANK[t] >= RANK["balanced"] for t in allowed):
        assert RANK[d.tier] >= RANK["balanced"]                                # floor holds whenever it can
    if d.downgraded_from is not None:
        assert RANK[d.tier] < RANK[d.downgraded_from] and d.confidence >= down_min and safe
        assert d.downgraded_from == d.baseline_tier
    elif RANK[d.tier] > RANK[d.baseline_tier] and "review_or_gate_floor" not in d.rules_fired:
        assert d.confidence >= up_min                                          # upgrades need their threshold too
    if RANK[d.tier] < RANK[d.baseline_tier]:
        assert d.downgraded_from == d.baseline_tier                            # any decrease is explained and logged


def test_hard_rules_hold_exhaustively_over_a_grid():
    cases = 0
    for allowed, kind, size, files, gate, review, retry, req in itertools.product(
            ALLOWED_SETS, KINDS[:6], SIZES[:7], FILES[:5], FLAGS[:3], FLAGS[:3], RETRIES[:4], REQUESTED):
        i = RoutingInput(task_kind=kind, prompt_bytes=size, file_count=files, has_approval_gate=gate,
                         is_review=review, retry_no=retry, requested_tier=req)
        d = decide(i, allowed_tiers=allowed)
        check(d, i, allowed, 0.6, 0.3, True)
        cases += 1
    assert cases > 100_000


def test_hard_rules_hold_over_random_junk_and_thresholds():
    rng = random.Random(1120)
    for _ in range(40_000):
        i = RoutingInput(role=rng.choice(KINDS), task_kind=rng.choice(KINDS), prompt_bytes=rng.choice(SIZES),
                         file_count=rng.choice(FILES), has_approval_gate=rng.choice(FLAGS), is_review=rng.choice(FLAGS),
                         retry_no=rng.choice(RETRIES), requested_tier=rng.choice(REQUESTED))
        allowed = rng.choice(ALLOWED_SETS)
        down, up = rng.choice([0.0, 0.3, 0.5, 0.6, 0.85, 1.0]), rng.choice([0.0, 0.3, 0.65, 0.95, 1.0])
        dry = rng.choice([True, False])
        d = decide(i, allowed_tiers=allowed, min_downgrade_confidence=down, min_upgrade_confidence=up, dry_run=dry)
        check(d, i, allowed, down, up, dry)
        assert d == decide(i, allowed_tiers=list(reversed(allowed)), min_downgrade_confidence=down,
                           min_upgrade_confidence=up, dry_run=dry)             # deterministic, set order irrelevant


# ------------------------------------------------------------------ savings estimate
PRICES = {"fast": {"input_per_mtok": 1.0, "output_per_mtok": 5.0},
          "balanced": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
          "deep": {"input_per_mtok": 15.0, "output_per_mtok": 75.0}}


def test_estimate_is_labelled_unmeasured_and_does_the_arithmetic():
    d = route(task_kind="format", prompt_bytes=100, file_count=1)
    e = estimate_savings(d, PRICES, expected_input_tokens=1_000_000, expected_output_tokens=100_000)
    assert e.label == ESTIMATE_LABEL and e.measured is False and "ESTIMATE" in e.label
    assert e.baseline_tier == "balanced" and e.chosen_tier == "fast"
    assert e.estimated_usd == pytest.approx((3.0 + 1.5) - (1.0 + 0.5))
    up = route(task_kind="architecture")
    assert estimate_savings(up, PRICES, expected_input_tokens=1_000_000, expected_output_tokens=0).estimated_usd < 0


def test_estimate_is_unknown_not_zero_without_valid_prices():
    d = route(task_kind="format", prompt_bytes=100, file_count=1)
    for pricing in ({}, {"fast": PRICES["fast"]}, {"fast": {"input_per_mtok": "x", "output_per_mtok": 1},
                                                      "balanced": PRICES["balanced"]},
                    {"fast": {"input_per_mtok": float("nan"), "output_per_mtok": 1}, "balanced": PRICES["balanced"]}, None):
        assert estimate_savings(d, pricing, expected_input_tokens=10, expected_output_tokens=10).estimated_usd is None
    same = route()
    # An unchanged tier is a zero saving only when the baseline price is known; otherwise it is unknown.
    for pricing in ({}, None, {"balanced": {"input_per_mtok": "x", "output_per_mtok": 1}}):
        assert estimate_savings(same, pricing, expected_input_tokens=1, expected_output_tokens=1).estimated_usd is None
    assert estimate_savings(same, PRICES, expected_input_tokens=1, expected_output_tokens=1).estimated_usd == 0.0


@pytest.mark.parametrize("bad", [-1, True, 1.5, "1"])
def test_estimate_inputs_are_validated(bad):
    with pytest.raises(ValueError):
        estimate_savings(route(), PRICES, expected_input_tokens=bad, expected_output_tokens=1)
    with pytest.raises(ValueError):
        estimate_savings(route(), PRICES, expected_input_tokens=1, expected_output_tokens=bad)
