import json
from threading import Event, Thread

import pytest

from synapse.application.message_gateway import MessageGateway
from synapse.application.messaging import MessageAccessDenied
from synapse.application.session_authority import SessionAuthority
from test_message_store import db


def raw(value):
    return json.dumps(value).encode()


def setup_delivery(db, *, extra_grants=()):
    auth = SessionAuthority()
    sender = auth.issue(instance_id='sender', run_id='run', grants=[('message.send', 'thread'), ('message.recipient', 'receiver')])
    receiver = auth.issue(instance_id='receiver', run_id='run', grants=[('inbox.claim', 'receiver'), ('thread.access', 'thread'), ('message.result_artifact', 'artifact'), *extra_grants])
    gateway = MessageGateway(db, auth)
    sent = gateway.invoke(sender.secret, 'send', raw({'idempotency_key': 'send', 'message_id': 'msg', 'thread_id': 'thread', 'type': 'question', 'payload': {}, 'created_at': '2026-11-19T12:00:00Z', 'recipients': ['receiver']}))
    delivery, = sent.response
    lease, = gateway.invoke(receiver.secret, 'claim', raw({'idempotency_key': 'poll'})).response
    return auth, receiver, gateway, delivery, lease


def test_owned_grant_preserves_identity_and_idempotent_completion(db):
    auth, session, gateway, delivery, lease = setup_delivery(db)
    with pytest.raises(MessageAccessDenied):
        gateway.invoke(session.secret, 'read', raw({'delivery_id': delivery}))
    gateway.grant_owned_delivery(session.identity.principal, delivery)
    gateway.grant_owned_delivery(session.identity.principal, delivery)
    assert auth.authenticate(session.secret) == session.identity
    assert gateway.invoke(session.secret, 'read', raw({'delivery_id': delivery}))['message_id'] == 'msg'
    command = raw({'idempotency_key': 'ack', 'delivery_id': delivery, 'token': lease['token'], 'success': True, 'result_artifact_id': 'artifact'})
    first = gateway.invoke(session.secret, 'complete', command)
    second = gateway.invoke(session.secret, 'complete', command)
    assert not first.replayed and second.replayed and first.response == second.response
    assert db.execute("SELECT COUNT(*) FROM idempotency_keys WHERE principal=? AND operation='delivery.complete'", (session.identity.principal,)).fetchone() == (1,)


def test_grant_rejects_other_instance_run_or_missing_capability(db):
    auth, receiver, gateway, delivery, _ = setup_delivery(db)
    outsider = auth.issue(instance_id='foreign', run_id='other', grants=[('inbox.claim', 'foreign'), ('thread.access', 'thread')])
    wrong_run = auth.issue(instance_id='receiver', run_id='other', grants=[('inbox.claim', 'receiver'), ('thread.access', 'thread')])
    no_thread = auth.issue(instance_id='receiver', run_id='run', grants=[('inbox.claim', 'receiver')])
    for principal in (outsider.identity.principal, wrong_run.identity.principal, no_thread.identity.principal, 'missing'):
        with pytest.raises(MessageAccessDenied):
            gateway.grant_owned_delivery(principal, delivery)
    with pytest.raises(ValueError, match='Unknown operation'):
        gateway.invoke(receiver.secret, 'grant_owned_delivery', b'{}')
    assert not auth.allows(no_thread.identity, 'delivery.read', delivery)
    auth.revoke(receiver.identity.principal)
    with pytest.raises(MessageAccessDenied):
        gateway.grant_owned_delivery(receiver.identity.principal, delivery)


def test_grant_limit_is_atomic(db):
    auth, receiver, gateway, delivery, _ = setup_delivery(db, extra_grants=[('custom', f'resource-{i}') for i in range(4092)])
    with pytest.raises(ValueError, match='Too many grants'):
        gateway.grant_owned_delivery(receiver.identity.principal, delivery)
    assert not auth.allows(receiver.identity, 'delivery.read', delivery)
    assert not auth.allows(receiver.identity, 'delivery.complete', delivery)


def test_daemon_grant_operation_is_serialized_with_revocation():
    auth = SessionAuthority()
    session = auth.issue(instance_id='receiver', run_id='run', grants=[])
    entered, release, revoked = Event(), Event(), Event()
    def grant():
        with auth.daemon_operation(session.identity.principal) as identity:
            entered.set()
            assert release.wait(3)
            auth._add_delivery_grants(identity, 'delivery')
    worker = Thread(target=grant)
    worker.start()
    assert entered.wait(3)
    revoker = Thread(target=lambda: (auth.revoke(session.identity.principal), revoked.set()))
    revoker.start()
    assert not revoked.wait(0.05)
    release.set()
    worker.join(3)
    revoker.join(3)
    assert revoked.is_set()
    assert auth.authenticate(session.secret) is None
    assert not auth.allows(session.identity, 'delivery.read', 'delivery')
