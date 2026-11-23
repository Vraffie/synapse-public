from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from synapse.domain.contracts import MessageEnvelope, MessageId, MessageType, RunId
from synapse.persistence.idempotency import IdempotencyConflict
from synapse.persistence.message_store import send_message
from synapse.persistence.migrations import apply_migrations, open_database


@pytest.fixture
def db(tmp_path):
    with closing(open_database(tmp_path / "messages.db")) as conn:
        apply_migrations(conn, Path(__file__).resolve().parents[1] / "migrations")
        conn.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        conn.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        conn.execute("INSERT INTO runs VALUES('other','wf','1','created',0,'now','now')")
        conn.execute("INSERT INTO agent_definitions VALUES('agent','1',NULL,'{}')")
        for name, run in (("sender", "run"), ("receiver", "run"), ("foreign", "other")):
            conn.execute(
                "INSERT INTO agent_instances(id,run_id,definition_id,definition_version,state,created_at) "
                "VALUES(?,?,'agent','1','running','now')", (name, run)
            )
        conn.execute("INSERT INTO threads VALUES('thread','run','subject','open','now')")
        conn.execute("INSERT INTO artifacts VALUES('artifact','hash','text/plain',0,'path','{}','now')")
        yield conn


def send(db, recipients=("receiver",), artifacts=("artifact",), question="hello"):
    message = MessageEnvelope(
        id=MessageId("message"), run_id=RunId("run"), thread_id="thread",
        sender_instance_id="sender", type=MessageType.QUESTION,
        payload={"question": question}, created_at="2026-11-19T12:00:00+00:00",
        artifact_refs=artifacts,
    )
    return send_message(
        db, message, recipients=recipients, principal="sender", idempotency_key="request",
    )


def test_send_is_atomic_and_replays_delivery_ids(db):
    first = send(db)
    assert send(db).response == first.response
    assert send(db).replayed
    for table in ("messages", "message_deliveries", "message_artifacts", "events", "outbox"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM messages WHERE id='message'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM message_artifacts WHERE message_id='message'")
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        db.execute("INSERT INTO message_artifacts VALUES('message','other',1)")


def test_cross_run_recipient_rejected(db):
    with pytest.raises(ValueError, match="belong"):
        send(db, recipients=("foreign",))
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)


def test_missing_artifact_rolls_back_message_and_idempotency(db):
    with pytest.raises(sqlite3.IntegrityError):
        send(db, artifacts=("missing",))
    for table in ("messages", "message_deliveries", "events", "outbox", "idempotency_keys"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_outbox_failure_rolls_back_everything(db):
    db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="failure"):
        send(db)
    for table in ("messages", "message_artifacts", "message_deliveries", "events", "idempotency_keys"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)



def test_same_idempotency_key_with_changed_message_is_rejected(db):
    send(db)
    with pytest.raises(IdempotencyConflict):
        send(db, question="different")
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)



def test_string_recipients_are_rejected_before_any_write(db):
    with pytest.raises(TypeError, match="sequence"):
        send(db, recipients="receiver")
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone() == (0,)


def test_message_and_seal_replacement_are_rejected(db):
    send(db)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("INSERT OR REPLACE INTO message_artifact_seals VALUES('message')")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(
            "INSERT OR REPLACE INTO messages SELECT * FROM messages WHERE id='message'"
        )
    assert db.execute("SELECT COUNT(*) FROM message_artifacts").fetchone() == (1,)


def test_migration_seals_preexisting_messages(tmp_path):
    root = Path(__file__).resolve().parents[1] / "migrations"
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    for name in ("0001_initial.sql", "0002_immutable_events.sql", "0003_approval_version.sql"):
        (legacy / name).write_bytes((root / name).read_bytes())
    with closing(open_database(tmp_path / "upgrade.db")) as conn:
        apply_migrations(conn, legacy)
        conn.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        conn.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        conn.execute("INSERT INTO agent_definitions VALUES('agent','1',NULL,'{}')")
        conn.execute("INSERT INTO agent_instances(id,run_id,definition_id,definition_version,state,created_at) VALUES('sender','run','agent','1','running','now')")
        conn.execute("INSERT INTO threads VALUES('thread','run','subject','open','now')")
        conn.execute("INSERT INTO messages(id,thread_id,sender_instance_id,type,schema_version,payload_json,created_at) VALUES('old','thread','sender','question',1,'{}','now')")
        conn.execute("INSERT INTO artifacts VALUES('artifact','hash','text/plain',0,'path','{}','now')")
        (legacy / "0004_message_artifacts.sql").write_bytes((root / "0004_message_artifacts.sql").read_bytes())
        assert apply_migrations(conn, legacy) == [4]
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            conn.execute("INSERT INTO message_artifacts VALUES('old','artifact',0)")
        assert conn.execute("SELECT id FROM messages").fetchall() == [("old",)]


def test_reused_message_id_with_new_key_is_a_value_error_and_writes_nothing(db):
    send(db)
    message = MessageEnvelope(
        id=MessageId("message"), run_id=RunId("run"), thread_id="thread",
        sender_instance_id="sender", type=MessageType.QUESTION,
        payload={"question": "other"}, created_at="2026-11-19T12:00:00+00:00",
    )
    with pytest.raises(ValueError, match="already exists"):
        send_message(db, message, recipients=("receiver",), principal="sender", idempotency_key="second")
    for table in ("messages", "message_deliveries", "events", "outbox", "idempotency_keys"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (1,)
