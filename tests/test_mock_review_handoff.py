import asyncio
from contextlib import closing
import json
from pathlib import Path

import pytest

from synapse.application.message_gateway import MessageGateway
from synapse.application.mock_review import MockReviewHandoff
from synapse.domain.contracts import RunId
from synapse.domain.runs import RunChange, RunState
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.infrastructure.daemon_database import daemon_database
from synapse.orchestration.mock_engine import MockWorkflowEngine
from synapse.persistence.run_store import write_run_transition
from synapse.persistence.workflow_store import create_run, publish_workflow
from synapse.providers.mock import MockOutcome, MockProvider, MockScenario

ROOT = Path(__file__).resolve().parents[1]


def prepare(db):
    db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,max_concurrency,created_at) VALUES('mock','mock','mock','mock',1,'now')")
    publish_workflow(db, workflow_id='wf', version='1', nodes=[{'id': 'edit', 'dependencies': []}], principal='operator', idempotency_key='publish')
    run = create_run(db, workflow_id='wf', version='1', principal='operator', idempotency_key='run').response
    db.execute('BEGIN IMMEDIATE')
    write_run_transition(db, RunChange(RunId(run['run_id']), RunState.QUEUED, RunState.RUNNING, 1), principal='operator')
    db.execute('COMMIT')
    for agent in ('author', 'reviewer'):
        db.execute('INSERT INTO agent_definitions(id,version,definition_json) VALUES(?,\'1\',\'{}\')', (agent,))
        db.execute("INSERT INTO agent_instances(id,run_id,definition_id,definition_version,state,created_at) VALUES(?,?,?,'1','running','now')", (agent, run['run_id'], agent))
    db.execute("INSERT INTO threads(id,run_id,subject,created_at) VALUES('thread',?,'Review','now')", (run['run_id'],))
    return run


