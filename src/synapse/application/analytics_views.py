"""Read-only analytics views for one run: attempts, retries, messages, tokens, with the provenance of every number.

Every numeric value is a :class:`Quantity` whose ``kind`` says what it is:

* ``MEASURED``  - a number a provider reported (tokens, cost), or an exact count of the daemon's own records.
* ``ESTIMATED`` - a number Synapse derived (``optimization.estimated`` events); ``basis`` names the method. Estimates
  live in their own ``estimated`` section and are never added to, or subtracted from, a measured total.
* ``UNKNOWN``   - nothing reported: ``value`` is ``None``, never 0.

Coverage follows ``persistence/usage_query.py``: ``known`` attempts reported the field out of ``of`` attempts, and
``lower_bound`` is true exactly when a value exists and ``known < of`` (the total omits the unknown attempts).

Two code paths build the same view from different sources and share ONE pure builder (:func:`_build`):
:func:`read_run_analytics` reads the authoritative tables, :func:`replay_run_analytics` folds the append-only event log.
Fields the event log cannot carry are reported as not reproducible (``not_reproducible``), never hidden or guessed.
Both paths open their own read transaction, never write, and refuse to materialise more than the hard caps below
(``AnalyticsTooLarge``) instead of silently truncating a total. Authorization is the caller's responsibility.
"""
from __future__ import annotations

import enum
import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Any

from synapse.domain.contracts import validate_id
from synapse.persistence.run_list import InvalidCursor, decode_cursor, encode_cursor
from synapse.persistence.run_snapshot import RunNotFound

USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd")
MAX_PAGE = 200
DEFAULT_PAGE = 50
MAX_ATTEMPTS = 20_000
MAX_OBSERVATIONS = 100_000
MAX_EVENTS = 200_000
MAX_PROFILES = 200
_CURSOR_MARK = "task"
_STATE_EVENTS = ("task_attempt.state_changed", "task_attempt.unknown", "task_attempt.mock_reconciled",
                 "task_attempt.mock_cancelled_after_stop")
_DELIVERY_EVENTS = ("delivery.claimed", "delivery.acknowledged", "delivery.dead_letter", "delivery.dead_lettered",
                    "delivery.pending")
_EVENT_TYPES = ("task.attempt_reserved", *_STATE_EVENTS, "agent.usage_observed", "optimization.estimated",
                "message.created", *_DELIVERY_EVENTS)
NOT_REPRODUCIBLE_FROM_EVENTS = ("per_profile", "messages.deliveries_created")


class AnalyticsTooLarge(RuntimeError):
    """The run exceeds a hard cap; the view is refused rather than silently truncated."""


class Kind(str, enum.Enum):
    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Quantity:
    kind: Kind
    value: int | float | None
    unit: str
    basis: str  # "provider_reported", "daemon_record" or the estimate method name
    known: int
    of: int
    lower_bound: bool

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "value": self.value, "unit": self.unit, "basis": self.basis,
                "known": self.known, "of": self.of, "lower_bound": self.lower_bound}


@dataclass(frozen=True, slots=True)
class TaskAnalytics:
    task_id: str
    attempts: Quantity
    retries: Quantity
    outcome_mix: dict[str, Quantity]
    usage: dict[str, Quantity]


@dataclass(frozen=True, slots=True)
class ProfileAnalytics:
    attempts: Quantity
    usage: dict[str, Quantity]


@dataclass(frozen=True, slots=True)
class MessageAnalytics:
    sent: Quantity
    deliveries_created: Quantity  # UNKNOWN on the replay path: no event records delivery creation
    claimed: Quantity  # "delivered": leased to a recipient at least once
    redeliveries: Quantity  # claims beyond the first, per delivery
    acknowledged: Quantity
    dead_lettered: Quantity


