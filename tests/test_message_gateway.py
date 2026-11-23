import json
from threading import Event, Thread

import pytest

from synapse.application.message_gateway import MessageGateway
from synapse.application.messaging import MessageAccessDenied
from synapse.application.session_authority import SessionAuthority
from test_message_store import db


def raw(value):
    return json.dumps(value).encode()


def test_derived_identity_send_claim_read_ack_and_revocation(db):
    auth = SessionAuthority()
    alice = auth.issue(instance_id='sender', run_id='run', grants=[('message.send', 'thread'), ('message.recipient', 'receiver')])
    bob = auth.issue(instance_id='receiver', run_id='run', grants=[('inbox.claim', 'receiver'), ('thread.access', 'thread')])
    gateway = MessageGateway(db, auth)
    request = dict(idempotency_key='send', message_id='msg', thread_id='thread', type='question',
                   payload={'text': 'hello'}, created_at='2026-11-19T12:00:00Z', recipients=['receiver'])
    delivery_id, = gateway.invoke(alice.secret, 'send', raw(request)).response
    lease, = gateway.invoke(bob.secret, 'claim', raw({'idempotency_key': 'poll'})).response
    # Exact delivery grants are issued by trusted daemon code after ID creation.
    auth.revoke(bob.identity.principal)
    bob = auth.issue(instance_id='receiver', run_id='run', grants=[('delivery.read', delivery_id), ('delivery.complete', delivery_id), ('thread.access', 'thread'), ('message.result_artifact', 'artifact')])
    assert gateway.invoke(bob.secret, 'read', raw({'delivery_id': delivery_id}))['payload'] == {'text': 'hello'}
    result = gateway.invoke(bob.secret, 'complete', raw({'idempotency_key': 'ack', 'delivery_id': delivery_id, 'token': lease['token'], 'success': True, 'result_artifact_id': 'artifact'}))
    assert result.response['state'] == 'acknowledged'
    assert db.execute('SELECT sender_instance_id FROM messages').fetchone() == ('sender',)
    assert auth.revoke(alice.identity.principal)
    with pytest.raises(MessageAccessDenied):
        gateway.invoke(alice.secret, 'send', raw(request))


def test_untrusted_fields_size_invalid_json_and_capability_are_rejected(db):
    auth = SessionAuthority()
    agent = auth.issue(instance_id='sender', run_id='run', grants=[])
    gateway = MessageGateway(db, auth, max_request_bytes=256)
    with pytest.raises(MessageAccessDenied):
        gateway.invoke('bad', 'claim', b'{}')
    for body in (b'{"principal":"someone"}', b'{"limit":1,"limit":2}', b'[' + b' ' * 300 + b']', b'{"limit":NaN}', b'[]', b'{"max_attempts":1000}'):
        with pytest.raises((ValueError, TypeError)):
            gateway.invoke(agent.secret, 'claim', body)
    for body in (b'{"limit":1e999}', b'{"limit":-1e999}'):
        with pytest.raises(ValueError, match="Non-finite JSON number"):
            gateway.invoke(agent.secret, 'claim', body)
    with pytest.raises(MessageAccessDenied):
        gateway.invoke(agent.secret, 'claim', raw({'idempotency_key': 'poll'}))
    assert db.execute('SELECT COUNT(*) FROM messages').fetchone() == (0,)


def test_revoke_waits_for_inflight_operation_and_blocks_next():
    auth = SessionAuthority()
    session = auth.issue(instance_id='agent', run_id='run', grants=[])
    entered, release, revoked = Event(), Event(), Event()
    def operation():
        with auth.authenticated_operation(session.secret):
            entered.set()
            assert release.wait(3)
    def revoke():
        auth.revoke(session.identity.principal)
        revoked.set()
    worker = Thread(target=operation)
    worker.start()
    assert entered.wait(3)
    revoker = Thread(target=revoke)
    revoker.start()
    assert not revoked.wait(0.05)
    release.set()
    worker.join(3)
    revoker.join(3)
    assert revoked.is_set()
    with pytest.raises(MessageAccessDenied):
        with auth.authenticated_operation(session.secret):
            pass


def test_idempotent_claim_replays_only_for_same_authorized_session(db):
    from synapse.persistence.idempotency import IdempotencyConflict

    authority = SessionAuthority()
    sender = authority.issue(instance_id='sender', run_id='run', grants=[('message.send', 'thread'), ('message.recipient', 'receiver')])
    receiver = authority.issue(instance_id='receiver', run_id='run', grants=[('inbox.claim', 'receiver')])
    gateway = MessageGateway(db, authority)
    send_request = dict(idempotency_key='send', message_id='msg', thread_id='thread', type='question',
                        payload={}, created_at='2026-11-19T12:00:00Z', recipients=['receiver'])
    gateway.invoke(sender.secret, 'send', raw(send_request))
    request = raw({'idempotency_key': 'poll', 'limit': 1})
    first = gateway.invoke(receiver.secret, 'claim', request)
    assert not first.replayed and len(first.response) == 1
    retry = gateway.invoke(receiver.secret, 'claim', request)
    assert retry.replayed and retry.response == first.response
    assert db.execute("SELECT attempts FROM message_deliveries").fetchone() == (1,)
    with pytest.raises(IdempotencyConflict):
        gateway.invoke(receiver.secret, 'claim', raw({'idempotency_key': 'poll', 'limit': 2}))
    assert authority.revoke(receiver.identity.principal)
    with pytest.raises(MessageAccessDenied):
        gateway.invoke(receiver.secret, 'claim', request)


def test_authenticated_claim_requires_key_before_mutation(db):
    authority = SessionAuthority()
    receiver = authority.issue(instance_id='receiver', run_id='run', grants=[('inbox.claim', 'receiver')])
    gateway = MessageGateway(db, authority)
    with pytest.raises(ValueError, match='Invalid request fields'):
        gateway.invoke(receiver.secret, 'claim', b'{}')
    with pytest.raises(ValueError, match='Invalid request fields'):
        gateway.invoke(receiver.secret, 'claim', raw({'limit': 10}))
    for invalid in (None, "", "  ", "x" * 257, 42):
        with pytest.raises(ValueError):
            gateway.invoke(receiver.secret, 'claim', raw({'idempotency_key': invalid}))
    assert db.execute("SELECT COUNT(*) FROM idempotency_keys WHERE operation='delivery.claim'").fetchone() == (0,)
    assert db.execute('SELECT COUNT(*) FROM message_deliveries').fetchone() == (0,)
