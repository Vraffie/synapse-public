"""Daemon-owned, transactional task and run context revisions."""
import sqlite3
from uuid import uuid4

from synapse.domain.contracts import EventEnvelope, encode_payload, validate_id
from synapse.persistence.idempotency import CommandResult, execute_idempotent
from synapse.persistence.state_store import StateConflict


def write_context(
    connection: sqlite3.Connection,
    *,
    scope: str,
    scope_id: str,
    key: str,
    expected_version: int,
    content: dict,
    principal: str,
) -> int:
    """Write a context revision in the caller's command transaction.

    The application must authorize scope and principal before calling. Version
    zero creates a document; later writes use compare-and-swap.
    """
    if not connection.in_transaction:
        raise RuntimeError("Context write requires a command transaction")
    if scope not in ("task", "run"):
        raise ValueError("Only task and run context can be written directly")
    for value in (scope_id, key, principal):
        validate_id(value)
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("expected_version must be a nonnegative integer")
    if not isinstance(content, dict):
        raise TypeError("content must be an object")

    target = connection.execute(
        "SELECT id FROM runs WHERE id=?" if scope == "run" else "SELECT run_id FROM tasks WHERE id=?",
        (scope_id,),
    ).fetchone()
    if target is None:
        raise ValueError("Context scope does not exist")
    row = connection.execute(
        "SELECT id, version FROM context_documents WHERE scope=? AND scope_id=? AND context_key=?",
        (scope, scope_id, key),
    ).fetchone()
    if expected_version == 0:
        if row is not None:
            raise StateConflict("Context version mismatch")
        document_id = str(uuid4())
    else:
        if row is None or row[1] != expected_version:
            raise StateConflict("Context version mismatch")
        document_id = row[0]
    event = EventEnvelope.create(
        aggregate_type="context", aggregate_id=document_id,
        event_type="context.updated",
        payload={"scope": scope, "key": key, "version": expected_version + 1, "content": content},
    )
    serialized = encode_payload(event.payload["content"])
    if expected_version == 0:
        connection.execute(
            "INSERT INTO context_documents(id,scope,scope_id,context_key,version,content_json,updated_by,updated_at) "
            "VALUES(?,?,?,?,1,?,?,?)",
            (document_id, scope, scope_id, key, serialized, principal, event.occurred_at),
        )
    else:
        updated = connection.execute(
            "UPDATE context_documents SET version=version+1,content_json=?,updated_by=?,updated_at=? "
            "WHERE id=? AND version=?",
            (serialized, principal, event.occurred_at, document_id, expected_version),
        )
        if updated.rowcount != 1:
            raise StateConflict("Context version mismatch")
    version = expected_version + 1
    connection.execute(
        "INSERT INTO context_revisions(document_id,version,content_json,updated_by,updated_at) VALUES(?,?,?,?,?)",
        (document_id, version, serialized, principal, event.occurred_at),
    )
    connection.execute(
        "INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (event.id, event.aggregate_type, event.aggregate_id, target[0], event.event_type,
         event.schema_version, encode_payload(event.payload), event.occurred_at),
    )
    connection.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
    return version



def write_context_idempotently(
    connection: sqlite3.Connection,
    *,
    scope: str,
    scope_id: str,
    key: str,
    expected_version: int,
    content: dict,
    principal: str,
    idempotency_key: str,
) -> CommandResult:
    """Write or replay a context command with every request field bound."""
    request = {
        "scope": scope, "scope_id": scope_id, "key": key,
        "expected_version": expected_version, "content": content,
    }
    return execute_idempotent(
        connection, principal=principal, operation="context.write",
        key=idempotency_key, request=request,
        execute=lambda conn: write_context(
            conn, scope=scope, scope_id=scope_id, key=key,
            expected_version=expected_version, content=content, principal=principal,
        ),
    )