@dataclass(frozen=True, slots=True)
class RunAnalytics:
    run_id: str
    source: str  # "tables" or "events"
    attempts: Quantity
    retries: Quantity
    outcome_mix: dict[str, Quantity]
    usage: dict[str, Quantity]  # MEASURED / UNKNOWN only
    attempts_with_any_usage: int
    estimated: dict[str, dict[str, Quantity]]  # method -> {"est_tokens": ESTIMATED Quantity}; never merged into usage
    messages: MessageAnalytics
    tasks: tuple[TaskAnalytics, ...]
    next_cursor: str | None
    per_profile: dict[str, ProfileAnalytics] | None  # None when not reproducible from events
    per_profile_truncated: bool
    not_reproducible: tuple[str, ...]
    replay_ignored_events: int | None  # events a replay could not attribute; None on the table path
    ignored_estimate_events: int

    def to_dict(self) -> dict[str, Any]:
        def q(d): return {k: v.to_dict() for k, v in d.items()}
        return {
            "run_id": self.run_id, "source": self.source, "attempts": self.attempts.to_dict(),
            "retries": self.retries.to_dict(), "outcome_mix": q(self.outcome_mix), "usage": q(self.usage),
            "attempts_with_any_usage": self.attempts_with_any_usage,
            "estimated": {m: q(v) for m, v in self.estimated.items()},
            "messages": {k: getattr(self.messages, k).to_dict() for k in self.messages.__slots__},
            "tasks": [{"task_id": t.task_id, "attempts": t.attempts.to_dict(), "retries": t.retries.to_dict(),
                       "outcome_mix": q(t.outcome_mix), "usage": q(t.usage)} for t in self.tasks],
            "next_cursor": self.next_cursor,
            "per_profile": None if self.per_profile is None else
            {p: {"attempts": v.attempts.to_dict(), "usage": q(v.usage)} for p, v in self.per_profile.items()},
            "per_profile_truncated": self.per_profile_truncated,
            "not_reproducible": list(self.not_reproducible),
            "replay_ignored_events": self.replay_ignored_events,
            "ignored_estimate_events": self.ignored_estimate_events,
        }


# ------------------------------------------------------------------ quantities
def _count(n: int) -> Quantity:
    """An exact count of the daemon's own records: always known, so never a lower bound."""
    return Quantity(Kind.MEASURED, n, "count", "daemon_record", n, n, False)


def _unknown_count(unit: str = "count") -> Quantity:
    return Quantity(Kind.UNKNOWN, None, unit, "daemon_record", 0, 0, False)


def _total(values: list[int | float], unit: str, field_name: str, known: int, of: int,
           kind: Kind = Kind.MEASURED, basis: str = "provider_reported") -> Quantity:
    if not values:
        return Quantity(Kind.UNKNOWN, None, unit, basis, 0, of, False)
    value = math.fsum(values) if field_name == "cost_usd" else sum(values)  # fsum: order independent
    return Quantity(kind, value, unit, basis, known, of, known < of)


def _unit(field_name: str) -> str:
    return "usd" if field_name == "cost_usd" else "tokens"


def _usage(observations: list[dict[str, Any]], of: int) -> dict[str, Quantity]:
    out = {}
    for name in USAGE_FIELDS:
        present = [o for o in observations if o[name] is not None]
        out[name] = _total([o[name] for o in present], _unit(name), name, len({o["attempt_id"] for o in present}), of)
    return out


def _clean(value: Any, name: str) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or not math.isfinite(value):
        return None
    return float(value) if name == "cost_usd" else (value if isinstance(value, int) else None)


# ------------------------------------------------------------------ pure builder shared by both paths
@dataclass
class _Facts:
    attempts: list[dict[str, Any]]  # attempt_id, task_id, attempt_no, state, profile_id (None if unknown)
    observations: list[dict[str, Any]]  # attempt_id + USAGE_FIELDS
    estimates: list[dict[str, Any]]  # attempt_id, method, est_tokens
    sent: int
    deliveries: list[tuple[str | None, int]]  # (final state, claims)
    deliveries_known: bool
    profiles_known: bool
    ignored: int | None = None
    ignored_estimates: int = 0


