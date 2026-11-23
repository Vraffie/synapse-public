from dataclasses import FrozenInstanceError
import pytest

from synapse.domain.contracts import EventEnvelope, new_id, validate_id


def test_new_ids_are_distinct():
    assert new_id() != new_id()


def test_event_factory_has_utc_timestamp_and_schema():
    event = EventEnvelope.create(
        aggregate_type="task", aggregate_id="task-1",
        event_type="task.created", payload={"state": "created"}
    )
    assert event.schema_version == 1
    assert event.occurred_at.endswith("+00:00")
    with pytest.raises(FrozenInstanceError):
        event.id = "another"


def test_rejects_naive_timestamps_and_invalid_schema():
    event = EventEnvelope.create(
        aggregate_type="run", aggregate_id="run-1",
        event_type="run.created", payload={}
    )
    with pytest.raises(ValueError, match="timezone"):
        EventEnvelope(event.id, event.aggregate_type, event.aggregate_id,
                      event.event_type, 1, {}, "2026-01-01T00:00:00")
    with pytest.raises(ValueError, match="schema_version"):
        EventEnvelope(event.id, event.aggregate_type, event.aggregate_id,
                      event.event_type, 0, {}, event.occurred_at)


def test_rejects_invalid_ids():
    with pytest.raises(ValueError):
        validate_id("")
    with pytest.raises(ValueError):
        validate_id("a" * 257)


def test_event_payload_is_detached_and_recursively_immutable():
    data = {"updates": [{"state": "ready"}]}
    event = EventEnvelope.create(
        aggregate_type="task", aggregate_id="task-1",
        event_type="task.state_changed", payload=data,
    )
    data["updates"][0]["state"] = "cancelled"
    data["updates"].append({"state": "running"})
    assert event.payload["updates"] == ({"state": "ready"},)
    with pytest.raises(TypeError):
        event.payload["updates"][0]["state"] = "failed"
    with pytest.raises(TypeError):
        event.payload["updates"] = ()


@pytest.mark.parametrize("version", [True, 1.5, "1", None, 0, -1])
def test_schema_version_requires_positive_integer(version):
    with pytest.raises(ValueError, match="schema_version"):
        EventEnvelope.create(
            aggregate_type="task", aggregate_id="task-1",
            event_type="task.created", payload={}, schema_version=version,
        )



def test_immutable_event_payload_serializes_as_json_snapshot():
    import json

    from synapse.domain.contracts import encode_payload

    source = {"nested": [{"name": "original"}]}
    event = EventEnvelope.create(
        aggregate_type="task", aggregate_id="task-1",
        event_type="task.created", payload=source,
    )
    source["nested"][0]["name"] = "changed"
    assert json.loads(encode_payload(event.payload)) == {"nested": [{"name": "original"}]}
