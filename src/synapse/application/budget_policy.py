"""Pure budget policy (M5.2): limits per run, task and profile, evaluated against known usage.

Nothing here touches the database, the clock or a provider. A caller (not yet wired) gathers usage
totals, builds a scope, and asks :func:`evaluate` whether work may begin or continue.

Semantics that never bend:

* A breach **fails closed** with the stable reason ``budget_exceeded``. Nothing here retries.
* **Unknown usage is never zero.** ``None`` (or a missing key) means "not reported". When a limit
  exists on a metric whose value is unknown, the decision is *denied* (``usage_unknown``) unless the
  policy says ``on_unknown="allow"`` (then ``usage_unknown_allowed``). Garbage (a bool, a negative,
  NaN, a string) is a caller bug and raises ``ValueError``; it is not "unknown".
* Wall-clock seconds and attempt counts are always known to the daemon; a limit on them with no
  observed value raises ``ValueError``.
* ``observed > limit`` is a breach; ``observed == limit`` is merely ``exhausted`` (listed, still
  allowed). To ask "may another attempt start?" pass the *projected* attempt count (used + 1).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
import re
from types import MappingProxyType
from typing import Any

POLICY_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
PROFILE_ID_RE = POLICY_VERSION_RE
MAX_PROFILES = 16
ON_UNKNOWN = ("deny", "allow")

# limit name -> (usage metric it bounds, kind, upper bound accepted in a policy)
_LIMITS: Mapping[str, tuple[str, str, float]] = MappingProxyType({
    "max_input_tokens": ("input_tokens", "int", 10**12),
    "max_output_tokens": ("output_tokens", "int", 10**12),
    "max_total_tokens": ("total_tokens", "int", 10**12),
    "max_cost_usd": ("cost_usd", "float", 10**6),
    "max_wall_seconds": ("wall_seconds", "float", 10**7),
    "max_attempts": ("attempts", "int", 1000),
})
# Metrics the daemon always knows; an absent observation for a limited one is a caller error.
_ALWAYS_KNOWN = frozenset({"wall_seconds", "attempts"})
_SCOPE_KINDS = ("run", "task", "profile")

REASON_WITHIN = "within_budget"
REASON_EXCEEDED = "budget_exceeded"
REASON_UNKNOWN = "usage_unknown"
REASON_UNKNOWN_ALLOWED = "usage_unknown_allowed"


def _number(value: Any, kind: str, what: str, *, upper: float | None = None) -> int | float:
    """Strictly validate one number: bools, NaN/inf, negatives and wrong types are rejected."""
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a number, not a bool")
    if kind == "int":
        if not isinstance(value, int):
            raise ValueError(f"{what} must be an integer")
    else:
        if not isinstance(value, (int, float)):
            raise ValueError(f"{what} must be a number")
        if not math.isfinite(value):
            raise ValueError(f"{what} must be finite")
        value = float(value)
    if value < 0:
        raise ValueError(f"{what} must not be negative")
    if upper is not None and value > upper:
        raise ValueError(f"{what} is above the supported maximum")
    return value


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Optional ceilings. ``None`` means "no limit on this metric"."""

    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    max_wall_seconds: float | None = None
    max_attempts: int | None = None

    def __post_init__(self) -> None:
        for name, (_, kind, upper) in _LIMITS.items():
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _number(value, kind, name, upper=upper))

    def active(self) -> dict[str, int | float]:
        return {name: getattr(self, name) for name in _LIMITS if getattr(self, name) is not None}

    def to_dict(self) -> dict[str, int | float]:
        return self.active()

    @classmethod
    def from_dict(cls, data: Any, what: str = "limits") -> BudgetLimits:
        if not isinstance(data, Mapping):
            raise ValueError(f"{what} must be an object")
        unknown = set(data) - set(_LIMITS)
        if unknown:
            raise ValueError(f"{what} has unknown keys")
        return cls(**{k: data[k] for k in data})


