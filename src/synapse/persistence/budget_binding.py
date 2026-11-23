"""Budget policy wiring (ADR 020): load the operator's policy file, bind an immutable copy to a run, and check
the budget inside the ``claim_task`` transaction.

Measured usage only: totals come from ``usage_observations`` and never from an estimate. Unknown is never zero:
a terminal attempt that did not report a limited metric makes that metric unknown for its scope, and the policy's
``on_unknown`` (default ``deny``) decides. Reservations are derived, never stored: every attempt row in the scope
counts as one attempt used (in flight included) and a claim is checked against ``used + 1``. A scope with a token
cap refuses a claim while any attempt in it is unresolved (``StateConflict``: retry on the next tick), so a cap
is overshot by at most one attempt.

This slice supports attempt and token limits only. ``max_cost_usd`` (ADR 020 Q2) and ``max_wall_seconds`` (no
measured source yet) are refused when the policy file is loaded, so a limit can never silently not apply.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from synapse.application.budget_policy import BudgetDecision, BudgetPolicy, Scope, evaluate_scopes
from synapse.domain.contracts import EventEnvelope, encode_payload, validate_id
from synapse.domain.workflow import State, StateChange
from synapse.persistence.audit_chain import append_audit
from synapse.persistence.state_store import StateConflict, write_task_transition

MAX_POLICY_BYTES = 64 * 1024
_TERMINAL = ("completed", "failed", "cancelled")
_TOKEN_LIMITS = ("max_input_tokens", "max_output_tokens", "max_total_tokens")
_UNSUPPORTED = ("max_cost_usd", "max_wall_seconds")


class BudgetPolicyError(ValueError):
    """The policy file is unusable; the daemon must not start (never degrade to "no policy")."""


class BudgetDenied(Exception):
    """A hard limit refused a claim (terminal for that task until an operator acts); not a StateConflict."""

    def __init__(self, decision: BudgetDecision, task_id: str):
        self.decision, self.task_id = decision, task_id
        super().__init__("%s: %s" % (decision.reason, describe(decision)))

    @property
    def reason(self) -> str:
        return self.decision.reason


def describe(decision: BudgetDecision) -> str:
    scope = decision.scope.kind + (":" + decision.scope.id if decision.scope.id else "")
    parts = ["scope %s" % scope]
    parts += ["%s limit %s observed %s" % (b.limit, b.limit_value, b.observed) for b in decision.breached]
    if decision.unknown:
        parts.append("unreported: " + ", ".join(decision.unknown))
    return "; ".join(parts)


def decision_dict(decision: BudgetDecision) -> dict[str, Any]:
    return {"allowed": decision.allowed, "reason": decision.reason, "policy_version": decision.policy_version,
            "scope": {"kind": decision.scope.kind, "id": decision.scope.id},
            "breached": [{"limit": b.limit, "limit_value": b.limit_value, "observed": b.observed} for b in decision.breached],
            "unknown": list(decision.unknown), "exhausted": list(decision.exhausted)}


def load_budget_policy(path: Path, *, profile_ids: frozenset[str] | None = None) -> BudgetPolicy:
    """Read and strictly validate a policy file.

    File rules as for the other operator configs: user-owned regular file, no symlink, not group/world writable,
    size capped. ``profile_ids``, when given, are the profiles that exist: a ``per_profile`` key naming anything
    else is an error (a typo must not silently mean "no cap").
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise BudgetPolicyError("cannot open budget policy: %s" % exc.strerror) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise BudgetPolicyError("budget policy must be a user-owned regular file that is not group/world writable")
        if info.st_size > MAX_POLICY_BYTES:
            raise BudgetPolicyError("budget policy is too large")
        raw = os.read(fd, MAX_POLICY_BYTES + 1)
    finally:
        os.close(fd)
    try:
        policy = BudgetPolicy.from_json(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BudgetPolicyError("invalid budget policy: %s" % exc) from exc
    scoped = [("per_run", policy.per_run), ("per_task", policy.per_task),
              *(("per_profile." + p, lim) for p, lim in policy.per_profile.items())]
    for name, limits in scoped:
        unsupported = [k for k in _UNSUPPORTED if k in limits.active()]
        if unsupported:
            raise BudgetPolicyError("%s: %s is not supported yet" % (name, ", ".join(unsupported)))
    if profile_ids is not None:
        missing = sorted(set(policy.per_profile) - set(profile_ids))
        if missing:
            raise BudgetPolicyError("per_profile names unknown profiles: " + ", ".join(missing))
    return policy


def bind_run(db: sqlite3.Connection, run_id: str, policy: BudgetPolicy) -> None:
    """Store the immutable per-run copy (caller's transaction, the one that starts the run)."""
    if not db.in_transaction:
        raise RuntimeError("Binding a budget policy requires a transaction")
    text = policy.to_json()
    db.execute("INSERT INTO run_budget_bindings(run_id,policy_version,policy_sha256,policy_json,bound_at) VALUES(?,?,?,?,?)",
               (run_id, policy.version, hashlib.sha256(text.encode()).hexdigest(), text,
                datetime.now(timezone.utc).isoformat()))


def bound_policy(db: sqlite3.Connection, run_id: str) -> BudgetPolicy | None:
    """The run's bound policy, or ``None`` for an unbudgeted run (also a database older than 0016)."""
    try:
        row = db.execute("SELECT policy_json FROM run_budget_bindings WHERE run_id=?", (run_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else BudgetPolicy.from_json(row[0])


# row layout: 0 id, 1 state, 2 profile_id, 3 sum in, 4 n in, 5 sum out, 6 n out, 7 sum total, 8 n total, 9 task_id
def _scope_totals(rows: list[tuple]) -> dict[str, Any]:
    """Totals for the attempts in ``rows``: token sums over terminal attempts, unknown if any of them lacks the field."""
    terminal = [r for r in rows if r[1] in _TERMINAL]
    totals: dict[str, Any] = {"attempts": len(rows) + 1}   # +1: the attempt this claim would start
    for name, (value_idx, count_idx) in {"input_tokens": (3, 4), "output_tokens": (5, 6), "total_tokens": (7, 8)}.items():
        if any(r[count_idx] == 0 for r in terminal):
            totals[name] = None
        else:
            totals[name] = sum(r[value_idx] or 0 for r in terminal)
    return totals


def check_claim_budget(db: sqlite3.Connection, task_id: str, profile_id: str) -> None:
    """Raise ``BudgetDenied`` (or ``StateConflict`` for the serial token-cap rule) if this claim may not start.

    Runs inside ``claim_task``'s ``BEGIN IMMEDIATE`` transaction after the capacity checks, and inside the
    approval-gate resume transaction before the real attempt is reserved. A run with no binding is unbudgeted and
    the call is a no-op. The attempt park created as gate bookkeeping (identified exactly by id in the immutable
    ``task.approval_gated`` event; no provider ran, so no usage can exist) is neither an attempt used nor unknown
    usage and is left out of every scope. Every other attempt, including a real one that ran before the gate
    existed, counts. A gate recorded without that id (older database) leaves its wait attempt counted: fail closed.
    """
    validate_id(task_id)
    run_id = db.execute("SELECT run_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    policy = bound_policy(db, run_id)
    if policy is None:
        return
    rows = db.execute(
        "SELECT a.id,a.state,a.profile_id,SUM(u.input_tokens),COUNT(u.input_tokens),SUM(u.output_tokens),"
        "COUNT(u.output_tokens),SUM(u.reported_total_tokens),COUNT(u.reported_total_tokens),a.task_id "
        "FROM task_attempts a JOIN tasks t ON t.id=a.task_id LEFT JOIN usage_observations u ON u.attempt_id=a.id "
        "WHERE t.run_id=? AND NOT EXISTS(SELECT 1 FROM events e WHERE e.aggregate_id=a.task_id AND e.type='task.approval_gated' "
        "AND json_extract(e.payload_json,'$.bookkeeping_attempt_id')=a.id) "
        "GROUP BY a.id", (run_id,)).fetchall()
    scopes = [(Scope("run"), policy.per_run, rows),
              (Scope("task"), policy.per_task, [r for r in rows if r[9] == task_id]),
              (Scope("profile", profile_id), policy.per_profile.get(profile_id), [r for r in rows if r[2] == profile_id])]
    for scope, limits, scoped in scopes:
        if limits is not None and any(k in limits.active() for k in _TOKEN_LIMITS) \
                and any(r[1] not in _TERMINAL for r in scoped):
            raise StateConflict("A token-capped %s scope has an unresolved attempt" % scope.kind)
    decision = evaluate_scopes(policy, [(scope, _scope_totals(scoped)) for scope, _, scoped in scopes])
    if not decision.allowed:
        raise BudgetDenied(decision, task_id)


def write_budget_block(db: sqlite3.Connection, *, principal: str, run_id: str, task_id: str, version: int,
                       before: State, profile_id: str, denied: BudgetDenied) -> int:
    """Record a refused claim or gate resume inside the caller's transaction (ADR 020 D4).

    Nothing was assigned or reserved; the task goes ``before`` -> BLOCKED (no silent downgrade, no retry) with a
    ``budget.exceeded`` event and a ``budget.denied`` audit row naming the reason. Returns the new task version.
    """
    if not db.in_transaction:
        raise RuntimeError("Recording a budget block requires a transaction")
    details = decision_dict(denied.decision)
    details.update(task_id=task_id, profile_id=profile_id, message=describe(denied.decision))
    event = EventEnvelope.create(aggregate_type="task", aggregate_id=task_id, event_type="budget.exceeded",
                                 payload={"task_id": task_id, "reason": denied.reason, "message": describe(denied.decision)})
    db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
               (event.id, event.aggregate_type, task_id, run_id, event.event_type, event.schema_version,
                encode_payload(event.payload), event.occurred_at))
    db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
    append_audit(db, principal, "budget.denied", "%s:%s" % (details["scope"]["kind"], details["scope"]["id"] or run_id),
                 "denied", None, json.dumps(details, sort_keys=True, separators=(",", ":")), event.occurred_at)
    return write_task_transition(db, StateChange(task_id, before, State.BLOCKED, version), principal=principal)
