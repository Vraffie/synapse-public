"""Deterministic model-tier routing rules (M5.5), dry-run first. Pure: no I/O, no model calls.

A task is routed to a *tier* (``fast`` < ``balanced`` < ``deep``); mapping a tier to a concrete model
is the profile's job, not this module's. The rules are small, explicit and logged by name.

Hard guarantees (property-tested):

* The chosen tier is always in ``allowed_tiers``.
* A review, an approval-gated step or a retry is **never downgraded** by a rule: its tier is never
  below the (allowed-clamped) requested tier, and reviews and gates are floored at ``balanced``
  whenever an allowed tier at or above that floor exists.
* A downgrade needs ``confidence >= min_downgrade_confidence``; an upgrade needs
  ``confidence >= min_upgrade_confidence``. Upgrading is cheap to be wrong about, downgrading is not.
* Missing or unparseable signals mean "no change". A missing/unparseable *safety* flag
  (``is_review``, ``has_approval_gate``, ``retry_no``) is treated as the protected value, so it can
  only ever block a downgrade; only an explicit ``True`` review/gate raises a tier to the floor.
* ``dry_run=True`` (the default) returns the decision with ``applied=False``; the caller must not act.

Out of scope on purpose: an LLM judge. A judge call costs tokens, needs its own budget and is hard
to test; revisit only if dry-run logs show these rules are not enough.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
import math
from typing import Any

TIERS = ("fast", "balanced", "deep")
_RANK = {t: i for i, t in enumerate(TIERS)}
DEFAULT_TIER = "balanced"
FLOOR_TIER = "balanced"  # reviews and approval-gated steps never run below this when possible

# Rule vocabulary. Kinds are matched exactly (lower-case); anything else is "no evidence".
MECHANICAL_KINDS = frozenset({"format", "rename", "summarize", "lint-fix", "docs", "read-only-search"})
HARD_KINDS = frozenset({"architecture", "security-review", "migration", "schema-design"})
SMALL_PROMPT_BYTES = 4096
TINY_PROMPT_BYTES = 2048
SMALL_FILE_COUNT = 2
LARGE_PROMPT_BYTES = 24576
LARGE_FILE_COUNT = 20


@dataclass(frozen=True, slots=True)
class RoutingInput:
    """Signals about one task. Fields may be ``None``/garbage; see the module guarantees."""

    role: Any = None
    task_kind: Any = None
    prompt_bytes: Any = None
    file_count: Any = None
    has_approval_gate: Any = None
    is_review: Any = None
    retry_no: Any = None
    requested_tier: Any = None


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    tier: str
    confidence: float                # of the rule that moved the tier; 0.0 when nothing fired
    rules_fired: tuple[str, ...]
    downgraded_from: str | None      # set only when a rule lowered the tier
    reason: str
    baseline_tier: str               # requested (or default) tier, clamped to allowed_tiers
    applied: bool = False            # False in dry-run: the caller must not act on it
    protected: bool = False          # review / approval gate / retry (or unknown safety signal)


def _count(value: Any) -> int | None:
    """A non-negative int (bools and other types are 'missing')."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _validated_allowed(allowed_tiers: Collection[str]) -> tuple[str, ...]:
    if isinstance(allowed_tiers, str) or not isinstance(allowed_tiers, Collection):
        raise ValueError("allowed_tiers must be a collection of tiers")
    allowed = tuple(sorted(set(allowed_tiers), key=lambda t: _RANK.get(t, 99)))
    if not allowed or any(t not in _RANK for t in allowed):
        raise ValueError("allowed_tiers must be a non-empty subset of fast, balanced, deep")
    return allowed


