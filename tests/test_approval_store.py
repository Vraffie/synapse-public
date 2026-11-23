from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from synapse.domain.approvals import ApprovalChange, ApprovalState
from synapse.domain.contracts import ApprovalId
from synapse.persistence.approval_store import write_approval_request, write_approval_transition
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import StateConflict

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "a" * 64


@pytest.fixture
def db(tmp_path):
    with closing(open_database(tmp_path / "db")) as connection:
        apply_migrations(connection, ROOT / "migrations")
        connection.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        connection.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        yield connection


def request(db):
    db.execute("BEGIN IMMEDIATE")
    write_approval_request(db, approval_id="approval", run_id="run", operation="push", target="repo",
                           candidate_sha256=DIGEST, policy_version="1",
                           expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    db.execute("COMMIT")


def decide(db, *, digest=DIGEST, policy="1"):
    return write_approval_transition(
        db, ApprovalChange(ApprovalId("approval"), ApprovalState.REQUESTED, ApprovalState.APPROVED, 0),
        principal="human", candidate_sha256=digest, policy_version=policy,
    )


def test_request_and_decision_are_cas_and_events(db):
    request(db)
    db.execute("BEGIN IMMEDIATE")
    assert decide(db) == 1
    db.execute("COMMIT")
    assert db.execute("SELECT state,state_version,decision_principal FROM approval_requests").fetchone() == ("approved", 1, "human")
    assert db.execute("SELECT type FROM events ORDER BY sequence").fetchall() == [("approval.requested",), ("approval.state_changed",)]
    assert db.execute("SELECT count(*) FROM outbox").fetchone() == (2,)
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(StateConflict):
        decide(db)
    db.execute("ROLLBACK")


@pytest.mark.parametrize("digest,policy", [("b" * 64, "1"), (DIGEST, "2"), (None, "1")])
def test_changed_binding_rejected(db, digest, policy):
    request(db)
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(StateConflict):
        decide(db, digest=digest, policy=policy)
    db.execute("ROLLBACK")
    assert db.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)


def test_outbox_failure_rolls_back_decision(db):
    request(db)
    db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'fail'); END")
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError):
        decide(db)
    db.execute("ROLLBACK")
    assert db.execute("SELECT state,state_version FROM approval_requests").fetchone() == ("requested", 0)
    assert db.execute("SELECT count(*) FROM events").fetchone() == (1,)


def test_expired_request_cannot_be_approved(db):
    db.execute("INSERT INTO approval_requests(id,run_id,operation,target,candidate_manifest_sha256,policy_version,expires_at,requested_at) VALUES('approval','run','push','repo',?,'1','2000-01-01T00:00:00+00:00','1999-01-01T00:00:00+00:00')", (DIGEST,))
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(StateConflict):
        decide(db)
    db.execute("ROLLBACK")


def test_upgrade_preserves_existing_approval(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for name in ("0001_initial.sql", "0002_immutable_events.sql"):
        (migrations / name).write_bytes((ROOT / "migrations" / name).read_bytes())
    with closing(open_database(tmp_path / "upgrade.db")) as connection:
        apply_migrations(connection, migrations)
        connection.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        connection.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        connection.execute("INSERT INTO approval_requests(id,run_id,operation,target,candidate_manifest_sha256,policy_version,expires_at,requested_at) VALUES(?,?,?,?,?,?,?,?)", ("old", "run", "push", "repo", DIGEST, "1", "2099-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
        (migrations / "0003_approval_version.sql").write_bytes((ROOT / "migrations" / "0003_approval_version.sql").read_bytes())
        assert apply_migrations(connection, migrations) == [3]
        assert connection.execute("SELECT id,state,state_version FROM approval_requests").fetchone() == ("old", "requested", 0)


def test_competing_decisions_have_one_winner(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    request(db)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)
    def run():
        with closing(open_database(path)) as connection:
            barrier.wait(timeout=5)
            connection.execute("BEGIN IMMEDIATE")
            try:
                decide(connection)
                connection.execute("COMMIT")
                return "won"
            except StateConflict:
                connection.execute("ROLLBACK")
                return "stale"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: run(), range(2))) == ["stale", "won"]
    assert db.execute("SELECT count(*) FROM events").fetchone() == (2,)
