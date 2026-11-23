from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from synapse.domain.contracts import MessageEnvelope, MessageId, MessageType, RunId
from synapse.persistence.dispatcher import dispatch_batch
from synapse.persistence.event_store import read_events
from synapse.persistence.message_store import send_message
from synapse.persistence.migrations import apply_migrations, open_database

ROOT = Path(__file__).resolve().parents[1]


def test_message_trace_survives_replay_and_dispatch(tmp_path):
    with closing(open_database(tmp_path / 'trace.db')) as conn:
        apply_migrations(conn, ROOT / 'migrations')
        conn.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        conn.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        conn.execute("INSERT INTO agent_definitions VALUES('agent','1',NULL,'{}')")
        for instance in ('sender', 'receiver'):
            conn.execute("INSERT INTO agent_instances(id,run_id,definition_id,definition_version,state,created_at) VALUES(?,'run','agent','1','running','now')", (instance,))
        conn.execute("INSERT INTO threads VALUES('thread','run','subject','open','now')")
        def message(mid, causation=None):
            return MessageEnvelope(id=MessageId(mid), run_id=RunId('run'), thread_id='thread',
                sender_instance_id='sender', type=MessageType.QUESTION, payload={},
                created_at='2026-11-19T12:00:00+00:00', correlation_id='conversation',
                causation_id=MessageId(causation) if causation else None)
        for item in (message('cause'), message('reply', 'cause')):
            send_message(conn, item, recipients=('receiver',), principal='sender', idempotency_key=item.id)
        replay = read_events(conn, run_id='run')
        assert [(e['correlation_id'], e['causation_id']) for e in replay] == [('conversation', None), ('conversation', 'cause')]
        seen = []
        dispatch_batch(conn, seen.append)
        assert [(e['correlation_id'], e['causation_id']) for e in seen] == [('conversation', None), ('conversation', 'cause')]


def test_upgrade_preserves_legacy_events_and_immutability(tmp_path):
    directory = tmp_path / 'migrations'
    directory.mkdir()
    for path in sorted((ROOT / 'migrations').glob('000[1-5]_*.sql')):
        (directory / path.name).write_bytes(path.read_bytes())
    with closing(open_database(tmp_path / 'upgrade.db')) as conn:
        assert apply_migrations(conn, directory) == [1, 2, 3, 4, 5]
        conn.execute("INSERT INTO events(id,aggregate_type,aggregate_id,type,schema_version,payload_json,occurred_at) VALUES('old','task','task','created',1,'{}','now')")
        conn.execute("INSERT INTO artifacts VALUES('a','hash','text/plain',0,'path','{}','now')")
        migration = ROOT / 'migrations' / '0006_event_trace_metadata.sql'
        (directory / migration.name).write_bytes(migration.read_bytes())
        assert apply_migrations(conn, directory) == [6]
        assert conn.execute('SELECT id,correlation_id,causation_id FROM events').fetchall() == [('old', None, None)]
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute("UPDATE events SET correlation_id='changed' WHERE id='old'")
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute("DELETE FROM artifacts WHERE id='a'")
        assert apply_migrations(conn, directory) == []
