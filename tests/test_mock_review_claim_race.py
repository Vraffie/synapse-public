import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from synapse.application.message_gateway import MessageGateway
from synapse.application.messaging import MessageAccessDenied
from synapse.application.mock_review import MockReviewHandoff
from synapse.application.session_authority import SessionAuthority
from synapse.domain.contracts import MessageEnvelope, MessageId, MessageType, RunId
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.infrastructure.daemon_database import daemon_database
from synapse.orchestration.mock_engine import MockWorkflowEngine
from synapse.persistence.message_store import send_message
from synapse.persistence.migrations import open_database
from synapse.providers.mock import MockOutcome, MockProvider, MockScenario
from test_mock_review_handoff import prepare
from test_message_store import db

ROOT = Path(__file__).resolve().parents[1]


def test_competing_sender_cannot_cause_unrelated_lease(tmp_path, monkeypatch):
    with daemon_database(tmp_path / 'state.db', ROOT / 'migrations') as db:
        run = prepare(db)
        blobs = ArtifactBlobStore(tmp_path, max_bytes=4096)
        handoff = MockReviewHandoff(db, blobs, run_id=run['run_id'], author_id='author', reviewer_id='reviewer', thread_id='thread', review=lambda *_: True)
        original = handoff._claim
        def competing(gateway, actor, delivery_id, message_id):
            other = MessageEnvelope(id=MessageId('other'), run_id=RunId(run['run_id']), thread_id='thread', sender_instance_id='author', type=MessageType.QUESTION, payload={}, created_at='2026-11-20T00:00:00Z')
            def send_competing():
                with closing(open_database(tmp_path / 'state.db')) as rival:
                    return send_message(rival, other, recipients=('reviewer',), principal='other-sender', idempotency_key='other')
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(send_competing).result(timeout=5)
            return original(gateway, actor, delivery_id, message_id)
        monkeypatch.setattr(handoff, '_claim', competing)
        async def verify(task, candidate):
            return handoff(task, candidate)
        engine = MockWorkflowEngine(db, MockProvider({'ok': MockScenario(('good',), MockOutcome.SUCCEEDED)}), blobs, verify, verifier_id='reviewer')
        with pytest.raises(RuntimeError, match='another delivery'):
            asyncio.run(engine.run(run['run_id'], 'mock', {run['tasks']['edit']: ('ok',)}))
        assert db.execute("SELECT d.state,d.attempts FROM message_deliveries d JOIN messages m ON m.id=d.message_id WHERE m.id='other'").fetchone() == ('pending', 0)
        assert db.execute("SELECT COUNT(*) FROM message_deliveries WHERE state='leased'").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM events WHERE type='delivery.claimed'").fetchone() == (0,)
        assert db.execute('SELECT state FROM tasks').fetchone() == ('blocked',)


def test_exact_claim_requires_owned_run_inbox_capability_and_message(db):
    from test_message_store import send
    # Seed via the trusted repository; returned delivery belongs to receiver.
    delivery, = send(db).response
    auth = SessionAuthority()
    permitted = auth.issue(instance_id='receiver', run_id='run', grants=[('inbox.claim', 'receiver')])
    foreign = auth.issue(instance_id='foreign', run_id='other', grants=[('inbox.claim', 'foreign')])
    denied = auth.issue(instance_id='receiver', run_id='run', grants=[])
    gateway = MessageGateway(db, auth)
    with pytest.raises(MessageAccessDenied):
        gateway.claim_exact_for_mock(foreign.secret, delivery, 'message')
    with pytest.raises(MessageAccessDenied):
        gateway.claim_exact_for_mock(denied.secret, delivery, 'message')
    with pytest.raises(RuntimeError, match='identity mismatch'):
        gateway.claim_exact_for_mock(permitted.secret, delivery, 'wrong')
    assert db.execute('SELECT state,attempts FROM message_deliveries').fetchone() == ('pending', 0)
    lease = gateway.claim_exact_for_mock(permitted.secret, delivery, 'message')
    assert lease.delivery_id == delivery and lease.message_id == 'message'
