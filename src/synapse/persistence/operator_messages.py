"""Operator-only, run-scoped message and thread queries."""
import json
import sqlite3

from synapse.domain.contracts import validate_id


def _page(after, limit):
    if type(after) is not int or not 0 <= after <= 9223372036854775807:
        raise ValueError("Invalid cursor")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid page size")


def read_threads(connection: sqlite3.Connection, run_id: str, after: int, limit: int):
    validate_id(run_id)
    _page(after, limit)
    if connection.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
        raise LookupError("Run not found")
    rows = connection.execute(
        "SELECT rowid,id,subject,status,created_at FROM threads "
        "WHERE run_id=? AND rowid>? ORDER BY rowid LIMIT ?", (run_id, after, limit)
    )
    keys = ("cursor", "id", "subject", "status", "created_at")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def read_messages(connection: sqlite3.Connection, run_id: str, thread_id: str, after: int, limit: int):
    validate_id(run_id)
    validate_id(thread_id)
    _page(after, limit)
    if connection.execute("SELECT 1 FROM threads WHERE id=? AND run_id=?", (thread_id, run_id)).fetchone() is None:
        raise LookupError("Thread not found")
    rows = connection.execute(
        "SELECT m.rowid,m.id,m.sender_instance_id,m.type,m.schema_version,m.correlation_id,"
        "m.causation_id,m.payload_json,m.created_at FROM messages m "
        "JOIN threads t ON t.id=m.thread_id WHERE t.run_id=? AND t.id=? AND m.rowid>? "
        "ORDER BY m.rowid LIMIT ?", (run_id, thread_id, after, limit)
    ).fetchall()
    result = []
    for cursor, mid, sender, kind, version, correlation, causation, payload, created in rows:
        artifacts = [row[0] for row in connection.execute(
            "SELECT artifact_id FROM message_artifacts WHERE message_id=? ORDER BY ordinal", (mid,)
        )]
        result.append({"cursor": cursor, "id": mid, "sender_instance_id": sender,
                       "type": kind, "schema_version": version, "correlation_id": correlation,
                       "causation_id": causation, "payload": json.loads(payload),
                       "created_at": created, "artifact_refs": artifacts})
    return result