def test_review_correction_uses_durable_bidirectional_handoff_and_acks(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    seen, replays = [], []
    original_invoke = MessageGateway.invoke
    def with_duplicate_send(self, credential, operation, raw):
        result = original_invoke(self, credential, operation, raw)
        if operation == 'send':
            replay = original_invoke(self, credential, operation, raw)
            assert replay.replayed and replay.response == result.response
            replays.append(True)
        return result
    monkeypatch.setattr(MessageGateway, 'invoke', with_duplicate_send)
    with daemon_database(path, ROOT / 'migrations') as db:
        run = prepare(db)
        blobs = ArtifactBlobStore(tmp_path, max_bytes=4096)
        def review(task, candidate):
            seen.append((task, candidate))
            return candidate == b'good'
        handoff = MockReviewHandoff(db, blobs, run_id=run['run_id'], author_id='author', reviewer_id='reviewer', thread_id='thread', review=review)
        async def verify(task, candidate):
            return handoff(task, candidate)
        provider = MockProvider({'bad': MockScenario(('bad',), MockOutcome.SUCCEEDED), 'good': MockScenario(('good',), MockOutcome.SUCCEEDED)})
        engine = MockWorkflowEngine(db, provider, blobs, verify, verifier_id='reviewer')
        assert asyncio.run(engine.run(run['run_id'], 'mock', {run['tasks']['edit']: ('bad', 'good')})) == 'succeeded'
        assert [value for _, value in seen] == [b'bad', b'good']
        assert len(replays) == 4
        messages = db.execute('SELECT id,sender_instance_id,type,causation_id,correlation_id,payload_json FROM messages ORDER BY rowid').fetchall()
        assert [m[2] for m in messages] == ['review.request', 'review.finding', 'review.request', 'review.finding']
        assert [m[1] for m in messages] == ['author', 'reviewer', 'author', 'reviewer']
        for request, finding in zip(messages[::2], messages[1::2], strict=True):
            assert finding[3] == request[0] and finding[4] == request[4]
        assert [json.loads(item[5])['approved'] for item in messages[1::2]] == [False, True]
        rows = db.execute("SELECT state,result_artifact_id FROM message_deliveries ORDER BY rowid").fetchall()
        assert len(rows) == 4 and all(state == 'acknowledged' and artifact for state, artifact in rows)
        for index, (state, receipt_id) in enumerate(rows):
            digest = db.execute('SELECT sha256 FROM artifacts WHERE id=?', (receipt_id,)).fetchone()[0]
            receipt = json.loads(blobs.read(digest))
            delivery_id, message_id = db.execute('SELECT id,message_id FROM message_deliveries ORDER BY rowid LIMIT 1 OFFSET ?', (index,)).fetchone()
            assert (receipt['delivery_id'], receipt['message_id']) == (delivery_id, message_id)
            assert receipt['result_message_id'] == (messages[index + 1][0] if index % 2 == 0 else None)
        assert db.execute('SELECT outcome FROM verification_results ORDER BY rowid').fetchall() == [('changes_required',), ('approved',)]
        assert db.execute('SELECT COUNT(*) FROM events').fetchone() == db.execute('SELECT COUNT(*) FROM outbox').fetchone()
        ids = [m[0] for m in messages]
    with daemon_database(path, ROOT / 'migrations') as reopened:
        assert [row[0] for row in reopened.execute('SELECT id FROM messages ORDER BY rowid')] == ids
        assert reopened.execute("SELECT COUNT(*) FROM message_deliveries WHERE state='acknowledged'").fetchone() == (4,)
        assert reopened.execute('SELECT COUNT(*) FROM verification_results').fetchone() == (2,)


def test_interrupted_review_blocks_without_unverified_completion_or_automatic_replay(tmp_path):
    path = tmp_path / 'state.db'
    with daemon_database(path, ROOT / 'migrations') as db:
        run = prepare(db)
        blobs = ArtifactBlobStore(tmp_path, max_bytes=4096)
        def review(_task, _candidate):
            raise RuntimeError('review unavailable')
        handoff = MockReviewHandoff(db, blobs, run_id=run['run_id'], author_id='author', reviewer_id='reviewer', thread_id='thread', review=review)
        async def verify(task, candidate):
            return handoff(task, candidate)
        engine = MockWorkflowEngine(db, MockProvider({'good': MockScenario(('good',), MockOutcome.SUCCEEDED)}), blobs, verify, verifier_id='reviewer')
        with pytest.raises(RuntimeError, match='review unavailable'):
            asyncio.run(engine.run(run['run_id'], 'mock', {run['tasks']['edit']: ('good',)}))
        assert db.execute('SELECT state FROM tasks').fetchone() == ('blocked',)
        assert db.execute('SELECT COUNT(*) FROM verification_results').fetchone() == (0,)
        assert db.execute("SELECT type FROM messages").fetchall() == [('review.request',)]
        assert db.execute('SELECT state FROM message_deliveries').fetchone() == ('leased',)
    with daemon_database(path, ROOT / 'migrations') as reopened:
        assert reopened.execute('SELECT state FROM tasks').fetchone() == ('blocked',)
        assert reopened.execute('SELECT COUNT(*) FROM verification_results').fetchone() == (0,)
        assert reopened.execute('SELECT COUNT(*) FROM messages').fetchone() == (1,)


def test_rejects_self_review_and_foreign_registered_instance(tmp_path):
    with daemon_database(tmp_path / 'state.db', ROOT / 'migrations') as db:
        run = prepare(db)
        blobs = ArtifactBlobStore(tmp_path, max_bytes=4096)
        with pytest.raises(ValueError, match='distinct'):
            MockReviewHandoff(db, blobs, run_id=run['run_id'], author_id='author', reviewer_id='author', thread_id='thread', review=lambda *_: True)
        db.execute("INSERT INTO runs VALUES('other','wf','1','running',0,'now','now')")
        db.execute("UPDATE agent_instances SET run_id='other' WHERE id='reviewer'")
        handoff = MockReviewHandoff(db, blobs, run_id=run['run_id'], author_id='author', reviewer_id='reviewer', thread_id='thread', review=lambda *_: True)
        with pytest.raises(ValueError, match='belong'):
            handoff(run['tasks']['edit'], b'good')
