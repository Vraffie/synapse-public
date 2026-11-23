"""Validated immutable identifiers and versioned event envelopes."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import math
import json
from types import MappingProxyType
from typing import Any, NewType
from uuid import uuid4

RunId = NewType("RunId", str)
TaskId = NewType("TaskId", str)
MessageId = NewType("MessageId", str)
ApprovalId = NewType("ApprovalId", str)


def new_id() -> str:
    return str(uuid4())


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("ID must be a nonempty string up to 256 characters")
    return value


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("payload keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError("payload values must be finite JSON values")


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def encode_payload(payload: Mapping[str, Any]) -> str:
    """Serialize an immutable JSON snapshot without changing its contents."""
    return json.dumps(payload, default=_json_mapping, allow_nan=False)


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    schema_version: int
    payload: Mapping[str, Any]
    occurred_at: str
    causation_id: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        for value in (self.id, self.aggregate_type, self.aggregate_id, self.event_type):
            validate_id(value)
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("schema_version must be a positive integer")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "payload", _freeze_json(self.payload))
        parsed = datetime.fromisoformat(self.occurred_at)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("occurred_at must include timezone")
        if self.causation_id is not None:
            validate_id(self.causation_id)
        if self.correlation_id is not None:
            validate_id(self.correlation_id)

    @classmethod
    def create(cls, *, aggregate_type: str, aggregate_id: str,
               event_type: str, payload: Mapping[str, Any],
               schema_version: int = 1, correlation_id: str | None = None,
               causation_id: str | None = None) -> "EventEnvelope":
        return cls(
            id=new_id(), aggregate_type=aggregate_type,
            aggregate_id=aggregate_id, event_type=event_type,
            schema_version=schema_version, payload=payload,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            correlation_id=correlation_id, causation_id=causation_id,
        )


class MessageType(StrEnum):
    TASK_REQUEST = "task.request"
    QUESTION = "question"
    ANSWER = "answer"
    REVIEW_REQUEST = "review.request"
    REVIEW_FINDING = "review.finding"
    CHANGE_REQUEST = "change.request"
    CHANGE_COMPLETED = "change.completed"
    VERIFICATION_RESULT = "verification.result"
    ESCALATION = "escalation"
    ACK = "ack"


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    """Immutable versioned message data; identity authorization is a service concern."""

    id: MessageId
    run_id: RunId
    thread_id: str
    sender_instance_id: str
    type: MessageType
    payload: Mapping[str, Any]
    created_at: str
    schema_version: int = 1
    correlation_id: str | None = None
    causation_id: MessageId | None = None
    artifact_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value in (self.id, self.run_id, self.thread_id, self.sender_instance_id):
            validate_id(value)
        if not isinstance(self.type, MessageType):
            raise ValueError("type must be a supported message type")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("schema_version must be a positive integer")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "payload", _freeze_json(self.payload))
        parsed = datetime.fromisoformat(self.created_at)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("created_at must include timezone")
        object.__setattr__(self, "created_at", parsed.astimezone(timezone.utc).isoformat(timespec="microseconds"))
        for value in (self.correlation_id, self.causation_id):
            if value is not None:
                validate_id(value)
        if not isinstance(self.artifact_refs, (tuple, list)):
            raise TypeError("artifact_refs must be a sequence of identifiers")
        for value in self.artifact_refs:
            validate_id(value)
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))