def _build(run_id: str, source: str, facts: _Facts, limit: int, after: str | None) -> RunAnalytics:
    by_task: dict[str, list[dict[str, Any]]] = {}
    for a in facts.attempts:
        by_task.setdefault(a["task_id"], []).append(a)
    obs_by_attempt: dict[str, list[dict[str, Any]]] = {}
    for o in facts.observations:
        obs_by_attempt.setdefault(o["attempt_id"], []).append(o)

    def mix(attempts: list[dict[str, Any]]) -> dict[str, Quantity]:
        counts: dict[str, int] = {}
        for a in attempts:
            counts[a["state"]] = counts.get(a["state"], 0) + 1
        return {s: _count(n) for s, n in sorted(counts.items())}

    def usage_of(attempts: list[dict[str, Any]]) -> dict[str, Quantity]:
        return _usage([o for a in attempts for o in obs_by_attempt.get(a["attempt_id"], ())], len(attempts))

    tasks = []
    for task_id in sorted(by_task):
        if after is not None and task_id <= after:
            continue
        group = by_task[task_id]
        tasks.append(TaskAnalytics(task_id, _count(len(group)), _count(len(group) - 1), mix(group), usage_of(group)))
    page = tuple(tasks[:limit])
    next_cursor = encode_cursor(_CURSOR_MARK, page[-1].task_id) if len(tasks) > limit else None

    total_attempts = len(facts.attempts)
    estimated: dict[str, dict[str, Quantity]] = {}
    for method in sorted({e["method"] for e in facts.estimates}):
        rows = [e for e in facts.estimates if e["method"] == method]
        estimated[method] = {"est_tokens": _total([e["est_tokens"] for e in rows], "est_tokens", "est_tokens",
                                                  len({e["attempt_id"] for e in rows}), total_attempts,
                                                  Kind.ESTIMATED, method)}

    per_profile = None
    truncated = False
    if facts.profiles_known:
        groups: dict[str, list[dict[str, Any]]] = {}
        for a in facts.attempts:
            groups.setdefault(a["profile_id"] or "<unknown>", []).append(a)
        names = sorted(groups)
        truncated = len(names) > MAX_PROFILES
        per_profile = {p: ProfileAnalytics(_count(len(groups[p])), usage_of(groups[p])) for p in names[:MAX_PROFILES]}

    d = facts.deliveries
    messages = MessageAnalytics(
        sent=_count(facts.sent),
        deliveries_created=_count(len(d)) if facts.deliveries_known else _unknown_count(),
        claimed=_count(sum(1 for _, c in d if c > 0)),
        redeliveries=_count(sum(max(c - 1, 0) for _, c in d)),
        acknowledged=_count(sum(1 for s, _ in d if s == "acknowledged")),
        dead_lettered=_count(sum(1 for s, _ in d if s == "dead_letter")))
    not_repro = () if source == "tables" else NOT_REPRODUCIBLE_FROM_EVENTS
    return RunAnalytics(
        run_id=run_id, source=source, attempts=_count(total_attempts),
        retries=_count(sum(len(g) - 1 for g in by_task.values())), outcome_mix=mix(facts.attempts),
        usage=usage_of(facts.attempts), attempts_with_any_usage=len({o["attempt_id"] for o in facts.observations}),
        estimated=estimated, messages=messages, tasks=page, next_cursor=next_cursor, per_profile=per_profile,
        per_profile_truncated=truncated, not_reproducible=not_repro, replay_ignored_events=facts.ignored,
        ignored_estimate_events=facts.ignored_estimates)


def _page_args(limit: int, cursor: str | None) -> str | None:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError("Invalid page size")
    if cursor is None:
        return None
    mark, task_id = decode_cursor(cursor)
    if mark != _CURSOR_MARK:
        raise InvalidCursor("Invalid cursor")
    return task_id


