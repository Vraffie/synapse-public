from datetime import datetime, timedelta, timezone

import pytest

from synapse.application.messaging import AgentIdentity, MessageAccessDenied, MessageService
from synapse.domain.contracts import MessageType
from test_message_store import db

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)


class Authority:
    def __init__(self):
        self.identities = {
            "alice": AgentIdentity("alice", "sender", "run"),
            "bob": AgentIdentity("bob", "receiver", "run"),
            "outsider": AgentIdentity("outsider", "foreign", "other"),
        }
        self.denied = set()

    def identity(self, principal):
        return self.identities.get(principal)

    def allows(self, identity, action, resource):
        return (identity.principal, action, resource) not in self.denied


def send(service, principal="alice", **overrides):
    values = dict(principal=principal, idempotency_key="send", message_id="msg",
                  thread_id="thread", type=MessageType.QUESTION,
                  payload={"text": "hello"}, created_at=NOW.isoformat(),
                  recipients=("receiver",), artifact_refs=("artifact",))
    values.update(overrides)
    return service.send(**values)


def test_scoped_send_claim_read_and_ack_with_idempotent_replay(db):
    auth = Authority()
    service = MessageService(db, auth)
    delivery_id, = send(service).response
    assert send(service).replayed
    assert db.execute("SELECT sender_instance_id FROM messages").fetchone() == ("sender",)
    lease, = service.claim(principal="bob", now=NOW)
    message = service.read_delivery(principal="bob", delivery_id=delivery_id)
    assert message["payload"] == {"text": "hello"}
    assert message["artifact_refs"] == ["artifact"]
    kwargs = dict(principal="bob", idempotency_key="ack", delivery_id=delivery_id,
                  token=lease.token, success=True, result_artifact_id="artifact")
    result = service.complete(**kwargs, now=NOW)
    assert result.response["state"] == "acknowledged"
    assert service.complete(**kwargs, now=NOW + timedelta(days=1)).replayed


def test_forged_and_cross_run_sessions_cannot_read_or_complete(db):
    auth = Authority()
    service = MessageService(db, auth)
    delivery_id, = send(service).response
    lease, = service.claim(principal="bob", now=NOW)
    with pytest.raises(MessageAccessDenied):
        service.read_delivery(principal="outsider", delivery_id=delivery_id)
    with pytest.raises(MessageAccessDenied):
        service.complete(principal="outsider", idempotency_key="x", delivery_id=delivery_id,
                         token=lease.token, success=False, now=NOW)
    auth.identities["forged"] = AgentIdentity("alice", "sender", "run")
    with pytest.raises(MessageAccessDenied):
        send(service, principal="forged")
    auth.identities["wrong-run"] = AgentIdentity("wrong-run", "sender", "other")
    with pytest.raises(MessageAccessDenied):
        send(service, principal="wrong-run")
    assert db.execute("SELECT state FROM message_deliveries").fetchone() == ("leased",)


@pytest.mark.parametrize("action,resource", [
    ("message.send", "thread"), ("message.recipient", "receiver"),
    ("message.artifact", "artifact"),
])
def test_denied_send_never_writes(db, action, resource):
    auth = Authority()
    auth.denied.add(("alice", action, resource))
    with pytest.raises(MessageAccessDenied):
        send(MessageService(db, auth))
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone() == (0,)


def test_revoked_access_blocks_replay_read_and_completion(db):
    auth = Authority()
    service = MessageService(db, auth)
    delivery_id, = send(service).response
    lease, = service.claim(principal="bob", now=NOW)
    auth.denied.add(("bob", "message.read_artifact", "artifact"))
    with pytest.raises(MessageAccessDenied):
        service.read_delivery(principal="bob", delivery_id=delivery_id)
    auth.denied.add(("bob", "message.result_artifact", "artifact"))
    with pytest.raises(MessageAccessDenied):
        service.complete(principal="bob", idempotency_key="ack", delivery_id=delivery_id,
                         token=lease.token, success=True, result_artifact_id="artifact", now=NOW)
    auth.denied.add(("alice", "message.send", "thread"))
    with pytest.raises(MessageAccessDenied):
        send(service)
    assert db.execute("SELECT state FROM message_deliveries").fetchone() == ("leased",)


def test_claim_denied_does_not_lease(db):
    auth = Authority()
    service = MessageService(db, auth)
    send(service)
    auth.denied.add(("bob", "inbox.claim", "receiver"))
    with pytest.raises(MessageAccessDenied):
        service.claim(principal="bob", now=NOW)
    assert db.execute("SELECT state,attempts FROM message_deliveries").fetchone() == ("pending", 0)


def test_cross_run_thread_and_recipient_are_rejected_even_if_policy_allows(db):
    service = MessageService(db, Authority())
    with pytest.raises(ValueError, match="belong"):
        send(service, recipients=("foreign",))
    db.execute("INSERT INTO threads VALUES('other-thread','other','subject','open','now')")
    with pytest.raises(ValueError, match="belong"):
        send(service, thread_id="other-thread")
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)


def test_ack_replay_reauthorizes_current_scope(db):
    auth = Authority()
    service = MessageService(db, auth)
    delivery_id, = send(service).response
    lease, = service.claim(principal="bob", now=NOW)
    kwargs = dict(principal="bob", idempotency_key="ack", delivery_id=delivery_id,
                  token=lease.token, success=True, result_artifact_id="artifact", now=NOW)
    service.complete(**kwargs)
    auth.denied.add(("bob", "thread.access", "thread"))
    with pytest.raises(MessageAccessDenied):
        service.complete(**kwargs)


def test_limits_enforced_before_any_write_and_exact_payload_boundary(db):
    from synapse.application.messaging import MessageLimits
    from synapse.domain.contracts import encode_payload

    payload = {"text": "å"}
    size = len(encode_payload(payload).encode("utf-8"))
    service = MessageService(db, Authority(), limits=MessageLimits(payload_bytes=size, recipients=1, artifacts=1))
    for overrides, message in (
        ({"recipients": ("receiver", "receiver")}, "Recipient limit"),
        ({"artifact_refs": ("artifact", "artifact")}, "Artifact reference limit"),
        ({"payload": {"text": "åx"}}, "payload limit"),
    ):
        with pytest.raises(ValueError, match=message):
            send(service, **overrides)
        for table in ("messages", "message_deliveries", "events", "outbox", "idempotency_keys"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    assert not send(service, payload=payload).replayed
    assert send(service, payload=payload).replayed


@pytest.mark.parametrize("values", [(0, 1, 1), (1, 0, 1), (1, 1, 0), (True, 1, 1), (1, -1, 1)])
def test_invalid_limits_rejected(values):
    from synapse.application.messaging import MessageLimits
    with pytest.raises(ValueError, match="positive integers"):
        MessageLimits(*values)
