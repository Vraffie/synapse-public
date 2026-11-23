"""Operator abandonment of a ``recovering`` real-agent run.

A real agent child can outlive a daemon crash, so after a restart its attempt is ``unknown`` and the
run sits in ``recovering``; nothing automatic may retry it. Abandoning ends that limbo: the run
becomes ``failed`` and its unfinished tasks ``cancelled``. Attempt rows stay ``unknown`` on purpose
(the honest record that the outcome was never established) and the worktree is kept for inspection.

The operator must assert that no agent process remains; the daemon cannot verify that (process
identity is not persisted). Nothing is re-spawned and no external effect is performed here.
"""
from __future__ import annotations

import sqlite3

from synapse.domain.approvals import ApprovalChange, ApprovalState
from synapse.domain.contracts import ApprovalId, EventEnvelope, RunId, encode_payload, validate_id
from synapse.domain.runs import RunChange, RunState
from synapse.domain.workflow import State, StateChange
from synapse.persistence.agent_tasks import is_agent_run
from synapse.persistence.approval_store import write_approval_transition
from synapse.persistence.idempotency import execute_idempotent
from synapse.persistence.run_store import write_run_transition
from synapse.persistence.state_store import StateConflict, write_task_transition
from synapse.persistence.task_approval_gate import write_task_approval_resolution

ASSERTION = "no-agent-process-remains"
EVENT_TYPE = "agent.run_abandoned"
_PRINCIPAL = "local-operator"
_DONE = (State.COMPLETED, State.FAILED, State.CANCELLED)


def abandon_agent_run(db: sqlite3.Connection, *, run_id: str, expected_run_version: int, assertion: str, key: str):
    validate_id(run_id)
    validate_id(key)
    if assertion != ASSERTION:
        raise ValueError("assertion must be exactly %r" % ASSERTION)
    if type(expected_run_version) is not int or expected_run_version < 0:
        raise ValueError("expected_run_version must be a non-negative integer")

    def write(conn):
        row = conn.execute("SELECT state,state_version FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None or not is_agent_run(conn, run_id):
            raise StateConflict("Only a recovering real-agent run can be abandoned")
        if row[0] != "recovering" or row[1] != expected_run_version:
            raise StateConflict("Run is not recovering at the expected version")
        # A task parked on an approval gate (its sibling is what left the run `recovering`) must not leave a
        # live, decidable approval behind: revoke a still-requested approval, then consume the gate through
        # its only writer, which fails the task. An already-approved gate is left to the generic cancellation
        # below (nothing can resume it: the run is ending).
        revoked = []
        for task_id, task_version, approval_id, approval_state, approval_version in conn.execute(
                "SELECT t.id,t.state_version,g.approval_id,a.state,a.state_version FROM tasks t "
                "JOIN task_approval_gates g ON g.task_id=t.id JOIN approval_requests a ON a.id=g.approval_id "
                "WHERE t.run_id=? AND t.state='awaiting_approval' AND g.resolved_at IS NULL ORDER BY t.id", (run_id,)).fetchall():
            if approval_state == ApprovalState.REQUESTED.value:
                write_approval_transition(
                    conn, ApprovalChange(ApprovalId(approval_id), ApprovalState.REQUESTED, ApprovalState.REVOKED, approval_version),
                    principal=_PRINCIPAL, reason="run abandoned by operator")
                revoked.append(approval_id)
                approval_state = ApprovalState.REVOKED.value
            if approval_state in (ApprovalState.REVOKED.value, ApprovalState.REJECTED.value, ApprovalState.EXPIRED.value):
                write_task_approval_resolution(conn, task_id=task_id, expected_task_version=task_version, principal=_PRINCIPAL)
        cancelled = []
        for task_id, state, version in conn.execute(
                "SELECT id,state,state_version FROM tasks WHERE run_id=? ORDER BY id", (run_id,)).fetchall():
            state = State(state)
            if state in _DONE:
                continue
            write_task_transition(conn, StateChange(task_id, state, State.CANCELLED, version), principal=_PRINCIPAL)
            cancelled.append(task_id)
        write_run_transition(conn, RunChange(RunId(run_id), RunState.RECOVERING, RunState.FAILED, row[1]), principal=_PRINCIPAL)
        event = EventEnvelope.create(
            aggregate_type="run", aggregate_id=run_id, event_type=EVENT_TYPE,
            payload={"run_id": run_id, "principal": _PRINCIPAL, "assertion": ASSERTION, "cancelled_tasks": cancelled, "revoked_approvals": revoked})
        conn.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                     (event.id, event.aggregate_type, run_id, run_id, event.event_type, event.schema_version,
                      encode_payload(event.payload), event.occurred_at))
        conn.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
        return {"run_id": run_id, "state": "failed", "cancelled_tasks": len(cancelled)}
    return execute_idempotent(db, principal=_PRINCIPAL, operation="agent.abandon", key=key,
                              request={"run_id": run_id, "expected_run_version": expected_run_version, "assertion": assertion},
                              execute=write)
