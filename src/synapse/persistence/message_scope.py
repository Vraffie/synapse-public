"""Daemon-only lookup of message ownership for authorization decisions."""
import sqlite3


def instance_in_run(connection: sqlite3.Connection, instance_id: str, run_id: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM agent_instances WHERE id=? AND run_id=?", (instance_id, run_id)
    ).fetchone() is not None


def delivery_scope(connection: sqlite3.Connection, delivery_id: str, recipient: str):
    """Return only a recipient-owned delivery's run and thread, or None."""
    return connection.execute(
        "SELECT t.run_id,t.id FROM message_deliveries d "
        "JOIN messages m ON m.id=d.message_id JOIN threads t ON t.id=m.thread_id "
        "WHERE d.id=? AND d.recipient_instance_id=?", (delivery_id, recipient),
    ).fetchone()


def read_owned_delivery(connection: sqlite3.Connection, delivery_id: str, recipient: str):
    """Read a delivery's envelope for its owner; authorization is still required."""
    row = connection.execute(
        "SELECT d.id,d.state,m.id,t.run_id,m.thread_id,m.sender_instance_id,m.type,"
        "m.schema_version,m.payload_json,m.created_at,m.correlation_id,m.causation_id "
        "FROM message_deliveries d JOIN messages m ON m.id=d.message_id "
        "JOIN threads t ON t.id=m.thread_id WHERE d.id=? AND d.recipient_instance_id=?",
        (delivery_id, recipient),
    ).fetchone()
    if row is None:
        return None
    artifacts = tuple(value[0] for value in connection.execute(
        "SELECT artifact_id FROM message_artifacts WHERE message_id=? ORDER BY ordinal", (row[2],)
    ))
    return row, artifacts