_NO_LIMITS = BudgetLimits()


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    version: str
    per_run: BudgetLimits = _NO_LIMITS
    per_task: BudgetLimits = _NO_LIMITS
    per_profile: Mapping[str, BudgetLimits] = field(default_factory=dict)
    on_unknown: str = "deny"

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not POLICY_VERSION_RE.fullmatch(self.version):
            raise ValueError("version must be 1-64 characters of [A-Za-z0-9._-]")
        for name in ("per_run", "per_task"):
            if not isinstance(getattr(self, name), BudgetLimits):
                raise ValueError(f"{name} must be BudgetLimits")
        if not isinstance(self.per_profile, Mapping) or len(self.per_profile) > MAX_PROFILES:
            raise ValueError(f"per_profile must be a mapping of at most {MAX_PROFILES} profiles")
        for pid, limits in self.per_profile.items():
            if not isinstance(pid, str) or not PROFILE_ID_RE.fullmatch(pid):
                raise ValueError("per_profile keys must be profile ids")
            if not isinstance(limits, BudgetLimits):
                raise ValueError("per_profile values must be BudgetLimits")
        if self.on_unknown not in ON_UNKNOWN:
            raise ValueError("on_unknown must be 'deny' or 'allow'")
        object.__setattr__(self, "per_profile", MappingProxyType(dict(self.per_profile)))

    # -- JSON round trip ---------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "on_unknown": self.on_unknown,
                "per_run": self.per_run.to_dict(), "per_task": self.per_task.to_dict(),
                "per_profile": {pid: lim.to_dict() for pid, lim in sorted(self.per_profile.items())}}

    @classmethod
    def from_dict(cls, data: Any) -> BudgetPolicy:
        if not isinstance(data, Mapping):
            raise ValueError("policy must be an object")
        if "version" not in data:
            raise ValueError("policy needs a version")
        if set(data) - {"version", "on_unknown", "per_run", "per_task", "per_profile"}:
            raise ValueError("policy has unknown keys")
        profiles = data.get("per_profile", {})
        if not isinstance(profiles, Mapping):
            raise ValueError("per_profile must be an object")
        return cls(version=data["version"], on_unknown=data.get("on_unknown", "deny"),
                   per_run=BudgetLimits.from_dict(data.get("per_run", {}), "per_run"),
                   per_task=BudgetLimits.from_dict(data.get("per_task", {}), "per_task"),
                   per_profile={k: BudgetLimits.from_dict(v, "per_profile entry") for k, v in profiles.items()})

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_json(cls, text: str) -> BudgetPolicy:
        """Strict parse: duplicate keys and NaN/Infinity constants are refused."""
        def no_duplicates(pairs):
            keys = [k for k, _ in pairs]
            if len(set(keys)) != len(keys):
                raise ValueError("duplicate key in policy JSON")
            return dict(pairs)

        def no_constants(name):
            raise ValueError("non-finite number in policy JSON")
        try:
            data = json.loads(text, object_pairs_hook=no_duplicates, parse_constant=no_constants)
        except json.JSONDecodeError:
            raise ValueError("policy is not valid JSON") from None
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class Scope:
    """Which limits apply: the run's, one task's, or one profile's (``id`` required for profile)."""

    kind: str
    id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _SCOPE_KINDS:
            raise ValueError("scope kind must be run, task or profile")
        if self.kind == "profile" and not (isinstance(self.id, str) and PROFILE_ID_RE.fullmatch(self.id)):
            raise ValueError("a profile scope needs a profile id")


@dataclass(frozen=True, slots=True)
class Breach:
    limit: str
    limit_value: int | float
    observed: int | float


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    reason: str
    breached: tuple[Breach, ...]
    unknown: tuple[str, ...]      # limited metrics whose value was not reported (sorted)
    exhausted: tuple[str, ...]    # limits met exactly: allowed, but nothing is left (sorted)
    policy_version: str
    scope: Scope


def _observed(totals: Mapping[str, Any], metric: str, kind: str) -> int | float | None:
    value = totals.get(metric)
    if value is None:
        return None
    return _number(value, kind, f"usage {metric}")


def _total_tokens(totals: Mapping[str, Any]) -> int | None:
    """Reported total, else input+output only when both are known; otherwise unknown."""
    reported = _observed(totals, "total_tokens", "int")
    if reported is not None:
        return reported
    a, b = _observed(totals, "input_tokens", "int"), _observed(totals, "output_tokens", "int")
    return None if a is None or b is None else a + b


def evaluate(policy: BudgetPolicy, usage_totals: Mapping[str, Any], *, scope: Scope) -> BudgetDecision:
    """Decide for one scope. ``usage_totals`` keys: input_tokens, output_tokens, total_tokens,
    cost_usd, wall_seconds, attempts. ``None``/absent = unknown (never zero)."""
    if not isinstance(policy, BudgetPolicy):
        raise ValueError("policy must be a BudgetPolicy")
    if not isinstance(scope, Scope):
        raise ValueError("scope must be a Scope")
    if not isinstance(usage_totals, Mapping):
        raise ValueError("usage_totals must be a mapping")
    if scope.kind == "run":
        limits = policy.per_run
    elif scope.kind == "task":
        limits = policy.per_task
    else:
        limits = policy.per_profile.get(scope.id, _NO_LIMITS)
    breached: list[Breach] = []
    unknown: list[str] = []
    exhausted: list[str] = []
    for name, limit in limits.active().items():
        metric, kind, _ = _LIMITS[name]
        value = _total_tokens(usage_totals) if metric == "total_tokens" else _observed(usage_totals, metric, kind)
        if value is None:
            if metric in _ALWAYS_KNOWN:
                raise ValueError(f"{metric} is always known and must be supplied for {name}")
            unknown.append(name)
        elif value > limit:
            breached.append(Breach(name, limit, value))
        elif value == limit:
            exhausted.append(name)
    unknown.sort()
    exhausted.sort()
    breached.sort(key=lambda b: b.limit)
    if breached:
        allowed, reason = False, REASON_EXCEEDED
    elif unknown:
        allowed = policy.on_unknown == "allow"
        reason = REASON_UNKNOWN_ALLOWED if allowed else REASON_UNKNOWN
    else:
        allowed, reason = True, REASON_WITHIN
    return BudgetDecision(allowed, reason, tuple(breached), tuple(unknown), tuple(exhausted), policy.version, scope)


def evaluate_scopes(policy: BudgetPolicy, scoped_totals: list[tuple[Scope, Mapping[str, Any]]]) -> BudgetDecision:
    """Combine several scopes (e.g. run + task + profile): allowed only if every scope allows.

    The returned decision is the most severe one (exceeded > unknown > within); on a tie the first
    wins. An empty list is an error: "no scopes" must not silently mean "allowed".
    """
    if not scoped_totals:
        raise ValueError("at least one scope is required")
    decisions = [evaluate(policy, totals, scope=scope) for scope, totals in scoped_totals]
    severity = {REASON_EXCEEDED: 3, REASON_UNKNOWN: 2, REASON_UNKNOWN_ALLOWED: 1, REASON_WITHIN: 0}
    # Every denying reason outranks every allowing one, so the worst decision denies iff any scope does.
    return max(decisions, key=lambda d: severity[d.reason])
