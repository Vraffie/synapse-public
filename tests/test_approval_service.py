from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from synapse.application.approval_service import Actor, ApprovalAccessDenied, ApprovalService
from synapse.persistence.idempotency import IdempotencyConflict
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import StateConflict

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "a" * 64


class Authority:
    def __init__(self):
        self.identities = {"human": Actor("human-1", "human"), "agent": Actor("agent-1", "agent"),
                           "self": Actor("agent-1", "human"), "other": Actor("human-2", "human")}
        self.permitted = True
        self.seen = []
    @contextmanager
    def authenticated(self, credential):
        if credential not in self.identities:
            raise ApprovalAccessDenied("denied")
        yield self.identities[credential]
    def allows(self, actor, action, resource):
        self.seen.append((actor, action, resource))
        return self.permitted


@pytest.fixture
def setup(tmp_path):
    with closing(open_database(tmp_path / "db")) as db:
        apply_migrations(db, ROOT / "migrations")
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        authority = Authority()
        yield db, authority, ApprovalService(db, authority)


def request(service, credential="agent", key="request"):
    return service.request(credential, approval_id="approval", run_id="run", operation="push", target="repo",
                           candidate_sha256=DIGEST, policy_version="1", expires_at="2099-01-01T00:00:00+00:00",
                           idempotency_key=key, correlation_id="trace")


def decide(service, credential="human", **overrides):
    params = dict(approval_id="approval", expected_version=0, decision="approved", operation="push", target="repo",
                  candidate_sha256=DIGEST, policy_version="1", idempotency_key="decide", correlation_id="trace")
    params.update(overrides)
    return service.decide(credential, **params)


def test_distinct_authenticated_human_decision_is_audited_and_idempotent(setup):
    db, authority, service = setup
    assert not request(service).replayed
    assert request(service).replayed
    assert db.execute("SELECT requester_subject,requester_kind FROM approval_requests").fetchone() == ("agent-1", "agent")
    assert not decide(service).replayed
    assert decide(service).replayed
    assert db.execute("SELECT state,state_version,decision_principal FROM approval_requests").fetchone() == ("approved", 1, "human:human-1")
    assert db.execute("SELECT principal,action,decision FROM audit_entries ORDER BY sequence").fetchall() == [("agent:agent-1", "approval.request", "allowed"), ("human:human-1", "approval.decide", "allowed")]
    assert db.execute("SELECT count(*) FROM events").fetchone() == db.execute("SELECT count(*) FROM outbox").fetchone() == (2,)
    resource = authority.seen[-1][2]
    assert (resource["requester_subject"], resource["operation"], resource["target"], resource["candidate_sha256"], resource["policy_version"]) == ("agent-1", "push", "repo", DIGEST, "1")
    with pytest.raises(IdempotencyConflict):
        decide(service, reason="different")
    authority.permitted = False
    with pytest.raises(ApprovalAccessDenied):
        decide(service)
    del authority.identities["human"]
    with pytest.raises(ApprovalAccessDenied):
        decide(service)


def test_agents_self_and_unauthorized_humans_cannot_decide(setup):
    db, authority, service = setup
    request(service)
    for credential in ("agent", "self", "missing"):
        with pytest.raises(ApprovalAccessDenied):
            decide(service, credential)
    authority.permitted = False
    with pytest.raises(ApprovalAccessDenied):
        decide(service)
    assert db.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)
    assert db.execute("SELECT count(*) FROM audit_entries").fetchone() == (1,)


@pytest.mark.parametrize("field,value", [("operation", "deploy"), ("target", "other"), ("candidate_sha256", "b" * 64), ("policy_version", "2")])
def test_mismatched_binding_rejected_without_mutation(setup, field, value):
    db, _, service = setup
    request(service)
    with pytest.raises(StateConflict):
        decide(service, **{field: value})
    assert db.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)


