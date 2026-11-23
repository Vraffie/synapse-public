from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sqlite3

import pytest

from synapse.application.approval_requests import DaemonApprovalRequester, approval_id_for
from synapse.application.approval_service import ApprovalAccessDenied, ApprovalService
from synapse.infrastructure.daemon_requester import DaemonRequesterAuthority
from synapse.infrastructure.local_approver import LocalApproverAuthority, provision_approver_token
from synapse.persistence.idempotency import IdempotencyConflict
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import StateConflict

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "a" * 64


def future(hours=1):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


@pytest.fixture
def env(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    token = provision_approver_token(fd)
    authority = LocalApproverAuthority(fd)
    with closing(open_database(tmp_path / "db")) as db:
        apply_migrations(db, ROOT / "migrations")
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        yield db, DaemonApprovalRequester(db), ApprovalService(db, authority), token
    authority.close()
    os.close(fd)


def create(requester, key="k1", **overrides):
    params = dict(run_id="run", operation="push", target="repo", candidate_sha256=DIGEST,
                  policy_version="1", expires_at=future(), idempotency_key=key, correlation_id="trace")
    params.update(overrides)
    return requester.request_approval(**params)


def decide(service, token, approval_id, **overrides):
    params = dict(approval_id=approval_id, expected_version=0, decision="approved", operation="push",
                  target="repo", candidate_sha256=DIGEST, policy_version="1",
                  idempotency_key="d1", correlation_id="trace")
    params.update(overrides)
    return service.decide(token, **params)


def test_request_is_bound_audited_and_emits_event(env):
    db, requester, _, _ = env
    expires = future()
    result = create(requester, expires_at=expires)
    assert not result.replayed
    approval_id = result.response["approval_id"]
    assert approval_id == approval_id_for("run", "k1")
    row = db.execute("SELECT run_id,operation,target,candidate_manifest_sha256,policy_version,state,"
                     "state_version,requester_subject,requester_kind FROM approval_requests").fetchone()
    assert row == ("run", "push", "repo", DIGEST, "1", "requested", 0, "daemon:workflow", "agent")
    assert db.execute("SELECT principal,action,resource,decision FROM audit_entries").fetchall() == [
        ("agent:daemon:workflow", "approval.request", approval_id, "allowed")]
    assert db.execute("SELECT type,run_id FROM events").fetchall() == [("approval.requested", "run")]
    assert db.execute("SELECT count(*) FROM outbox").fetchone() == (1,)


def test_idempotent_replay_and_conflict(env):
    db, requester, _, _ = env
    expires = future()
    first = create(requester, expires_at=expires)
    again = create(requester, expires_at=expires)
    assert again.replayed and again.response == first.response
    for table in ("approval_requests", "audit_entries", "events", "outbox"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,)
    with pytest.raises(IdempotencyConflict):
        create(requester, expires_at=expires, target="other")
    assert create(requester, key="k2").response["approval_id"] != first.response["approval_id"]


def test_expiry_binding_and_run_validation(env):
    db, requester, _, _ = env
    for bad in (future(-1), future(25), "2099-01-01T00:00:00", "garbage"):
        with pytest.raises(ValueError):
            create(requester, expires_at=bad)
    for overrides in ({"candidate_sha256": "A" * 64}, {"candidate_sha256": "short"}, {"target": ""},
                      {"operation": " "}, {"policy_version": ""}, {"run_id": "missing"}):
        with pytest.raises(ValueError):
            create(requester, **overrides)
    for table in ("approval_requests", "audit_entries", "events", "outbox", "idempotency_keys"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_audit_or_outbox_failure_rolls_back_request(env):
    db, requester, _, _ = env
    for table in ("audit_entries", "outbox"):
        db.execute(f"CREATE TRIGGER fail BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'boom'); END")
        with pytest.raises(sqlite3.IntegrityError, match="boom"):
            create(requester)
        db.execute("DROP TRIGGER fail")
        for t in ("approval_requests", "audit_entries", "events", "outbox", "idempotency_keys"):
            assert db.execute(f"SELECT count(*) FROM {t}").fetchone() == (0,)
    assert not create(requester).replayed


def test_requester_cannot_decide_and_approver_cannot_request(env):
    db, requester, approver_service, token = env
    approval_id = create(requester).response["approval_id"]
    requester_service = requester._service
    capability = requester._authority.capability
    with pytest.raises(ApprovalAccessDenied):
        decide(requester_service, capability, approval_id)
    authority = DaemonRequesterAuthority()
    with authority.authenticated(authority.capability) as actor:
        assert authority.allows(actor, "approval.decide", {}) is False
    # The human approver authority and its token cannot create requests.
    with pytest.raises(ApprovalAccessDenied):
        approver_service.request(token, approval_id="x", run_id="run", operation="push", target="repo",
                                 candidate_sha256=DIGEST, policy_version="1", expires_at=future(),
                                 idempotency_key="r", correlation_id="t")
    # Strings, the operator-style tokens, and another instance's capability are rejected.
    for credential in (token, "daemon:workflow", None, object(), DaemonRequesterAuthority().capability):
        with pytest.raises(ApprovalAccessDenied):
            requester_service.request(credential, approval_id="y", run_id="run", operation="push", target="repo",
                                      candidate_sha256=DIGEST, policy_version="1", expires_at=future(),
                                      idempotency_key="r2", correlation_id="t")
    assert db.execute("SELECT state FROM approval_requests").fetchall() == [("requested",)]
    assert db.execute("SELECT count(*) FROM audit_entries").fetchone() == (1,)


def test_end_to_end_human_decides_and_requester_cannot_self_approve(env):
    db, requester, approver_service, token = env
    approval_id = create(requester).response["approval_id"]
    with pytest.raises(ApprovalAccessDenied):
        decide(approver_service, "wrong-token", approval_id)
    with pytest.raises(StateConflict):
        decide(approver_service, token, approval_id, target="other")
    assert decide(approver_service, token, approval_id).response["state"] == "approved"
    assert db.execute("SELECT state,decision_principal FROM approval_requests").fetchone() == ("approved", "human:local-approver")


def test_requester_identity_equal_to_decider_is_refused_by_service(env):
    # A decider that presents the requester's subject is refused on subject comparison.
    db, requester, _, _ = env
    approval_id = create(requester).response["approval_id"]

    from contextlib import contextmanager
    from synapse.application.approval_service import Actor

    class Impostor:
        @contextmanager
        def authenticated(self, credential):
            yield Actor("daemon:workflow", "human")
        def allows(self, actor, action, resource):
            return True

    with pytest.raises(ApprovalAccessDenied):
        decide(ApprovalService(db, Impostor()), "x", approval_id)
    assert db.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)