def _threshold(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{what} must be a number between 0 and 1")
    return float(value)


def _nearest(allowed: tuple[str, ...], target: str, *, at_least: str | None = None) -> str:
    """The allowed tier closest to ``target`` (ties go higher), optionally not below ``at_least``."""
    pool = [t for t in allowed if at_least is None or _RANK[t] >= _RANK[at_least]] or [allowed[-1]]
    return min(pool, key=lambda t: (abs(_RANK[t] - _RANK[target]), -_RANK[t]))


def _step(tier: str, delta: int) -> str:
    return TIERS[max(0, min(len(TIERS) - 1, _RANK[tier] + delta))]


def decide(inp: RoutingInput, *, allowed_tiers: Collection[str], min_downgrade_confidence: float = 0.6,
           min_upgrade_confidence: float = 0.3, dry_run: bool = True) -> RoutingDecision:
    """Route one task. ``allowed_tiers``/thresholds are operator config and are validated strictly
    (``ValueError``); the task signals in ``inp`` are never trusted to be well-formed."""
    if not isinstance(inp, RoutingInput):
        raise ValueError("inp must be a RoutingInput")
    allowed = _validated_allowed(allowed_tiers)
    down_min = _threshold(min_downgrade_confidence, "min_downgrade_confidence")
    up_min = _threshold(min_upgrade_confidence, "min_upgrade_confidence")
    requested = inp.requested_tier if isinstance(inp.requested_tier, str) and inp.requested_tier in _RANK else DEFAULT_TIER
    base = _nearest(allowed, requested)
    fired: list[str] = []
    if base != requested:
        fired.append("clamped_to_allowed")

    kind = inp.task_kind.lower() if isinstance(inp.task_kind, str) else None
    prompt_bytes, files = _count(inp.prompt_bytes), _count(inp.file_count)
    review, gate, retries = _flag(inp.is_review), _flag(inp.has_approval_gate), _count(inp.retry_no)
    # Unknown safety signal -> assume protected (it can only block a downgrade, never cause one).
    protected = review is not False or gate is not False or retries != 0
    floored = review is True or gate is True  # only an EXPLICIT review/gate raises a tier; unknown just blocks downgrades

    tier, confidence = base, 0.0
    downgraded_from = None

    # --- upgrades (checked first: evidence of difficulty always beats evidence of simplicity) ---
    up: list[tuple[str, float, str]] = []  # (rule, confidence, target tier)
    if kind in HARD_KINDS:
        up.append(("hard_kind", 0.9, "deep"))
    if (prompt_bytes is not None and prompt_bytes >= LARGE_PROMPT_BYTES) or (files is not None and files >= LARGE_FILE_COUNT):
        up.append(("large_context", 0.7, _step(base, +1)))
    if retries is not None and retries >= 1:
        up.append(("retry_escalation", 0.6, _step(base, +1)))
    up = [(r, c, t) for r, c, t in up if c >= up_min and _RANK[t] > _RANK[base]]
    if up:
        rule, conf, target = max(up, key=lambda u: (_RANK[u[2]], u[1]))
        tier = _nearest(allowed, target, at_least=base)
        fired += [r for r, _, _ in up]
        confidence = conf
        if _RANK[tier] <= _RANK[base]:  # the allowed set had nothing higher
            tier, confidence = base, 0.0
            fired.append("upgrade_unavailable")

    # --- floors for reviews and approval gates ----------------------------------------------
    if floored and _RANK[tier] < _RANK[FLOOR_TIER]:
        raised = _nearest(allowed, FLOOR_TIER, at_least=FLOOR_TIER)
        if _RANK[raised] >= _RANK[FLOOR_TIER]:
            tier = raised
            fired.append("review_or_gate_floor")
        else:
            fired.append("floor_unavailable")

    # --- downgrades: only unprotected tasks, only with no upgrade evidence, only with confidence ---
    if not protected and tier == base:
        down: list[tuple[str, float, str]] = []
        if (kind in MECHANICAL_KINDS and prompt_bytes is not None and prompt_bytes <= SMALL_PROMPT_BYTES
                and files is not None and files <= SMALL_FILE_COUNT):
            down.append(("small_mechanical", 0.8, "fast"))
        if (kind not in HARD_KINDS and prompt_bytes is not None and prompt_bytes <= TINY_PROMPT_BYTES
                and files is not None and files <= 1):
            down.append(("tiny_task", 0.5, _step(base, -1)))
        down = [(r, c, t) for r, c, t in down if c >= down_min and _RANK[t] < _RANK[base]]
        if down:
            rule, conf, target = max(down, key=lambda d: (d[1], -_RANK[d[2]]))
            chosen = _nearest(allowed, target)
            if _RANK[chosen] < _RANK[base]:
                downgraded_from, tier, confidence = base, chosen, conf
                fired += [r for r, _, _ in down]

    if tier != base:
        reason = f"{base} -> {tier} by " + ", ".join(fired)
    else:
        reason = f"kept {base} ({', '.join(fired)})" if fired else "no routing rule applied"
    return RoutingDecision(tier=tier, confidence=confidence, rules_fired=tuple(fired), downgraded_from=downgraded_from,
                           reason=reason, baseline_tier=base, applied=not dry_run, protected=protected)


@dataclass(frozen=True, slots=True)
class SavingsEstimate:
    """An ESTIMATE from caller-supplied expectations and a price table. Never measured usage:
    it has no field that fits the usage ledger, and must not be added to measured totals."""

    label: str
    baseline_tier: str
    chosen_tier: str
    estimated_usd: float | None      # positive = cheaper than baseline, negative = costlier, None = unknown
    basis: Mapping[str, Any] = field(default_factory=dict)
    measured: bool = False


ESTIMATE_LABEL = "ESTIMATE (not measured usage)"


def _price(pricing: Mapping[str, Any], tier: str) -> tuple[float, float] | None:
    entry = pricing.get(tier) if isinstance(pricing, Mapping) else None
    if not isinstance(entry, Mapping):
        return None
    out = []
    for key in ("input_per_mtok", "output_per_mtok"):
        v = entry.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            return None
        out.append(float(v))
    return out[0], out[1]


def estimate_savings(decision: RoutingDecision, pricing: Mapping[str, Any], *, expected_input_tokens: int,
                     expected_output_tokens: int) -> SavingsEstimate:
    """Estimated USD difference between running at the baseline tier and the chosen tier.

    ``pricing``: ``{tier: {"input_per_mtok": usd, "output_per_mtok": usd}}`` supplied by the operator.
    Missing or invalid prices give ``estimated_usd=None`` (unknown), never 0.
    """
    for what, v in (("expected_input_tokens", expected_input_tokens), ("expected_output_tokens", expected_output_tokens)):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"{what} must be a non-negative integer")
    basis = {"expected_input_tokens": expected_input_tokens, "expected_output_tokens": expected_output_tokens}
    if decision.tier == decision.baseline_tier:
        known = _price(pricing, decision.baseline_tier) is not None  # no price means unknown, not a measured zero
        return SavingsEstimate(ESTIMATE_LABEL, decision.baseline_tier, decision.tier, 0.0 if known else None, basis)
    base, chosen = _price(pricing, decision.baseline_tier), _price(pricing, decision.tier)
    if base is None or chosen is None:
        return SavingsEstimate(ESTIMATE_LABEL, decision.baseline_tier, decision.tier, None, basis)

    def cost(p: tuple[float, float]) -> float:
        return (expected_input_tokens * p[0] + expected_output_tokens * p[1]) / 1_000_000
    return SavingsEstimate(ESTIMATE_LABEL, decision.baseline_tier, decision.tier, round(cost(base) - cost(chosen), 10), basis)