def _begin(connection: sqlite3.Connection, run_id: str) -> None:
    validate_id(run_id)
    if connection.in_transaction:
        raise RuntimeError("Analytics query requires its own read transaction")
    connection.execute("BEGIN")
    try:
        if connection.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            raise RunNotFound("Run does not exist")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _capped(rows: list[Any], cap: int, what: str) -> list[Any]:
    if len(rows) > cap:
        raise AnalyticsTooLarge(f"Run has more than {cap} {what}; analytics refused rather than truncated")
    return rows


# ------------------------------------------------------------------ path 1: authoritative tables
def read_run_analytics(connection: sqlite3.Connection, run_id: str, *, limit: int = DEFAULT_PAGE,
                       cursor: str | None = None) -> RunAnalytics:
    """Build the view from the tables. Unknown runs raise RunNotFound; oversized runs raise AnalyticsTooLarge."""
    after = _page_args(limit, cursor)
    _begin(connection, run_id)
    try:
        attempts = _capped(connection.execute(
            "SELECT a.id,a.task_id,a.attempt_no,a.state,a.profile_id FROM task_attempts a JOIN tasks t ON t.id=a.task_id "
            "WHERE t.run_id=? ORDER BY a.task_id,a.attempt_no LIMIT ?", (run_id, MAX_ATTEMPTS + 1)).fetchall(),
            MAX_ATTEMPTS, "attempts")
        obs = _capped(connection.execute(
            "SELECT u.attempt_id,u.input_tokens,u.output_tokens,u.cache_read_tokens,u.cache_write_tokens,u.cost_usd "
            "FROM usage_observations u JOIN task_attempts a ON a.id=u.attempt_id JOIN tasks t ON t.id=a.task_id "
            "WHERE t.run_id=? ORDER BY u.observed_at,u.id LIMIT ?", (run_id, MAX_OBSERVATIONS + 1)).fetchall(),
            MAX_OBSERVATIONS, "usage observations")
        sent = connection.execute(
            "SELECT COUNT(*) FROM messages m JOIN threads th ON th.id=m.thread_id WHERE th.run_id=?",
            (run_id,)).fetchone()[0]
        deliveries = _capped(connection.execute(
            "SELECT d.state,d.attempts FROM message_deliveries d JOIN messages m ON m.id=d.message_id "
            "JOIN threads th ON th.id=m.thread_id WHERE th.run_id=? LIMIT ?", (run_id, MAX_EVENTS + 1)).fetchall(),
            MAX_EVENTS, "deliveries")
        events = _capped(connection.execute(
            "SELECT payload_json FROM events WHERE run_id=? AND type='optimization.estimated' "
            "ORDER BY sequence LIMIT ?", (run_id, MAX_EVENTS + 1)).fetchall(), MAX_EVENTS, "estimate events")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    facts = _Facts(
        attempts=[dict(attempt_id=r[0], task_id=r[1], attempt_no=r[2], state=r[3], profile_id=r[4]) for r in attempts],
        observations=[dict(attempt_id=r[0], **dict(zip(USAGE_FIELDS, r[1:]))) for r in obs],
        estimates=[], sent=sent, deliveries=[(r[0], r[1]) for r in deliveries], deliveries_known=True,
        profiles_known=True)
    # Estimates have no table yet (ADR 017 D3); the event is their only record, read identically on both paths.
    facts.estimates, facts.ignored_estimates = _estimates([r[0] for r in events], {a["attempt_id"] for a in facts.attempts})
    return _build(run_id, "tables", facts, limit, after)


def _estimates(payloads: list[str], attempt_ids: set[str]) -> tuple[list[dict[str, Any]], int]:
    out, ignored = [], 0
    for raw in payloads:
        p = _payload(raw)
        value = p.get("est_tokens")
        method = p.get("method")
        if (p.get("attempt_id") not in attempt_ids or not isinstance(method, str) or not method
                or isinstance(value, bool) or not isinstance(value, int) or value < 0):
            ignored += 1  # unattributable or malformed: counted, never summed
            continue
        out.append({"attempt_id": p["attempt_id"], "method": method, "est_tokens": value})
    return out, ignored


