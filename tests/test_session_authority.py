from dataclasses import replace

import pytest

from synapse.application.session_authority import SessionAuthority


def test_credentials_bind_identity_and_exact_capabilities():
    authority = SessionAuthority()
    session = authority.issue(instance_id='agent', run_id='run', grants=[('message.send', 'thread')])
    assert len(session.secret) == 43
    assert session.secret not in repr(session)
    assert authority.authenticate(session.secret) == session.identity
    assert authority.identity(session.identity.principal) == session.identity
    assert authority.allows(session.identity, 'message.send', 'thread')
    assert not authority.allows(session.identity, 'message.send', 'another')
    assert not authority.allows(replace(session.identity, run_id='other'), 'message.send', 'thread')
    assert authority.authenticate(session.secret[:-1] + ('A' if session.secret[-1] != 'A' else 'B')) is None
    assert authority.authenticate('not-a-token') is None


def test_revocation_and_capacity():
    authority = SessionAuthority(max_sessions=1)
    first = authority.issue(instance_id='a', run_id='r', grants=[])
    with pytest.raises(RuntimeError, match='capacity'):
        authority.issue(instance_id='b', run_id='r', grants=[])
    assert authority.revoke(first.identity.principal)
    assert not authority.revoke(first.identity.principal)
    assert authority.authenticate(first.secret) is None
    assert authority.identity(first.identity.principal) is None
    assert not authority.allows(first.identity, 'inbox.claim', 'a')
    assert authority.issue(instance_id='b', run_id='r', grants=[]).identity.instance_id == 'b'


def test_rejects_invalid_limits_grants_and_wildcards():
    for count in (0, True, 4097):
        with pytest.raises(ValueError):
            SessionAuthority(max_sessions=count)
    authority = SessionAuthority()
    for grants in ([('*', 'id')], [('action', '*')], [('action',)], [('action', '')]):
        with pytest.raises(ValueError):
            authority.issue(instance_id='a', run_id='r', grants=grants)
