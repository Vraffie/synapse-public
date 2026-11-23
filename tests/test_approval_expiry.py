import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

import synapse.persistence.approval_expiry as mod
from synapse.daemon.__main__ import _with_expiry_sweeps
from synapse.persistence.approval_expiry import expire_due_approvals, run_expiry_sweeps
from synapse.persistence.migrations import apply_migrations, open_database

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
PAST = "2020-01-01T00:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    with closing(open_database(tmp_path / "db")) as c:
        apply_migrations(c, ROOT / "migrations")
        c.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        c.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        yield c


def add(db, aid, expires, state="requested", version=0):
    db.execute("INSERT INTO approval_requests(id,run_id,operation,target,candidate_manifest_sha256,policy_version,"
               "state,expires_at,requested_at,state_version) VALUES(?,'run','push','repo',?,'1',?,?,'2020-01-01T00:00:00+00:00',?)",
               (aid, "a" * 64, state, expires, version))
    db.commit()


def state(db, aid):
    return db.execute("SELECT state,state_version FROM approval_requests WHERE id=?", (aid,)).fetchone()


def counts(db):
    return tuple(db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("events", "outbox", "audit_entries"))


def test_due_expires_with_audit_event_outbox(db):
    add(db, "a1", PAST)
    assert expire_due_approvals(db, now=NOW) == ["a1"]
    assert state(db, "a1") == ("expired", 1)
    assert counts(db) == (1, 1, 1)
    row = db.execute("SELECT principal,action,resource,decision FROM audit_entries").fetchone()
    assert row == ("daemon:expiry", "approval.expire", "a1", "allowed")
    assert not db.in_transaction


def test_not_due_untouched(db):
    add(db, "a1", "2999-01-01T00:00:00+00:00")
    assert expire_due_approvals(db, now=NOW) == []
    assert state(db, "a1") == ("requested", 0) and counts(db) == (0, 0, 0)


def test_exact_boundary(db):
    edge = "2020-06-01T00:00:00+00:00"
    add(db, "a1", edge)
    at = datetime.fromisoformat(edge)
    assert expire_due_approvals(db, now=at - timedelta(microseconds=1)) == []
    assert expire_due_approvals(db, now=at) == ["a1"]


def test_offset_timestamp_compared_in_utc(db):
    add(db, "a1", "2020-06-01T02:00:00+02:00")  # 00:00Z
    assert expire_due_approvals(db, now=datetime(2020, 5, 31, 23, 59, tzinfo=timezone.utc)) == []
    assert expire_due_approvals(db, now=datetime(2020, 6, 1, tzinfo=timezone.utc)) == ["a1"]


@pytest.mark.parametrize("st", ["approved", "rejected", "expired", "revoked"])
def test_decided_rows_untouched(db, st):
    add(db, "a1", PAST, state=st, version=1)
    assert expire_due_approvals(db, now=NOW) == []
    assert state(db, "a1") == (st, 1) and counts(db) == (0, 0, 0)


def test_naive_and_invalid_skipped_not_fatal(db, caplog):
    add(db, "bad", "not-a-date")
    add(db, "naive", "2020-01-01T00:00:00")
    add(db, "ok", PAST)
    assert expire_due_approvals(db, now=NOW) == ["ok"]
    assert state(db, "bad")[0] == state(db, "naive")[0] == "requested"
    assert "bad" in caplog.text and "naive" in caplog.text


def test_naive_now_and_bad_limit_rejected(db):
    with pytest.raises(ValueError):
        expire_due_approvals(db, now=datetime(2030, 1, 1))
    with pytest.raises(ValueError):
        expire_due_approvals(db, now=NOW, limit=0)


def test_batch_limit_and_idempotent_rerun(db):
    for i in range(5):
        add(db, f"a{i}", PAST)
    assert len(expire_due_approvals(db, now=NOW, limit=2)) == 2
    assert len(expire_due_approvals(db, now=NOW, limit=2)) == 2
    assert len(expire_due_approvals(db, now=NOW, limit=2)) == 1
    assert expire_due_approvals(db, now=NOW) == []
    assert counts(db) == (5, 5, 5)


def test_concurrent_decision_after_scan_is_skipped(db, monkeypatch):
    add(db, "a1", PAST)
    add(db, "a2", PAST)
    real = mod.write_approval_transition

    def racing(conn, change, **kw):
        if change.approval_id == "a1":  # another writer's committed decision, seen as stale CAS
            conn.execute("UPDATE approval_requests SET state='approved',state_version=1 WHERE id='a1'")
            conn.execute("COMMIT")
            conn.execute("BEGIN IMMEDIATE")
        return real(conn, change, **kw)
    monkeypatch.setattr(mod, "write_approval_transition", racing)
    assert expire_due_approvals(db, now=NOW) == ["a2"]
    assert state(db, "a1") == ("approved", 1)
    assert state(db, "a2") == ("expired", 1)
    assert counts(db) == (1, 1, 1)
    assert not db.in_transaction


def test_atomic_rollback_when_audit_fails(db):
    add(db, "a1", PAST)
    db.execute("CREATE TRIGGER boom BEFORE INSERT ON audit_entries BEGIN SELECT RAISE(ABORT,'boom'); END")
    with pytest.raises(sqlite3.DatabaseError):
        expire_due_approvals(db, now=NOW)
    assert not db.in_transaction
    assert state(db, "a1") == ("requested", 0) and counts(db) == (0, 0, 0)


def test_rejects_open_transaction(db):
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError):
        expire_due_approvals(db, now=NOW)
    db.rollback()


def test_periodic_task_sweeps_and_cancels(db):
    add(db, "a1", PAST)

    async def go():
        task = asyncio.create_task(run_expiry_sweeps(db, 0.01))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if state(db, "a1")[0] == "expired":
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    assert state(db, "a1") == ("expired", 1)
    with pytest.raises(ValueError):
        asyncio.run(run_expiry_sweeps(db, 0))


def test_daemon_hook_runs_and_stops_with_server(db):
    add(db, "a1", PAST)

    async def serve():
        await asyncio.sleep(0.1)

    async def go():
        await _with_expiry_sweeps(db, serve(), interval=0.01)
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert asyncio.run(go()) == []
    assert state(db, "a1")[0] == "expired"


def test_daemon_hook_stops_when_server_fails(db):
    async def serve():
        raise RuntimeError("down")

    async def go():
        with pytest.raises(RuntimeError):
            await _with_expiry_sweeps(db, serve(), interval=0.01)
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert asyncio.run(go()) == []