def test_expired_and_legacy_unbound_requests_fail_closed(setup):
    db, _, service = setup
    db.execute("INSERT INTO approval_requests(id,run_id,operation,target,candidate_manifest_sha256,policy_version,expires_at,requested_at) VALUES('approval','run','push','repo',?,'1','2099-01-01T00:00:00+00:00','now')", (DIGEST,))
    with pytest.raises(ApprovalAccessDenied):
        decide(service)
    db.execute("INSERT INTO approval_requests(id,run_id,operation,target,candidate_manifest_sha256,policy_version,expires_at,requested_at,requester_subject,requester_kind) VALUES('expired','run','push','repo',?,'1','2000-01-01T00:00:00+00:00','now','agent-1','agent')", (DIGEST,))
    for decision in ("approved", "rejected"):
        with pytest.raises(StateConflict, match="expired"):
            decide(service, approval_id="expired", decision=decision)
    assert db.execute("SELECT count(*) FROM events").fetchone() == (0,)


def test_audit_or_outbox_failure_rolls_back_entire_decision(setup):
    db, _, service = setup
    request(service)
    db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON audit_entries BEGIN SELECT RAISE(ABORT, 'audit failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="audit failed"):
        decide(service)
    assert db.execute("SELECT state,state_version FROM approval_requests").fetchone() == ("requested", 0)
    assert db.execute("SELECT count(*) FROM events").fetchone() == db.execute("SELECT count(*) FROM outbox").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM idempotency_keys WHERE operation='approval.decide'").fetchone() == (0,)
    db.execute("DROP TRIGGER fail_audit")
    db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'outbox failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="outbox failed"):
        decide(service)
    assert db.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)
    assert db.execute("SELECT count(*) FROM audit_entries").fetchone() == (1,)


def test_competing_distinct_humans_cannot_both_decide(setup):
    db, _, service = setup
    request(service)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)
    def attempt(credential):
        with closing(open_database(path)) as conn:
            barrier.wait(timeout=5)
            try:
                return decide(ApprovalService(conn, Authority()), credential).response["state"]
            except StateConflict:
                return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("human", "other")))
    assert sorted(results) == ["approved", "conflict"]
    assert db.execute("SELECT count(*) FROM audit_entries WHERE action='approval.decide'").fetchone() == (1,)


def test_migration_preserves_legacy_and_prevents_binding_changes(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    for source in sorted((ROOT / "migrations").glob("*.sql")):
        if int(source.name[:4]) >= 10:
            continue
        (directory / source.name).write_bytes(source.read_bytes())
    with closing(open_database(tmp_path / "db")) as db:
        assert apply_migrations(db, directory) == list(range(1, 10))
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        db.execute("INSERT INTO approval_requests(id,run_id,operation,target,candidate_manifest_sha256,policy_version,expires_at,requested_at) VALUES('legacy','run','push','repo',?,'1','2099-01-01T00:00:00+00:00','now')", (DIGEST,))
        source = ROOT / "migrations" / "0010_approval_requester.sql"
        (directory / source.name).write_bytes(source.read_bytes())
        assert apply_migrations(db, directory) == [10]
        assert db.execute("SELECT requester_subject,requester_kind FROM approval_requests").fetchone() == (None, None)
        for sql in ("UPDATE approval_requests SET requester_subject='agent' WHERE id='legacy'", "DELETE FROM approval_requests WHERE id='legacy'", "INSERT OR REPLACE INTO approval_requests(id,run_id,operation,target,candidate_manifest_sha256,policy_version,expires_at,requested_at) VALUES('legacy','run','push','repo','hash','1','later','now')"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO approval_requests(id,run_id,operation,target,candidate_manifest_sha256,policy_version,expires_at,requested_at,requester_subject) VALUES('invalid','run','push','repo','hash','1','later','now','agent')")


def test_human_requester_cannot_self_approve_and_other_human_can_reject(setup):
    db, _, service = setup
    request(service, credential="human")
    with pytest.raises(ApprovalAccessDenied):
        decide(service, credential="human")
    result = decide(service, credential="other", decision="rejected", reason="incorrect candidate")
    assert result.response["state"] == "rejected"
    assert db.execute("SELECT state,decision_principal,decision_reason FROM approval_requests").fetchone() == ("rejected", "human:human-2", "incorrect candidate")
    assert db.execute("SELECT count(*) FROM audit_entries WHERE action='approval.decide'").fetchone() == (1,)
