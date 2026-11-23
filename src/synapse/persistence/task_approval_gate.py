"""Task-level approval gates: park a running task until its approval is decided.

Only the gated task waits; sibling tasks and the run are unaffected. A rejected,
expired or revoked approval fails the task (never auto-retried). An approved
task may return to RUNNING only when the caller supplies explicit evidence that
the execution-time binding recheck passed. That recheck (candidate hash,
operation, target, policy, expiry, principal) is NOT implemented here: see
``write_task_approval_resolution`` for the seam.
"""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from synapse.domain.approvals import ApprovalState
from synapse.domain.contracts import EventEnvelope, encode_payload, validate_id
from synapse.domain.workflow import State, StateChange
from synapse.persistence.state_store import StateConflict, _write_task_transition

_DENIED = frozenset({ApprovalState.REJECTED.value, ApprovalState.EXPIRED.value, ApprovalState.REVOKED.value})


class BindingRecheckRequired(StateConflict):
    """An approved gate cannot resume without explicit binding-recheck evidence."""


def _expired(value: object) -> bool:
    """True if the stored expiry has passed; naive or invalid values are errors."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise StateConflict("Approval expiry is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StateConflict("Approval expiry is invalid")
    return parsed <= datetime.now(timezone.utc)


_BINDING_DENIALS = frozenset({"binding_mismatch", "malformed", "wrong_run"})
_TERMINAL_RUNS = ("succeeded", "failed", "cancelled")


def _event(connection, *, task_id, run_id, kind, payload) -> None:
    event = EventEnvelope.create(aggregate_type="task", aggregate_id=task_id, event_type=kind, payload=payload)
    connection.execute(
        "INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (event.id, event.aggregate_type, task_id, run_id, kind, event.schema_version,
         encode_payload(event.payload), event.occurred_at),
    )
    connection.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))


def write_task_approval_gate(connection: sqlite3.Connection, *, task_id: str, approval_id: str,
                             expected_task_version: int, principal: str,
                             bookkeeping_attempt_id: str | None = None) -> int:
    """Park a RUNNING task behind a REQUESTED approval inside a command transaction.

    The task's attempts must already be resolved (the caller finishes the
    worker's attempt in the same transaction), so a restart has no unresolved
    attempt to reconcile or relaunch. ``bookkeeping_attempt_id`` names that wait attempt in the
    immutable ``task.approval_gated`` event so the budget check can tell it from real attempts.
    Returns the new task version.
    """
    if not connection.in_transaction:
        raise RuntimeError("Approval gate requires a command transaction")
    for value in (task_id, approval_id, principal):
        validate_id(value)
    if type(expected_task_version) is not int or expected_task_version < 0:
        raise ValueError("expected_task_version must be a nonnegative integer")
    task = connection.execute(
        "SELECT t.run_id,t.state,t.state_version,r.state FROM tasks t JOIN runs r ON r.id=t.run_id WHERE t.id=?",
        (task_id,)).fetchone()
    if task is None or task[1] != State.RUNNING or task[2] != expected_task_version:
        raise StateConflict("Task state/version mismatch")
    if task[3] != "running":
        raise StateConflict("Run is not running")
    approval = connection.execute(
        "SELECT run_id,state,expires_at FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
    if approval is None or approval[0] != task[0]:
        raise StateConflict("Approval does not exist for this run")
    if approval[1] != ApprovalState.REQUESTED.value:
        raise StateConflict("Approval is not pending")
    if _expired(approval[2]):
        raise StateConflict("Approval has expired")
    if connection.execute(
        "SELECT 1 FROM task_attempts WHERE task_id=? AND state NOT IN ('completed','failed','cancelled') LIMIT 1",
        (task_id,)).fetchone():
        raise StateConflict("Task has an unresolved attempt")
    if bookkeeping_attempt_id is not None:
        validate_id(bookkeeping_attempt_id)
        # It must be the wait attempt itself: completed, the task's newest attempt, and no provider usage.
        if connection.execute(
            "SELECT 1 FROM task_attempts a WHERE a.id=? AND a.task_id=? AND a.state='completed' "
            "AND a.attempt_no=(SELECT MAX(attempt_no) FROM task_attempts WHERE task_id=a.task_id) "
            "AND NOT EXISTS(SELECT 1 FROM usage_observations u WHERE u.attempt_id=a.id)",
            (bookkeeping_attempt_id, task_id)).fetchone() is None:
            raise StateConflict("Bookkeeping attempt is not this task's completed, usage-free newest attempt")
    now = datetime.now(timezone.utc).isoformat()
    try:
        connection.execute(
            "INSERT INTO task_approval_gates(task_id,approval_id,run_id,created_at) VALUES(?,?,?,?)",
            (task_id, approval_id, task[0], now))
    except sqlite3.IntegrityError as exc:
        raise StateConflict("Task or approval is already gated") from exc
    version = _write_task_transition(
        connection, StateChange(task_id, State.RUNNING, State.AWAITING_APPROVAL, expected_task_version),
        principal=principal)
    _event(connection, task_id=task_id, run_id=task[0], kind="task.approval_gated",
           payload={"approval_id": approval_id, "version": version, "principal": principal,
                    **({} if bookkeeping_attempt_id is None else {"bookkeeping_attempt_id": bookkeeping_attempt_id})})
    return version


def write_task_approval_resolution(connection: sqlite3.Connection, *, task_id: str,
                                   expected_task_version: int, principal: str,
                                   binding_recheck_passed: bool = False,
                                   binding_denial: str | None = None) -> State:
    """Resolve a gated task from its decided approval; returns the new task state.

    rejected/expired/revoked -> FAILED. approved -> RUNNING only if
    ``binding_recheck_passed is True`` (SEAM: the caller, not this module,
    performs the execution-time binding recheck and vouches for it). An
    approved request whose expiry has lapsed fails closed, as does a
    still-requested one past its expiry. Resuming requires a running run; failing
    is allowed in any non-terminal run state. The gate is consumed (resolved_at)
    and can never be resolved again. A pending approval
    leaves the task parked (StateConflict).

    ``binding_denial`` (a ``DenialReason`` value from the execution-time
    recheck) applies only to an approved, unexpired approval: it records an
    immutable denial row (migration 0013) and fails the task. It is ignored
    for other approval states, whose own outcome already decides the task.
    It cannot be combined with ``binding_recheck_passed``.
    """
    if not connection.in_transaction:
        raise RuntimeError("Approval gate requires a command transaction")
    for value in (task_id, principal):
        validate_id(value)
    if type(expected_task_version) is not int or expected_task_version < 0:
        raise ValueError("expected_task_version must be a nonnegative integer")
    if binding_denial is not None and (binding_denial not in _BINDING_DENIALS or binding_recheck_passed):
        raise ValueError("invalid binding denial")
    row = connection.execute(
        "SELECT t.run_id,t.state,t.state_version,g.approval_id,a.state,a.expires_at,r.state,g.resolved_at "
        "FROM tasks t JOIN task_approval_gates g ON g.task_id=t.id "
        "JOIN approval_requests a ON a.id=g.approval_id JOIN runs r ON r.id=t.run_id WHERE t.id=?",
        (task_id,)).fetchone()
    if row is None or row[1] != State.AWAITING_APPROVAL or row[2] != expected_task_version:
        raise StateConflict("Task state/version mismatch")
    run_id, _, _, approval_id, approval_state, expires_at, run_state, resolved_at = row
    if resolved_at is not None:
        raise StateConflict("Approval gate is already resolved")
    if run_state in _TERMINAL_RUNS:
        raise StateConflict("Run is terminal")
    if approval_state == ApprovalState.REQUESTED.value:
        if not _expired(expires_at):
            raise StateConflict("Approval is still pending")
        target, reason = State.FAILED, "requested_lapsed"
    elif approval_state in _DENIED:
        target, reason = State.FAILED, approval_state
    elif approval_state == ApprovalState.APPROVED.value:
        if _expired(expires_at):
            target, reason = State.FAILED, "approved_lapsed"
        elif binding_denial is not None:
            connection.execute(
                "INSERT INTO task_gate_binding_denials(task_id,approval_id,reason,recorded_at) VALUES(?,?,?,?)",
                (task_id, approval_id, binding_denial, datetime.now(timezone.utc).isoformat()))
            target, reason = State.FAILED, binding_denial
        elif run_state != "running":
            raise StateConflict("Run is not running; task cannot resume")
        elif binding_recheck_passed is not True:
            raise BindingRecheckRequired("Approved task requires explicit binding recheck evidence")
        else:
            target, reason = State.RUNNING, "approved"
    else:  # pragma: no cover - schema states are exhaustive
        raise StateConflict("Unknown approval state")
    version = _write_task_transition(
        connection, StateChange(task_id, State.AWAITING_APPROVAL, target, expected_task_version),
        principal=principal)
    consumed = connection.execute(
        "UPDATE task_approval_gates SET resolved_at=? WHERE task_id=? AND resolved_at IS NULL",
        (datetime.now(timezone.utc).isoformat(), task_id))
    if consumed.rowcount != 1:
        raise StateConflict("Approval gate is already resolved")
    _event(connection, task_id=task_id, run_id=run_id, kind="task.approval_resolved",
           payload={"approval_id": approval_id, "outcome": reason, "after": target.value,
                    "version": version, "principal": principal})
    return target


def _own_transaction(connection, function, **kwargs):
    if connection.in_transaction:
        raise RuntimeError("Nested transaction is not supported")
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = function(connection, **kwargs)
        connection.execute("COMMIT")
        return result
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def park_task_for_approval(connection: sqlite3.Connection, **kwargs) -> int:
    """Autocommit-connection wrapper owning a BEGIN IMMEDIATE transaction."""
    return _own_transaction(connection, write_task_approval_gate, **kwargs)


def resolve_task_approval_gate(connection: sqlite3.Connection, **kwargs) -> State:
    """Autocommit-connection wrapper owning a BEGIN IMMEDIATE transaction."""
    return _own_transaction(connection, write_task_approval_resolution, **kwargs)
