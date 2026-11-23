"""Transactional message persistence for the daemon's trusted command path."""
import json
import sqlite3
from uuid import uuid4

from synapse.domain.contracts import EventEnvelope, MessageEnvelope, encode_payload, validate_id
from synapse.persistence.idempotency import CommandResult, execute_idempotent


def write_message(
    connection: sqlite3.Connection,
    message: MessageEnvelope,
    *,
    recipients: tuple[str, ...],
) -> tuple[str, ...]:
    """Persist a message and delivery records in the caller's transaction.

    The application authenticates the sender and authorizes every recipient and
    artifact before invoking this repository. Exceptions must roll back the command.
    """
    if not connection.in_transaction:
        raise RuntimeError("Message write requires a command transaction")
    if not isinstance(recipients, (tuple, list)):
        raise TypeError("Recipients must be a sequence of identifiers")
    if not recipients or len(set(recipients)) != len(recipients):
        raise ValueError("Recipients must be nonempty and distinct")
    for recipient in recipients:
        validate_id(recipient)
    if len(set(message.artifact_refs)) != len(message.artifact_refs):
        raise ValueError("Artifact references must be distinct")

    if connection.execute("SELECT 1 FROM messages WHERE id=?", (message.id,)).fetchone():
        # A reused message ID under a new idempotency key is a caller error, not a
        # database fault; the immutability trigger would surface it as IntegrityError.
        raise ValueError("Message ID already exists")
    thread = connection.execute(
        "SELECT run_id, status FROM threads WHERE id=?", (message.thread_id,)
    ).fetchone()
    if thread is None or thread != (message.run_id, "open"):
        raise ValueError("Thread must be open and belong to the message run")
    for instance in (message.sender_instance_id, *recipients):
        row = connection.execute("SELECT run_id FROM agent_instances WHERE id=?", (instance,)).fetchone()
        if row is None or row[0] != message.run_id:
            raise ValueError("Sender and recipients must belong to the message run")
    if message.causation_id is not None:
        cause = connection.execute(
            "SELECT thread_id FROM messages WHERE id=?", (message.causation_id,)
        ).fetchone()
        if cause is None or cause[0] != message.thread_id:
            raise ValueError("Causation must refer to a message in the same thread")

    event = EventEnvelope.create(
        aggregate_type="message", aggregate_id=message.id, event_type="message.created",
        payload={"message_id": message.id, "thread_id": message.thread_id},
        correlation_id=message.correlation_id, causation_id=message.causation_id,
    )
    connection.execute(
        "INSERT INTO messages(id,thread_id,sender_instance_id,type,schema_version,"
        "correlation_id,causation_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (message.id, message.thread_id, message.sender_instance_id, message.type.value,
         message.schema_version, message.correlation_id, message.causation_id,
         encode_payload(message.payload), message.created_at),
    )
    for ordinal, artifact_id in enumerate(message.artifact_refs):
        connection.execute(
            "INSERT INTO message_artifacts(message_id,artifact_id,ordinal) VALUES(?,?,?)",
            (message.id, artifact_id, ordinal),
        )
    connection.execute("INSERT INTO message_artifact_seals(message_id) VALUES(?)", (message.id,))
    deliveries = tuple(str(uuid4()) for _ in recipients)
    for delivery_id, recipient in zip(deliveries, recipients, strict=True):
        connection.execute(
            "INSERT INTO message_deliveries(id,message_id,recipient_instance_id) VALUES(?,?,?)",
            (delivery_id, message.id, recipient),
        )
    connection.execute(
        "INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at,correlation_id,causation_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (event.id, event.aggregate_type, event.aggregate_id, message.run_id,
         event.event_type, event.schema_version, encode_payload(event.payload), event.occurred_at,
         event.correlation_id, event.causation_id),
    )
    connection.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
    return deliveries



def send_message(
    connection: sqlite3.Connection,
    message: MessageEnvelope,
    *,
    recipients: tuple[str, ...],
    principal: str,
    idempotency_key: str,
) -> CommandResult:
    """Atomically store and replay a fully scoped message command.

    The caller must authenticate and authorize the principal before invocation.
    """
    if not isinstance(recipients, (tuple, list)):
        raise TypeError("Recipients must be a sequence of identifiers")
    request = {
        "id": message.id,
        "run_id": message.run_id,
        "thread_id": message.thread_id,
        "sender_instance_id": message.sender_instance_id,
        "type": message.type.value,
        "schema_version": message.schema_version,
        "payload": json.loads(encode_payload(message.payload)),
        "created_at": message.created_at,
        "correlation_id": message.correlation_id,
        "causation_id": message.causation_id,
        "artifact_refs": list(message.artifact_refs),
        "recipients": list(recipients),
    }
    return execute_idempotent(
        connection, principal=principal, operation="message.send",
        key=idempotency_key, request=request,
        execute=lambda conn: list(write_message(conn, message, recipients=recipients)),
    )
