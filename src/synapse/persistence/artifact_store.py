"""Daemon-owned artifact publication and metadata persistence."""
from collections.abc import Iterable
import json
import sqlite3
from typing import Any

from synapse.domain.contracts import EventEnvelope, encode_payload, validate_id
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.persistence.idempotency import (
    CommandResult, canonical_request_hash, execute_idempotent,
)


def publish_artifact(
    connection: sqlite3.Connection,
    store: ArtifactBlobStore,
    *,
    chunks: Iterable[bytes],
    artifact_id: str,
    media_type: str,
    provenance: dict[str, Any],
    principal: str,
    idempotency_key: str,
) -> CommandResult:
    """Publish bytes, then atomically persist metadata, event and response.

    The application must authenticate and authorize the principal and artifact
    access. Blob publication precedes the database transaction. A failed
    transaction may leave an unreferenced blob for later reconciliation.
    """
    if connection.in_transaction:
        raise RuntimeError("Artifact publication cannot run inside a transaction")
    for value in (artifact_id, media_type, principal, idempotency_key):
        validate_id(value)
    if not isinstance(provenance, dict):
        raise TypeError("provenance must be an object")
    canonical_request_hash(provenance)
    provenance_json = json.dumps(provenance, sort_keys=True, allow_nan=False)
    provenance_snapshot = json.loads(provenance_json)

    blob = store.put(chunks)
    request = {
        "artifact_id": artifact_id,
        "sha256": blob.sha256,
        "byte_size": blob.byte_size,
        "media_type": media_type,
        "provenance": provenance_snapshot,
    }

    def persist(conn: sqlite3.Connection) -> dict[str, Any]:
        event = EventEnvelope.create(
            aggregate_type="artifact", aggregate_id=artifact_id,
            event_type="artifact.created",
            payload={"artifact_id": artifact_id, "sha256": blob.sha256,
                     "byte_size": blob.byte_size, "media_type": media_type},
        )
        conn.execute(
            "INSERT INTO artifacts(id,sha256,media_type,byte_size,storage_path,provenance_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (artifact_id, blob.sha256, media_type, blob.byte_size, blob.sha256,
             provenance_json, event.occurred_at),
        )
        conn.execute(
            "INSERT INTO events(id,aggregate_type,aggregate_id,type,schema_version,payload_json,occurred_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (event.id, event.aggregate_type, event.aggregate_id, event.event_type,
             event.schema_version, encode_payload(event.payload), event.occurred_at),
        )
        conn.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
        return {"artifact_id": artifact_id, "sha256": blob.sha256, "byte_size": blob.byte_size}

    return execute_idempotent(
        connection, principal=principal, operation="artifact.publish",
        key=idempotency_key, request=request, execute=persist,
    )
