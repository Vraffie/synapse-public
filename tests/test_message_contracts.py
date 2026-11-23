from dataclasses import FrozenInstanceError

import pytest

from synapse.domain.contracts import MessageEnvelope, MessageId, MessageType, RunId


def message(**overrides):
    values = {
        "id": MessageId("message"), "run_id": RunId("run"),
        "thread_id": "thread", "sender_instance_id": "sender",
        "type": MessageType.QUESTION, "payload": {"items": [{"value": 1}]},
        "created_at": "2026-11-19T12:00:00+00:00",
        "artifact_refs": ["artifact"],
    }
    values.update(overrides)
    return MessageEnvelope(**values)


def test_message_detaches_nested_content_and_artifact_references():
    content = {"items": [{"value": 1}]}
    refs = ["artifact"]
    envelope = message(payload=content, artifact_refs=refs)
    content["items"][0]["value"] = 2
    refs.append("other")
    assert envelope.payload["items"][0]["value"] == 1
    assert envelope.artifact_refs == ("artifact",)
    with pytest.raises(TypeError):
        envelope.payload["items"][0]["value"] = 3
    with pytest.raises(FrozenInstanceError):
        envelope.thread_id = "other"


@pytest.mark.parametrize("overrides", [
    {"id": ""}, {"run_id": " "}, {"thread_id": ""},
    {"sender_instance_id": ""}, {"type": "question"},
    {"schema_version": True}, {"schema_version": 0},
    {"created_at": "2026-11-19T12:00:00"},
    {"correlation_id": ""}, {"causation_id": ""},
    {"artifact_refs": [""]}, {"artifact_refs": "artifact"},
])
def test_message_rejects_invalid_contract_fields(overrides):
    with pytest.raises((ValueError, TypeError)):
        message(**overrides)


def test_message_rejects_non_json_payload():
    with pytest.raises(TypeError):
        message(payload={"invalid": float("nan")})



def test_message_timestamp_is_normalized_to_utc():
    first = message(created_at="2026-11-19T12:00:00+05:00")
    second = message(created_at="2026-11-19T07:00:00Z")
    assert first.created_at == second.created_at == "2026-11-19T07:00:00.000000+00:00"
