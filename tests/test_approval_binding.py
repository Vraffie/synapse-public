from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import copy
import pickle
from pathlib import Path

import pytest

from synapse.domain.approvals import ApprovalChange, ApprovalState
from synapse.domain.contracts import ApprovalId
from synapse.persistence.approval_binding import (
    ApprovalGrant, ApprovalNotGranted, DenialReason, verify_approval_for_execution as verify)
from synapse.persistence.approval_store import write_approval_request, write_approval_transition
from synapse.persistence.migrations import apply_migrations, open_database

ROOT = Path(__file__).resolve().parents[1]
D = "a" * 64
ARGS = dict(approval_id="ap", run_id="run", operation="push", target="repo", candidate_sha256=D, policy_version="1")


@pytest.fixture
def db(tmp_path):
    with closing(open_database(tmp_path / "db")) as c:
        apply_migrations(c, ROOT / "migrations")
        c.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        c.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        c.execute("INSERT INTO runs VALUES('run2','wf','1','created',0,'now','now')")
        yield c


def make(db, *, requester="agent-1", kind="agent", decide=ApprovalState.APPROVED, principal="human-1", hours=1.0):
    db.execute("BEGIN IMMEDIATE")
    write_approval_request(db, approval_id="ap", run_id="run", operation="push", target="repo",
                           candidate_sha256=D, policy_version="1",
                           expires_at=(datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(),
                           requester_subject=requester, requester_kind=kind if requester else None)
    db.execute("COMMIT")
    if decide:
        db.execute("BEGIN IMMEDIATE")
        write_approval_transition(db, ApprovalChange(ApprovalId("ap"), ApprovalState.REQUESTED, decide, 0),
                                  principal=principal, candidate_sha256=D, policy_version="1")
        db.execute("COMMIT")


def denied(db, reason, **over):
    with pytest.raises(ApprovalNotGranted) as e:
        verify(db, **{**ARGS, **over})
    assert e.value.reason == reason
    return e.value


def test_success_returns_grant(db):
    make(db)
    g = verify(db, **ARGS)
    assert (g.approval_id, g.state_version, g.principal) == ("ap", 1, "human-1")
    assert g.decided_at == db.execute("SELECT decided_at FROM approval_requests").fetchone()[0]
    assert not db.in_transaction


def test_not_found(db):
    denied(db, DenialReason.NOT_FOUND)


def test_wrong_run(db):
    make(db)
    denied(db, DenialReason.WRONG_RUN, run_id="run2")


def test_requested_not_approved(db):
    make(db, decide=None)
    denied(db, DenialReason.NOT_APPROVED)


def test_rejected_not_approved(db):
    make(db, decide=ApprovalState.REJECTED)
    denied(db, DenialReason.NOT_APPROVED)


def test_revoked_state(db):
    make(db)
    db.execute("UPDATE approval_requests SET state='revoked',state_version=2")
    denied(db, DenialReason.REVOKED)


def test_expired_state(db):
    make(db)
    db.execute("UPDATE approval_requests SET state='expired',state_version=2")
    denied(db, DenialReason.EXPIRED)


def test_expired_even_if_still_approved(db):
    make(db)
    denied(db, DenialReason.EXPIRED, now=datetime.now(timezone.utc) + timedelta(hours=2))


def test_expiry_boundary(db):
    make(db)
    exp = datetime.fromisoformat(db.execute("SELECT expires_at FROM approval_requests").fetchone()[0])
    verify(db, **ARGS, now=exp - timedelta(microseconds=1))
    denied(db, DenialReason.EXPIRED, now=exp)
    denied(db, DenialReason.EXPIRED, now=exp + timedelta(microseconds=1))


def test_now_non_utc_timezone_normalized(db):
    make(db)
    exp = datetime.fromisoformat(db.execute("SELECT expires_at FROM approval_requests").fetchone()[0])
    tz = timezone(timedelta(hours=5))
    verify(db, **ARGS, now=(exp - timedelta(seconds=1)).astimezone(tz))
    denied(db, DenialReason.EXPIRED, now=exp.astimezone(tz))


@pytest.mark.parametrize("field,value", [("operation", "deploy"), ("target", "other"),
                                         ("candidate_sha256", "b" * 64), ("policy_version", "2")])
def test_single_field_mismatch(db, field, value):
    make(db)
    denied(db, DenialReason.BINDING_MISMATCH, **{field: value})


@pytest.mark.parametrize("over", [dict(candidate_sha256="A" * 64), dict(candidate_sha256="abc"),
                                  dict(operation=""), dict(approval_id=None), dict(now="2030-01-01"),
                                  dict(now=datetime(2030, 1, 1))])
def test_malformed_input(db, over):
    make(db)
    denied(db, DenialReason.MALFORMED, **over)


def test_requester_equals_approver(db):
    make(db, requester="same", kind="human", principal="same")
    denied(db, DenialReason.MALFORMED)


def test_requester_equals_approver_namespaced_principal(db):
    # F9: real rows store decision_principal as "kind:subject" and requester bare.
    make(db, requester="local-approver", kind="human", principal="human:local-approver")
    assert db.execute("SELECT decision_principal,requester_subject FROM approval_requests").fetchone() == (
        "human:local-approver", "local-approver")
    denied(db, DenialReason.MALFORMED)


def test_distinct_namespaced_principal_granted(db):
    make(db, requester="agent-1", kind="agent", principal="human:local-approver")
    assert verify(db, **ARGS).principal == "human:local-approver"


def test_missing_principal(db):
    make(db)
    db.execute("UPDATE approval_requests SET decision_principal=NULL")
    denied(db, DenialReason.MALFORMED)


def test_missing_requester_legacy_refused(db):
    make(db, requester=None)
    denied(db, DenialReason.MALFORMED)


@pytest.mark.parametrize("col,val", [("expires_at", "2999-01-01T00:00:00"), ("expires_at", "garbage"),
                                     ("decided_at", None)])
def test_malformed_record(db, col, val):
    make(db)
    db.execute("DROP TRIGGER approval_binding_immutable")  # simulate a corrupted row
    db.execute(f"UPDATE approval_requests SET {col}=?", (val,))
    denied(db, DenialReason.MALFORMED)


def test_malformed_version(db):
    make(db)
    db.execute("UPDATE approval_requests SET state_version=0")
    denied(db, DenialReason.MALFORMED)


def test_nothing_written(db):
    make(db)
    tables = ("events", "outbox", "audit_entries", "approval_requests")
    before_changes = db.total_changes
    before = [db.execute(f"SELECT * FROM {t}").fetchall() for t in tables]
    verify(db, **ARGS)
    denied(db, DenialReason.BINDING_MISMATCH, target="x")
    denied(db, DenialReason.EXPIRED, now=datetime.now(timezone.utc) + timedelta(days=1))
    assert db.total_changes == before_changes
    assert before == [db.execute(f"SELECT * FROM {t}").fetchall() for t in tables]
    assert not db.in_transaction


def test_refuses_inside_transaction(db):
    make(db)
    db.execute("BEGIN")
    with pytest.raises(RuntimeError):
        verify(db, **ARGS)
    db.execute("ROLLBACK")


def test_snapshot_consistent_under_concurrent_revocation(db, tmp_path):
    make(db)
    other = open_database(tmp_path / "db")

    class Proxy:
        in_transaction = False

        def execute(self, sql, *a):
            result = db.execute(sql, *a)
            if sql.startswith("SELECT run_id"):
                other.execute("UPDATE approval_requests SET state='revoked',state_version=2")
            return result

    try:
        assert verify(Proxy(), **ARGS).state_version == 1  # snapshot predates the revoke
        denied(db, DenialReason.REVOKED)  # the next check sees it
    finally:
        other.close()


def test_uncommitted_concurrent_write_invisible(db, tmp_path):
    make(db)
    other = open_database(tmp_path / "db")
    other.execute("BEGIN IMMEDIATE")
    other.execute("UPDATE approval_requests SET state='revoked',state_version=2")
    try:
        verify(db, **ARGS)
    finally:
        other.execute("ROLLBACK")
        other.close()


def test_grant_not_forgeable(db):
    make(db)
    g = verify(db, **ARGS)
    with pytest.raises(TypeError):
        ApprovalGrant("ap", 1, "x", "y", "z")
    with pytest.raises(TypeError):
        replace(g, approval_id="other")
    with pytest.raises(Exception):
        g.principal = "x"
    with pytest.raises(TypeError):
        pickle.dumps(g)
    with pytest.raises(TypeError):
        class Sub(ApprovalGrant):
            pass
    assert copy.copy(g) is g and copy.deepcopy(g) is g


@pytest.mark.parametrize("prefix", ["system", "service", "human", "agent", "bot"])
def test_self_approval_denied_for_any_principal_prefix(db, prefix):
    make(db, requester="alice", principal=f"{prefix}:alice")
    denied(db, DenialReason.MALFORMED)


def test_other_subject_with_unknown_prefix_is_granted(db):
    make(db, requester="alice", principal="service:bob")
    assert verify(db, **ARGS).principal == "service:bob"


def test_self_approval_check_is_case_sensitive(db):
    # Subjects are opaque ids and are not case-normalised anywhere, so "Alice" and "alice" differ.
    make(db, requester="alice", principal="system:Alice")
    assert verify(db, **ARGS).principal == "system:Alice"
