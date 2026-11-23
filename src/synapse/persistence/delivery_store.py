"""Daemon-owned recipient delivery leases.

Application services must authenticate and authorize the recipient before using
these primitives. Callers own the surrounding transaction.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3
from uuid import uuid4

from synapse.domain.contracts import EventEnvelope, encode_payload, validate_id
from synapse.persistence.idempotency import CommandResult, execute_idempotent


@dataclass(frozen=True, slots=True)
class DeliveryLease:
    delivery_id: str
    message_id: str
    token: str
    lease_until: str
    attempt: int


class LeaseConflict(RuntimeError):
    """The delivery lease is missing, expired, or no longer owned."""


def _now(value: datetime | None) -> datetime:
    value = value if value is not None else datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _event(connection: sqlite3.Connection, delivery_id: str, run_id: str, kind: str) -> None:
    event = EventEnvelope.create(
        aggregate_type="delivery", aggregate_id=delivery_id,
        event_type=kind, payload={"delivery_id": delivery_id},
    )
    connection.execute(
        "INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (event.id, event.aggregate_type, delivery_id, run_id, event.event_type,
         event.schema_version, encode_payload(event.payload), event.occurred_at),
    )
    connection.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))


def claim_deliveries(
    connection: sqlite3.Connection,
    *,
    recipient: str,
    limit: int = 100,
    lease_seconds: int = 60,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> list[DeliveryLease]:
    """Lease pending or expired deliveries and dead-letter exhausted records.

    Open the caller-owned transaction with BEGIN IMMEDIATE to serialize
    competing claimers. Delivery order follows durable message insertion order.
    """
    if not connection.in_transaction:
        raise RuntimeError("Delivery claim requires a command transaction")
    validate_id(recipient)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if type(lease_seconds) is not int or lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    instant = _now(now)
    timestamp = instant.isoformat()
    expires = (instant + timedelta(seconds=lease_seconds)).isoformat()
    # Process a bounded batch of exhausted records separately so they cannot
    # consume the claim limit and hide eligible work behind them.
    exhausted = connection.execute(
        "SELECT d.id,t.run_id FROM message_deliveries d "
        "JOIN messages m ON m.id=d.message_id JOIN threads t ON t.id=m.thread_id "
        "WHERE d.recipient_instance_id=? AND d.attempts>=? AND "
        "(d.state='pending' OR (d.state='leased' AND d.lease_until<=?)) "
        "ORDER BY m.rowid,d.id LIMIT ?",
        (recipient, max_attempts, timestamp, limit),
    ).fetchall()
    for delivery_id, run_id in exhausted:
        connection.execute(
            "UPDATE message_deliveries SET state='dead_letter',lease_owner=NULL,lease_until=NULL WHERE id=?",
            (delivery_id,),
        )
        _event(connection, delivery_id, run_id, "delivery.dead_lettered")
    rows = connection.execute(
        "SELECT d.id,d.message_id,d.attempts,t.run_id FROM message_deliveries d "
        "JOIN messages m ON m.id=d.message_id JOIN threads t ON t.id=m.thread_id "
        "WHERE d.recipient_instance_id=? AND d.attempts<? AND "
        "(d.state='pending' OR (d.state='leased' AND d.lease_until<=?)) "
        "ORDER BY m.rowid,d.id LIMIT ?",
        (recipient, max_attempts, timestamp, limit),
    ).fetchall()
    leases = []
    for delivery_id, message_id, attempts, run_id in rows:
        token = str(uuid4())
        connection.execute(
            "UPDATE message_deliveries SET state='leased',attempts=attempts+1,lease_owner=?,lease_until=? WHERE id=?",
            (token, expires, delivery_id),
        )
        _event(connection, delivery_id, run_id, "delivery.claimed")
        leases.append(DeliveryLease(delivery_id, message_id, token, expires, attempts + 1))
    return leases


def finish_delivery(
    connection: sqlite3.Connection,
    *,
    recipient: str,
    delivery_id: str,
    token: str,
    success: bool,
    result_artifact_id: str | None = None,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> None:
    """ACK or NACK a current lease; stale tokens cannot change delivery state."""
    if not connection.in_transaction:
        raise RuntimeError("Delivery completion requires a command transaction")
    for value in (recipient, delivery_id, token):
        validate_id(value)
    if type(success) is not bool:
        raise ValueError("success must be a boolean")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if success:
        validate_id(result_artifact_id)
    elif result_artifact_id is not None:
        raise ValueError("NACK cannot include a result artifact")
    timestamp = _now(now).isoformat()
    row = connection.execute(
        "SELECT d.attempts,t.run_id FROM message_deliveries d "
        "JOIN messages m ON m.id=d.message_id JOIN threads t ON t.id=m.thread_id "
        "WHERE d.id=? AND d.recipient_instance_id=? AND d.state='leased' "
        "AND d.lease_owner=? AND d.lease_until>?",
        (delivery_id, recipient, token, timestamp),
    ).fetchone()
    if row is None:
        raise LeaseConflict("Delivery lease is not current")
    if success:
        artifact = connection.execute("SELECT id FROM artifacts WHERE id=?", (result_artifact_id,)).fetchone()
        if artifact is None:
            raise ValueError("Result artifact does not exist")
        state = "acknowledged"
    else:
        state = "dead_letter" if row[0] >= max_attempts else "pending"
    connection.execute(
        "UPDATE message_deliveries SET state=?,lease_owner=NULL,lease_until=NULL,acknowledged_at=?,result_artifact_id=? WHERE id=?",
        (state, timestamp if success else None, result_artifact_id, delivery_id),
    )
    _event(connection, delivery_id, row[1], "delivery." + state)


def complete_delivery_idempotently(
    connection: sqlite3.Connection,
    *,
    principal: str,
    idempotency_key: str,
    recipient: str,
    delivery_id: str,
    token: str,
    success: bool,
    result_artifact_id: str | None = None,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> CommandResult:
    """Commit a delivery completion and replay its durable response on retry.

    The caller authenticates the principal and authorizes the recipient and
    result artifact before every call, including a replay. The server time is
    not part of the request hash; replays return the original response without
    checking the lease expiry again.
    """
    request = {
        "recipient": recipient,
        "delivery_id": delivery_id,
        "lease_token": token,
        "success": success,
        "result_artifact_id": result_artifact_id,
        "max_attempts": max_attempts,
    }

    def complete(conn: sqlite3.Connection) -> dict[str, str]:
        finish_delivery(
            conn, recipient=recipient, delivery_id=delivery_id, token=token,
            success=success, result_artifact_id=result_artifact_id,
            max_attempts=max_attempts, now=now,
        )
        state = conn.execute(
            "SELECT state FROM message_deliveries WHERE id=?", (delivery_id,)
        ).fetchone()[0]
        return {"delivery_id": delivery_id, "state": state}

    return execute_idempotent(
        connection, principal=principal, operation="delivery.complete",
        key=idempotency_key, request=request, execute=complete,
    )


def claim_deliveries_idempotently(
    connection: sqlite3.Connection,
    *, principal: str, idempotency_key: str, recipient: str, limit: int = 100,
    lease_seconds: int = 60, max_attempts: int = 3, now: datetime | None = None,
) -> CommandResult:
    """Atomically reserve leases and their replay response for one session key.

    A replay is valid only while every returned lease is still current. An empty
    page replays empty; callers use a new key for each distinct poll.
    """
    validate_id(idempotency_key)
    request = {'recipient': recipient, 'limit': limit, 'lease_seconds': lease_seconds,
               'max_attempts': max_attempts}

    def claim(conn):
        return [dict(delivery_id=lease.delivery_id, message_id=lease.message_id,
                     token=lease.token, lease_until=lease.lease_until, attempt=lease.attempt)
                for lease in claim_deliveries(conn, recipient=recipient, limit=limit,
                                               lease_seconds=lease_seconds,
                                               max_attempts=max_attempts, now=now)]

    result = execute_idempotent(connection, principal=principal, operation='delivery.claim',
                                key=idempotency_key, request=request, execute=claim)
    if result.replayed:
        instant = _now(now)
        for lease in result.response:
            row = connection.execute(
                "SELECT state,lease_owner,lease_until FROM message_deliveries "
                "WHERE id=? AND message_id=? AND recipient_instance_id=?",
                (lease['delivery_id'], lease['message_id'], recipient),
            ).fetchone()
            if (row is None or row[0] != 'leased' or row[1] != lease['token']
                    or row[2] != lease['lease_until'] or _now(datetime.fromisoformat(row[2])) <= instant):
                raise LeaseConflict('Previously claimed lease is no longer current; use a new poll key')
    return result
