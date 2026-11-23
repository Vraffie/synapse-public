"""Single-daemon, at-least-once outbox dispatch with explicit crash semantics.

Only one dispatcher loop may own a database at a time. Multi-process claims
need a future lease migration; this implementation deliberately does not
pretend that selecting pending rows is an atomic claim.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import sqlite3

from synapse.persistence.event_store import pending_outbox, record_dispatch_result


@dataclass(frozen=True, slots=True)
class DispatchSummary:
    delivered: int
    retried: int
    missing: int


def dispatch_batch(
    connection: sqlite3.Connection,
    publish: Callable[[dict], None],
    *,
    limit: int = 100,
    retry_seconds: int = 30,
) -> DispatchSummary:
    """Publish pending events and persist acknowledgement after delivery.

    The callback must be synchronous. A callback exception causes a durable
    retry schedule and processing continues with the rest of the selected
    batch. If the process dies after publish but before ACK, the event will
    be redelivered. Consumers MUST deduplicate using the immutable event ID.

    No SQLite transaction spans a call to publish. Run this only in the
    daemon's serialized dispatcher context, never concurrently against the
    same outbox. An exception writing an ACK propagates for operator recovery.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if retry_seconds < 1:
        raise ValueError("retry_seconds must be positive")
    if connection.autocommit is not True and (
        connection.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL
        or connection.isolation_level is not None
    ):
        raise ValueError("Outbox dispatch requires an autocommit connection")
    if connection.in_transaction:
        raise RuntimeError("Cannot dispatch from inside a transaction")

    delivered = retried = missing = 0
    for event_id in pending_outbox(connection, limit=limit):
        row = connection.execute(
            "SELECT sequence,id,aggregate_type,aggregate_id,run_id,type,"
            "schema_version,payload_json,occurred_at,correlation_id,causation_id FROM events WHERE id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            # Foreign keys should prevent this; surface corruption rather
            # than silently acknowledging or dropping the outbox record.
            raise RuntimeError(f"Outbox references missing event: {event_id}")
        event = {
            "sequence": row[0], "id": row[1], "aggregate_type": row[2],
            "aggregate_id": row[3], "run_id": row[4], "type": row[5],
            "schema_version": row[6], "payload": json.loads(row[7]),
            "occurred_at": row[8], "correlation_id": row[9], "causation_id": row[10],
        }
        try:
            publish(event)
        except Exception:
            if record_dispatch_result(connection, event_id, success=False,
                                      retry_seconds=retry_seconds):
                retried += 1
            else:
                missing += 1
        else:
            if record_dispatch_result(connection, event_id, success=True):
                delivered += 1
            else:
                missing += 1
    return DispatchSummary(delivered, retried, missing)