def _payload(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# ------------------------------------------------------------------ path 2: replay of the event log
def replay_run_analytics(connection: sqlite3.Connection, run_id: str, *, limit: int = DEFAULT_PAGE,
                         cursor: str | None = None) -> RunAnalytics:
    """Rebuild the view by folding the run's events in sequence order. See ``NOT_REPRODUCIBLE_FROM_EVENTS``."""
    after = _page_args(limit, cursor)
    _begin(connection, run_id)
    try:
        marks = ",".join("?" * len(_EVENT_TYPES))
        rows = _capped(connection.execute(
            f"SELECT type,aggregate_id,payload_json FROM events WHERE run_id=? AND type IN ({marks}) "
            "ORDER BY sequence LIMIT ?", (run_id, *_EVENT_TYPES, MAX_EVENTS + 1)).fetchall(), MAX_EVENTS, "events")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    ignored = 0
    attempts: dict[str, dict[str, Any]] = {}
    sent = 0
    deliveries: dict[str, list[Any]] = {}
    usage_payloads: list[tuple[str, dict[str, Any]]] = []
    estimate_payloads: list[str] = []
    for kind, aggregate_id, raw in rows:
        p = _payload(raw)
        if kind == "task.attempt_reserved":
            attempt_id = p.get("attempt_id")
            if not isinstance(attempt_id, str) or attempt_id in attempts:
                ignored += 1
                continue
            attempts[attempt_id] = dict(attempt_id=attempt_id, task_id=aggregate_id, attempt_no=p.get("attempt_number"),
                                        state="created", profile_id=None)
        elif kind in _STATE_EVENTS:
            if aggregate_id in attempts and isinstance(p.get("after"), str):
                attempts[aggregate_id]["state"] = p["after"]
            else:
                ignored += 1
        elif kind == "agent.usage_observed":
            usage_payloads.append((aggregate_id, p))
        elif kind == "optimization.estimated":
            estimate_payloads.append(raw)
        elif kind == "message.created":
            sent += 1
        else:  # delivery.*: the last event decides the state; claims are counted
            entry = deliveries.setdefault(aggregate_id, [None, 0])
            if kind == "delivery.claimed":
                entry[0], entry[1] = "leased", entry[1] + 1
            else:
                entry[0] = {"delivery.acknowledged": "acknowledged", "delivery.pending": "pending"}.get(kind, "dead_letter")
    observations = []
    for attempt_id, p in usage_payloads:
        if attempt_id not in attempts:
            ignored += 1
            continue
        observations.append(dict(attempt_id=attempt_id, **{n: _clean(p.get(n), n) for n in USAGE_FIELDS}))
    estimates, ignored_estimates = _estimates(estimate_payloads, set(attempts))
    facts = _Facts(
        attempts=sorted(attempts.values(), key=lambda a: (a["task_id"], a["attempt_no"] if isinstance(a["attempt_no"], int) else 0)),
        observations=observations, estimates=estimates, sent=sent, deliveries=[(s, c) for s, c in deliveries.values()],
        deliveries_known=False, profiles_known=False, ignored=ignored, ignored_estimates=ignored_estimates)
    return _build(run_id, "events", facts, limit, after)


def reproducibility_diff(tables: RunAnalytics, replay: RunAnalytics) -> list[str]:
    """Names of top-level fields that differ, ignoring those the replay declares not reproducible."""
    a, b = tables.to_dict(), replay.to_dict()
    skip = {"source", "not_reproducible", "replay_ignored_events", "per_profile", "per_profile_truncated", "messages"}
    diff = [k for k in a if k not in skip and a[k] != b[k]]
    diff += [f"messages.{k}" for k, v in a["messages"].items()
             if k != "deliveries_created" and v != b["messages"][k]]
    return diff
