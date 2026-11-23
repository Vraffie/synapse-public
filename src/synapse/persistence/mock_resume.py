"""Explicit reconciliation for the built-in in-process fixed mock fixture."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

from synapse.domain.contracts import EventEnvelope, RunId, encode_payload, validate_id
from synapse.persistence.audit_chain import append_audit
from synapse.domain.runs import RunChange, RunState
from synapse.domain.workflow import State, StateChange
from synapse.persistence.idempotency import execute_idempotent
from synapse.persistence.run_store import write_run_transition
from synapse.persistence.state_store import StateConflict, write_task_transition

ASSERTION = "fixed-v1-no-external-effects"
SCENARIO_ASSERTION = "scenario-v1-no-external-effects"
_ASSERTIONS = {"fixed-v1": ASSERTION, "scenario-v1": SCENARIO_ASSERTION}
MOCK_MAX_ATTEMPTS = 2


def resume_fixed_mock(connection: sqlite3.Connection, *, run_id: str, expected_run_version: int,
                      tasks: list[dict], assertion: str, key: str, correlation_id: str,
                      no_workers_survive: bool):
    """Reconcile exactly identified blocked tasks and resume in one transaction.

    Only trusted daemon code may attest worker termination. The operator's
    assertion is deliberately limited to the immutable fixed in-process fixture.
    A replay is a receipt and MUST NOT trigger another execution.
    """
    for value in (run_id, key, correlation_id):
        validate_id(value)
    if type(expected_run_version) is not int or not 0 <= expected_run_version <= 9223372036854775807:
        raise ValueError("Invalid run version")
    if assertion not in _ASSERTIONS.values():
        raise ValueError("Explicit mock evidence is required")
    if not isinstance(tasks, list) or len(tasks) > 1000:
        raise ValueError("Invalid task evidence")
    normalized = []
    for item in tasks:
        if not isinstance(item, dict) or set(item) != {"task_id", "expected_version", "unknown_attempt_id"}:
            raise ValueError("Invalid task evidence")
        validate_id(item["task_id"])
        if type(item["expected_version"]) is not int or not 0 <= item["expected_version"] <= 9223372036854775807:
            raise ValueError("Invalid task version")
        if item["unknown_attempt_id"] is not None:
            validate_id(item["unknown_attempt_id"])
        normalized.append(dict(item))
    normalized.sort(key=lambda item: item["task_id"])
    if len({item["task_id"] for item in normalized}) != len(normalized):
        raise ValueError("Duplicate task evidence")
    request = {"run_id": run_id, "expected_run_version": expected_run_version,
               "tasks": normalized, "assertion": assertion}

    def write(db):
        if no_workers_survive is not True:
            raise StateConflict("Mock workers have not been fenced")
        if db.execute("SELECT 1 FROM mock_run_config c JOIN runs r ON r.id=c.run_id WHERE r.state='running' AND r.id<>? LIMIT 1", (run_id,)).fetchone():
            raise StateConflict("Another mock run is active")
        from synapse.persistence.mock_scenarios import KIND_PROFILES, mock_kind
        kind = mock_kind(db, run_id)
        run = db.execute("SELECT state,state_version FROM runs WHERE id=?", (run_id,)).fetchone()
        if kind is None or run != ("recovering", expected_run_version) or assertion != _ASSERTIONS[kind]:
            raise StateConflict("Run state, version or fixture differs")
        kind_profile = KIND_PROFILES[kind]
        if db.execute("SELECT 1 FROM mock_cancellation_requests WHERE run_id=?", (run_id,)).fetchone():
            raise StateConflict("Cancellation is pending")
        foreign = db.execute("SELECT 1 FROM tasks t LEFT JOIN profiles p ON p.id=t.assigned_profile_id WHERE t.run_id=? AND t.assigned_profile_id IS NOT NULL AND (t.assigned_profile_id<>? OR p.provider<>'mock' OR p.provider IS NULL) UNION ALL SELECT 1 FROM task_attempts a JOIN tasks t ON t.id=a.task_id LEFT JOIN profiles p ON p.id=a.profile_id WHERE t.run_id=? AND (a.profile_id IS NULL OR a.profile_id<>? OR p.provider<>'mock' OR p.provider IS NULL) LIMIT 1", (run_id, kind_profile, run_id, kind_profile)).fetchone()
        if foreign:
            raise StateConflict("Run contains non-fixture work")
        if db.execute("SELECT 1 FROM tasks WHERE run_id=? AND state IN ('assigned','running','verifying')", (run_id,)).fetchone():
            raise StateConflict("Run contains active tasks")
        rows = db.execute("SELECT id,state_version,attempt_count,assigned_profile_id FROM tasks WHERE run_id=? AND state='blocked' ORDER BY id", (run_id,)).fetchall()
        if [row[0] for row in rows] != [item["task_id"] for item in normalized]:
            raise StateConflict("Evidence must cover all blocked tasks")
        if not db.execute("SELECT 1 FROM tasks WHERE run_id=?", (run_id,)).fetchone():
            raise StateConflict("Run contains no tasks")
        for row, item in zip(rows, normalized, strict=True):
            task_id, version, count, profile = row
            if version != item["expected_version"] or profile != kind_profile or count >= MOCK_MAX_ATTEMPTS:
                raise StateConflict("Blocked task version, profile or retry limit differs")
            all_attempts = db.execute("SELECT a.id,a.state,a.profile_id,p.provider,a.attempt_no FROM task_attempts a LEFT JOIN profiles p ON p.id=a.profile_id WHERE a.task_id=? ORDER BY a.attempt_no", (task_id,)).fetchall()
            if any(a[2] != kind_profile or a[3] != 'mock' for a in all_attempts):
                raise StateConflict("Non-fixture attempt cannot be reconciled")
            if [a[4] for a in all_attempts] != list(range(1, count + 1)):
                raise StateConflict("Attempt history is inconsistent")
            unresolved = [a for a in all_attempts if a[1] not in ("completed", "failed", "cancelled")]
            if len(unresolved) > 1 or (unresolved and (unresolved[0][4] != count or unresolved[0][1] != "unknown" or unresolved[0][0] != item["unknown_attempt_id"])) or (not unresolved and item["unknown_attempt_id"] is not None):
                raise StateConflict("Attempt evidence differs")
            if unresolved:
                attempt_id = unresolved[0][0]
                event = EventEnvelope.create(aggregate_type="task_attempt", aggregate_id=attempt_id,
                    event_type="task_attempt.mock_reconciled", payload={"task_id": task_id, "before": "unknown", "after": "failed", "assertion": assertion, "principal": "local-operator"})
                changed = db.execute("UPDATE task_attempts SET state='failed' WHERE id=? AND task_id=? AND state='unknown'", (attempt_id, task_id))
                if changed.rowcount != 1:
                    raise StateConflict("Attempt changed")
                db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)", (event.id, event.aggregate_type, attempt_id, run_id, event.event_type, 1, encode_payload(event.payload), event.occurred_at))
                db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
            write_task_transition(db, StateChange(task_id, State.BLOCKED, State.READY, version), principal="local-operator")
        remaining = db.execute("SELECT 1 FROM task_attempts a JOIN tasks t ON t.id=a.task_id WHERE t.run_id=? AND a.state NOT IN ('completed','failed','cancelled') LIMIT 1", (run_id,)).fetchone()
        if remaining:
            raise StateConflict("Run has unresolved attempts")
        write_run_transition(db, RunChange(RunId(run_id), RunState.RECOVERING, RunState.RUNNING, expected_run_version), principal="local-operator")
        append_audit(db, "local-operator", "mock.resume", run_id, "allowed", correlation_id, json.dumps(request, sort_keys=True), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        return {"run_id": run_id, "state": "running", "assertion": assertion}
    return execute_idempotent(connection, principal="local-operator", operation="mock.resume", key=key, request=request, execute=write)
