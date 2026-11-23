"""M5.2 wiring for the agent engine: budgets are checked before a task is scheduled and a refusal is terminal.

The claim-time evaluation itself is ``persistence.budget_binding.check_claim_budget`` (ADR 020: measured usage
only, unknown is never zero, ``on_unknown`` decides). This module adds what the agent engine needs on top of it:

* ``agent_budget_check`` fails closed. ``bound_policy`` treats a failed read of the binding table as "no policy";
  here the binding is read strictly and any error while reading it or the usage ledger refuses the claim with the
  distinct reason ``budget_check_failed`` instead of letting the task through unbudgeted. An unbudgeted run (no
  binding row) is exactly as before: the check is a no-op.
* ``write_budget_failure`` ends a refused task ``failed`` (not ``blocked``): no attempt is reserved, nothing
  is spawned, the engine never retries a failed task, and dependents are cancelled by the engine's stranded
  logic. The state machine has no ``ready -> failed`` edge, so a task refused at claim is claimed without the
  budget hook and failed inside the same transaction (``ready -> assigned -> failed``: no attempt, no capacity
  held after commit).
* ``bound_policy_identity`` gives the version and hash of the run's bound policy, recorded with each spawn.

The event and audit rows carry the decision (scope, limit names, limit values, observed numbers, policy version)
and never the prompt, tool inputs or paths.
"""
from __future__ import annotations

import json
import sqlite3

from synapse.application.budget_policy import BudgetDecision, Scope
from synapse.domain.contracts import EventEnvelope, encode_payload
from synapse.domain.workflow import State, StateChange
from synapse.persistence.audit_chain import append_audit
from synapse.persistence.budget_binding import BudgetDenied, check_claim_budget, decision_dict, describe
from synapse.persistence.state_store import StateConflict, write_task_transition
from synapse.persistence.task_claim import claim_task

REASON_CHECK_FAILED = "budget_check_failed"


class BudgetCheckFailed(BudgetDenied):
    """The budget could not be evaluated (binding or usage unreadable): refuse, never assume within budget."""

    def __init__(self, task_id: str, policy_version: str | None, cause: BaseException):
        decision = BudgetDecision(allowed=False, reason=REASON_CHECK_FAILED, breached=(), unknown=(), exhausted=(),
                                  policy_version=policy_version, scope=Scope("run"))  # type: ignore[arg-type]
        super().__init__(decision, task_id)
        self.cause = type(cause).__name__


def bound_policy_identity(db: sqlite3.Connection, run_id: str) -> tuple[str, str] | None:
    """``(policy_version, policy_sha256)`` of the run's bound policy, ``None`` if unbudgeted. Errors propagate."""
    row = db.execute("SELECT policy_version,policy_sha256 FROM run_budget_bindings WHERE run_id=?", (run_id,)).fetchone()
    return None if row is None else (row[0], row[1])


def agent_budget_check(db: sqlite3.Connection, task_id: str, profile_id: str) -> None:
    """``claim_task`` budget hook for the agent engine: ``check_claim_budget`` plus a fail-closed read.

    Raises ``BudgetDenied`` (breach or unknown usage under a limit), ``BudgetCheckFailed`` (a ``BudgetDenied``
    with reason ``budget_check_failed``) or ``StateConflict`` (serial token-cap rule: try again next tick).
    """
    try:
        row = db.execute("SELECT b.policy_version FROM tasks t LEFT JOIN run_budget_bindings b ON b.run_id=t.run_id "
                         "WHERE t.id=?", (task_id,)).fetchone()
    except Exception as exc:
        raise BudgetCheckFailed(task_id, None, exc) from exc
    if row is None:
        raise BudgetCheckFailed(task_id, None, LookupError("task not found"))
    if row[0] is None:
        return  # unbudgeted run: unchanged behaviour
    try:
        check_claim_budget(db, task_id, profile_id)
    except (BudgetDenied, StateConflict):
        raise
    except Exception as exc:
        raise BudgetCheckFailed(task_id, row[0], exc) from exc


def write_budget_failure(db: sqlite3.Connection, *, principal: str, run_id: str, task_id: str, version: int,
                         before: State, profile_id: str, denied: BudgetDenied, max_running: int = 6) -> int:
    """Record a refused claim or gate resume and fail the task, inside the caller's transaction.

    Same signature as ``budget_binding.write_budget_block`` (the mock engine's writer). ``before`` is READY
    (claim or park refused) or RUNNING (gate resume refused, no attempt reserved). Returns the new task version.
    """
    if not db.in_transaction:
        raise RuntimeError("Recording a budget failure requires a transaction")
    if before not in (State.READY, State.RUNNING):
        raise ValueError("a budget refusal fails a ready or resumed task only")
    message = describe(denied.decision)
    details = decision_dict(denied.decision)
    details.update(task_id=task_id, profile_id=profile_id, message=message, outcome="failed")
    event = EventEnvelope.create(aggregate_type="task", aggregate_id=task_id, event_type="budget.exceeded",
                                 payload={"task_id": task_id, "reason": denied.reason, "message": message,
                                          "policy_version": denied.decision.policy_version, "outcome": "failed"})
    db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
               (event.id, event.aggregate_type, task_id, run_id, event.event_type, event.schema_version,
                encode_payload(event.payload), event.occurred_at))
    db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
    append_audit(db, principal, "budget.denied", "%s:%s" % (details["scope"]["kind"], details["scope"]["id"] or run_id),
                 "denied", None, json.dumps(details, sort_keys=True, separators=(",", ":")), event.occurred_at)
    if before == State.READY:
        # No ready -> failed edge: claim (capacity checks only, no budget) and fail in this same transaction, so
        # no slot is held after commit and no attempt exists. A capacity StateConflict rolls everything back.
        version = claim_task(db, task_id=task_id, expected_version=version, profile_id=profile_id,
                             principal=principal, max_running=max_running)
        before = State.ASSIGNED
    return write_task_transition(db, StateChange(task_id, before, State.FAILED, version), principal=principal)
