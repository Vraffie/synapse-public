"""Atomic mock-attempt and verification lifecycle writes, owned by the daemon."""
from __future__ import annotations

import json
import re
import sqlite3

from synapse.domain.contracts import EventEnvelope, encode_payload, new_id, validate_id
from synapse.domain.workflow import State, StateChange
from synapse.persistence.state_store import StateConflict, write_task_transition, write_verified_task_transition


def set_attempt(connection: sqlite3.Connection, *, attempt_id: str, before: str, after: str, run_id: str, task_id: str) -> None:
    if not connection.in_transaction:
        raise RuntimeError("Attempt transition requires a transaction")
    for value in (attempt_id, run_id, task_id):
        validate_id(value)
    if (before, after) not in {("created", "running"), ("running", "completed"), ("running", "failed"), ("running", "cancelled"), ("running", "unknown")}:
        raise ValueError("Invalid attempt transition")
    event = EventEnvelope.create(aggregate_type="task_attempt", aggregate_id=attempt_id,
        event_type="task_attempt.state_changed", payload={"before": before, "after": after, "task_id": task_id})
    cursor = connection.execute("UPDATE task_attempts SET state=? WHERE id=? AND task_id=? AND state=? AND EXISTS (SELECT 1 FROM tasks WHERE tasks.id=task_attempts.task_id AND tasks.run_id=?)", (after, attempt_id, task_id, before, run_id))
    if cursor.rowcount != 1:
        raise StateConflict("Attempt state mismatch")
    connection.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)", (event.id, event.aggregate_type, attempt_id, run_id, event.event_type, 1, encode_payload(event.payload), event.occurred_at))
    connection.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))


def record_verification(connection: sqlite3.Connection, *, run_id: str, task_id: str, task_version: int, candidate_sha256: str, artifact_id: str, verifier_id: str, approved: bool) -> None:
    if not connection.in_transaction:
        raise RuntimeError("Verification requires a transaction")
    for value in (run_id, task_id, artifact_id, verifier_id):
        validate_id(value)
    if not isinstance(candidate_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", candidate_sha256) is None:
        raise ValueError("candidate_sha256 must be a lowercase SHA-256 digest")
    if type(approved) is not bool:
        raise ValueError("approved must be bool")
    task = connection.execute("SELECT run_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None or task[0] != run_id:
        raise StateConflict("Task does not belong to run")
    row = connection.execute("SELECT sha256,provenance_json FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    if row is None or row[0] != candidate_sha256:
        raise StateConflict("Candidate artifact is missing or differs")
    provenance = json.loads(row[1])
    if not isinstance(provenance, dict) or provenance.get("run_id") != run_id or provenance.get("task_id") != task_id:
        raise StateConflict("Candidate artifact provenance does not match task and run")
    attempt = connection.execute("SELECT a.id FROM task_attempts a JOIN tasks t ON t.id=a.task_id WHERE t.id=? AND t.run_id=? AND a.attempt_no=t.attempt_count AND a.state='completed'", (task_id, run_id)).fetchone()
    if attempt is None or provenance.get("attempt_id") != attempt[0]:
        raise StateConflict("Candidate artifact does not belong to current completed attempt")
    result_id = new_id()
    event = EventEnvelope.create(aggregate_type="verification", aggregate_id=result_id,
        event_type="verification.recorded", payload={"task_id": task_id, "candidate_sha256": candidate_sha256, "candidate_artifact_id": artifact_id, "verifier_id": verifier_id, "approved": approved})
    connection.execute("INSERT INTO verification_results(id,run_id,task_id,candidate_sha256,verifier_id,outcome,created_at,candidate_artifact_id,task_attempt_id) VALUES(?,?,?,?,?,?,?,?,?)", (result_id, run_id, task_id, candidate_sha256, verifier_id, "approved" if approved else "changes_required", event.occurred_at, artifact_id, attempt[0]))
    connection.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)", (event.id, event.aggregate_type, result_id, run_id, event.event_type, 1, encode_payload(event.payload), event.occurred_at))
    connection.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
    if approved:
        write_verified_task_transition(connection, StateChange(task_id, State.VERIFYING, State.COMPLETED, task_version), principal=verifier_id, verification_id=result_id, artifact_id=artifact_id)
    else:
        write_task_transition(connection, StateChange(task_id, State.VERIFYING, State.CHANGES_REQUIRED, task_version), principal=verifier_id)
