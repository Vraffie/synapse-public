"""Daemon-owned run transitions and transactional lifecycle events."""
import sqlite3

from synapse.domain.contracts import EventEnvelope, encode_payload
from synapse.domain.runs import RunChange
from synapse.persistence.state_store import StateConflict


def write_run_transition(
    connection: sqlite3.Connection,
    change: RunChange,
    *,
    principal: str,
) -> int:
    """Write a run change in the caller's command transaction."""
    if not connection.in_transaction:
        raise RuntimeError("Run transition requires a command transaction")
    if not isinstance(principal, str) or not principal.strip():
        raise ValueError("principal required")

    version = change.expected_version + 1
    payload = {
        "before": change.before.value, "after": change.after.value,
        "version": version, "principal": principal,
    }
    event = EventEnvelope.create(
        aggregate_type="run", aggregate_id=change.run_id,
        event_type="run.state_changed", payload=payload,
    )
    updated = connection.execute(
        "UPDATE runs SET state=?, state_version=?, updated_at=? "
        "WHERE id=? AND state=? AND state_version=?",
        (change.after.value, version, event.occurred_at,
         change.run_id, change.before.value, change.expected_version),
    )
    if updated.rowcount != 1:
        raise StateConflict("Run state/version mismatch")
    connection.execute(
        "INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,"
        "schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
        (event.id, event.aggregate_type, event.aggregate_id, change.run_id,
         event.event_type, event.schema_version, encode_payload(event.payload), event.occurred_at),
    )
    connection.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
    return version
