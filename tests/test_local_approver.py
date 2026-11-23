from contextlib import closing
from pathlib import Path
import os

import pytest

from synapse.application.approval_service import Actor, ApprovalAccessDenied, ApprovalService
from synapse.infrastructure.local_approver import LocalApproverAuthority, provision_approver_token
from synapse.persistence.approval_store import write_approval_request
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import StateConflict

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "a" * 64


@pytest.fixture
def state(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    yield directory, fd
    os.close(fd)


def test_token_is_private_atomic_and_not_silently_replaced(state):
    directory, fd = state
    token = provision_approver_token(fd)
    path = directory / "approver.token"
    assert path.read_text() == token and len(token) == 43
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in directory.iterdir()] == ["approver.token"]
    with pytest.raises(FileExistsError):
        provision_approver_token(fd)
    assert path.read_text() == token
    rotated = provision_approver_token(fd, rotate=True)
    assert rotated != token and path.read_text() == rotated
    assert [p.name for p in directory.iterdir()] == ["approver.token"]


def test_authentication_accepts_only_current_token_and_revocation_is_immediate(state):
    directory, fd = state
    token = provision_approver_token(fd)
    authority = LocalApproverAuthority(fd)
    try:
        with authority.authenticated(token) as actor:
            assert actor == Actor("local-approver", "human")
        for bad in ("", "x" * 43, None, 7, token[:-1]):
            with pytest.raises(ApprovalAccessDenied):
                with authority.authenticated(bad):
                    pass
        rotated = provision_approver_token(fd, rotate=True)
        with pytest.raises(ApprovalAccessDenied):
            with authority.authenticated(token):
                pass
        with authority.authenticated(rotated):
            pass
        (directory / "approver.token").unlink()
        with pytest.raises(ApprovalAccessDenied):
            with authority.authenticated(rotated):
                pass
    finally:
        authority.close()


def test_unsafe_token_file_is_rejected(state):
    directory, fd = state
    token = provision_approver_token(fd)
    authority = LocalApproverAuthority(fd)
    try:
        (directory / "approver.token").chmod(0o640)
        with pytest.raises(PermissionError):
            with authority.authenticated(token):
                pass
        (directory / "approver.token").chmod(0o600)
        os.link(directory / "approver.token", directory / "alias")
        with pytest.raises(PermissionError):
            with authority.authenticated(token):
                pass
    finally:
        authority.close()


def test_only_decide_is_allowed():
    authority = LocalApproverAuthority.__new__(LocalApproverAuthority)
    human = Actor("local-approver", "human")
    assert authority.allows(human, "approval.decide", {})
    assert not authority.allows(human, "approval.request", {})
    assert not authority.allows(Actor("local-approver", "agent"), "approval.decide", {})
    assert not authority.allows(Actor("someone", "human"), "approval.decide", {})


def test_service_decision_with_local_approver_and_requester_separation(state, tmp_path):
    directory, fd = state
    token = provision_approver_token(fd)
    authority = LocalApproverAuthority(fd)
    try:
        with closing(open_database(tmp_path / "db")) as db:
            apply_migrations(db, ROOT / "migrations")
            db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
            db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
            service = ApprovalService(db, authority)
            def add(approval_id, subject):
                db.execute("BEGIN IMMEDIATE")
                write_approval_request(db, approval_id=approval_id, run_id="run", operation="push", target="repo",
                                       candidate_sha256=DIGEST, policy_version="1", expires_at="2099-01-01T00:00:00+00:00",
                                       requester_subject=subject, requester_kind="human" if subject == "local-approver" else "agent")
                db.execute("COMMIT")
            add("by-agent", "agent-1")
            add("by-self", "local-approver")
            params = dict(expected_version=0, decision="approved", operation="push", target="repo",
                          candidate_sha256=DIGEST, policy_version="1", correlation_id="trace")
            with pytest.raises(ApprovalAccessDenied):  # operator-style or wrong credential
                service.decide("not-the-token", approval_id="by-agent", idempotency_key="k0", **params)
            with pytest.raises(ApprovalAccessDenied):  # approver cannot decide their own request
                service.decide(token, approval_id="by-self", idempotency_key="k1", **params)
            result = service.decide(token, approval_id="by-agent", idempotency_key="k2", **params)
            assert not result.replayed
            assert db.execute("SELECT state,decision_principal FROM approval_requests WHERE id='by-agent'").fetchone() == ("approved", "human:local-approver")
            with pytest.raises(StateConflict):
                service.decide(token, approval_id="by-agent", idempotency_key="k3", **params)
    finally:
        authority.close()


def test_symlinked_or_unreadable_token_file_is_access_denied_not_a_server_error(state):
    directory, fd = state
    token = provision_approver_token(fd)
    authority = LocalApproverAuthority(fd)
    try:
        real = directory / "approver.token"
        real.rename(directory / "elsewhere")
        real.symlink_to(directory / "elsewhere")
        with pytest.raises(ApprovalAccessDenied):
            with authority.authenticated(token):
                pass
        real.unlink()
        (directory / "elsewhere").rename(real)
        os.link(real, directory / "alias")
        with pytest.raises(ApprovalAccessDenied):
            with authority.authenticated(token):
                pass
    finally:
        authority.close()
