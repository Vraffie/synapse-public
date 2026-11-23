from pathlib import Path
import sqlite3

import pytest

from synapse.domain.workflow import State, StateChange
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import StateConflict, write_verified_task_transition

ROOT = Path(__file__).resolve().parents[1]


def _seed(db):
    db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
    db.execute("INSERT INTO runs VALUES('run','wf','1','running',0,'now','now')")
    db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) VALUES('mock','mock','mock','mock','now')")
    db.execute("INSERT INTO tasks(id,run_id,node_id,state,attempt_count,created_at,updated_at) VALUES('task','run','node','verifying',2,'now','now')")
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('old','task',1,'mock','completed')")
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('new','task',2,'mock','completed')")
    for artifact, attempt in (("old-artifact", "old"), ("new-artifact", "new")):
        db.execute("INSERT INTO artifacts VALUES(?,?,'text/plain',1,'path',?, 'now')", (artifact, 'a' * 64, '{"run_id":"run","task_id":"task","attempt_id":"' + attempt + '"}'))


def test_upgrade_preserves_legacy_evidence_but_cannot_use_it(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for source in sorted((ROOT / "migrations").glob("000[1-6]_*.sql")):
        (migrations / source.name).write_bytes(source.read_bytes())
    db = open_database(tmp_path / "test.db")
    try:
        assert apply_migrations(db, migrations) == [1, 2, 3, 4, 5, 6]
        _seed(db)
        db.execute("INSERT INTO verification_results(id,run_id,task_id,candidate_sha256,verifier_id,outcome,created_at) VALUES('legacy','run','task',?,'reviewer','approved','now')", ('a' * 64,))
        for version in ("0007", "0008"):
            source = next((ROOT / "migrations").glob(version + "_*.sql"))
            (migrations / source.name).write_bytes(source.read_bytes())
        assert apply_migrations(db, migrations) == [7, 8]
        assert db.execute("SELECT candidate_artifact_id,task_attempt_id FROM verification_results WHERE id='legacy'").fetchone() == (None, None)
        db.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(StateConflict):
                write_verified_task_transition(db, StateChange('task', State.VERIFYING, State.COMPLETED, 0), principal='reviewer', verification_id='legacy', artifact_id='new-artifact')
        finally:
            db.execute("ROLLBACK")
        assert db.execute("SELECT state FROM tasks").fetchone() == ('verifying',)
    finally:
        db.close()


def test_same_hash_new_attempt_cannot_reuse_old_approval(tmp_path):
    db = open_database(tmp_path / "test.db")
    try:
        apply_migrations(db, ROOT / "migrations")
        _seed(db)
        db.execute("INSERT INTO verification_results(id,run_id,task_id,candidate_sha256,verifier_id,outcome,created_at,candidate_artifact_id,task_attempt_id) VALUES('v','run','task',?,'reviewer','approved','now','old-artifact','old')", ('a' * 64,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE verification_results SET task_attempt_id='new',candidate_artifact_id='new-artifact' WHERE id='v'")
        db.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(StateConflict):
                write_verified_task_transition(db, StateChange('task', State.VERIFYING, State.COMPLETED, 0), principal='reviewer', verification_id='v', artifact_id='new-artifact')
        finally:
            db.execute("ROLLBACK")
        assert db.execute("SELECT state FROM tasks").fetchone() == ('verifying',)
    finally:
        db.close()


@pytest.mark.parametrize("recursive", [0, 1])
def test_replace_cannot_rewrite_verification_record(tmp_path, recursive):
    db = open_database(tmp_path / "test.db")
    try:
        apply_migrations(db, ROOT / "migrations")
        _seed(db)
        db.execute("PRAGMA recursive_triggers = " + str(recursive))
        db.execute("INSERT INTO verification_results(id,run_id,task_id,candidate_sha256,verifier_id,outcome,created_at,candidate_artifact_id,task_attempt_id) VALUES('v','run','task',?,'reviewer','approved','now','old-artifact','old')", ('a' * 64,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("INSERT OR REPLACE INTO verification_results(id,run_id,task_id,candidate_sha256,verifier_id,outcome,created_at,candidate_artifact_id,task_attempt_id) VALUES('v','run','task',?,'reviewer','approved','now','new-artifact','new')", ('a' * 64,))
        assert db.execute("SELECT candidate_artifact_id,task_attempt_id FROM verification_results WHERE id='v'").fetchone() == ('old-artifact', 'old')
    finally:
        db.close()
