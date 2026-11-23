"""Durable ordered event replay and at-least-once outbox delivery primitives."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Any


def read_events(
    connection: sqlite3.Connection,
    *,
    after_sequence: int = 0,
    limit: int = 100,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read events by monotonically increasing sequence, never by timestamps."""
    if after_sequence < 0 or not 1 <= limit <= 1000:
        raise ValueError("Invalid cursor or page size")
    sql = ("SELECT sequence,id,aggregate_type,aggregate_id,run_id,type,"
           "schema_version,payload_json,occurred_at,correlation_id,causation_id FROM events WHERE sequence>?")
    params: list[Any] = [after_sequence]
    if run_id is not None:
        sql += " AND run_id=?"
        params.append(run_id)
    sql += " ORDER BY sequence ASC LIMIT ?"
    params.append(limit)
    return [
        dict(sequence=row[0], id=row[1], aggregate_type=row[2],
             aggregate_id=row[3], run_id=row[4], type=row[5],
             schema_version=row[6], payload=json.loads(row[7]), occurred_at=row[8],
             correlation_id=row[9], causation_id=row[10])
        for row in connection.execute(sql, params)
    ]


def pending_outbox(
    connection: sqlite3.Connection,
    *,
    now: str | None = None,
    limit: int = 100,
) -> list[str]:
    """Read eligible outbox event IDs; delivery is idempotent at consumers."""
    if not 1 <= limit <= 1000:
        raise ValueError("Invalid page size")
    now = now or datetime.now(timezone.utc).isoformat()
    return [
        row[0] for row in connection.execute(
            "SELECT o.event_id FROM outbox o LEFT JOIN events e ON e.id=o.event_id "
            "WHERE o.dispatched_at IS NULL AND "
            "(o.next_attempt_at IS NULL OR o.next_attempt_at<=?) "
            "ORDER BY e.sequence LIMIT ?", (now, limit)
        )
    ]


def record_dispatch_result(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    success: bool,
    retry_seconds: int = 30,
) -> bool:
    """Record success or schedule retry; caller owns external delivery.

    No external calls occur inside the SQLite transaction. Duplicate dispatch
    after crashes is possible, so subscribers MUST deduplicate by event ID.
    """
    if retry_seconds < 1:
        raise ValueError("retry_seconds must be positive")
    now = datetime.now(timezone.utc)
    if success:
        cursor = connection.execute(
            "UPDATE outbox SET dispatched_at=?,attempts=attempts+1,"
            "next_attempt_at=NULL WHERE event_id=? AND dispatched_at IS NULL",
            (now.isoformat(), event_id),
        )
    else:
        cursor = connection.execute(
            "UPDATE outbox SET attempts=attempts+1,next_attempt_at=? "
            "WHERE event_id=? AND dispatched_at IS NULL",
            ((now + timedelta(seconds=retry_seconds)).isoformat(), event_id),
        )
    return cursor.rowcount == 1
