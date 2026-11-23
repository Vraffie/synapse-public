from contextlib import closing
import json
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from synapse.api.app import create_app
from synapse.application.approval_service import ApprovalService
from synapse.cli.__main__ import main as cli_main
from synapse.infrastructure.local_approver import LocalApproverAuthority, provision_approver_token
from synapse.persistence.approval_store import write_approval_request
from synapse.persistence.migrations import apply_migrations, open_database

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "o" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "k1", "X-Correlation-ID": "trace"}
DIGEST = "a" * 64
BODY = {"decision": "approved", "expected_version": 0, "operation": "push", "target": "repo",
        "candidate_sha256": DIGEST, "policy_version": "1"}


@pytest.fixture
def system(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    approver = provision_approver_token(fd)
    authority = LocalApproverAuthority(fd)
    with closing(open_database(tmp_path / "db")) as db:
        apply_migrations(db, ROOT / "migrations")
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        db.execute("BEGIN IMMEDIATE")
        write_approval_request(db, approval_id="ap", run_id="run", operation="push", target="repo",
                               candidate_sha256=DIGEST, policy_version="1", expires_at="2099-01-01T00:00:00+00:00",
                               requester_subject="agent-1", requester_kind="agent")
        db.execute("COMMIT")
        # TestClient serves requests on another thread; production keeps one owning thread.
        shared = sqlite3.connect(str(tmp_path / "db"), isolation_level=None, check_same_thread=False)
        shared.execute("PRAGMA foreign_keys = ON")
        service = ApprovalService(shared, authority)
        app = create_app(token=TOKEN, read_events=lambda a, l: [],
                         decide_approval=lambda t, aid, *, key, correlation, **f: service.decide(
                             t, approval_id=aid, idempotency_key=key, correlation_id=correlation, **f),
                         audit_denied_decision=service.record_denied)
        with TestClient(app, client=None) as client:
            yield client, db, approver, state
        shared.close()
    authority.close()
    os.close(fd)


def test_decision_requires_operator_bearer_and_valid_approver_token(system):
    client, db, approver, _ = system
    url = "/api/v1/approvals/ap/decision"
    assert client.post(url, json=BODY, headers={**HEADERS, "Authorization": "Bearer wrong", "X-Approver-Token": approver}).status_code == 401
    assert client.post(url, json=BODY, headers=HEADERS).status_code == 401  # operator token alone is not a human
    assert client.post(url, json=BODY, headers={**HEADERS, "X-Approver-Token": "short"}).status_code == 401
    assert client.post(url, json=BODY, headers={**HEADERS, "X-Approver-Token": "z" * 43}).status_code == 403
    assert client.post(url, json=BODY, headers={**HEADERS, "X-Approver-Token": TOKEN}).status_code == 403
    assert db.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)


def test_human_decision_is_applied_once_and_replayed(system):
    client, db, approver, _ = system
    headers = {**HEADERS, "X-Approver-Token": approver}
    first = client.post("/api/v1/approvals/ap/decision", json=BODY, headers=headers)
    assert first.status_code == 200 and first.json() == {"approval_id": "ap", "state": "approved", "state_version": 1}
    assert first.headers["idempotency-replayed"] == "false"
    assert client.post("/api/v1/approvals/ap/decision", json=BODY, headers=headers).headers["idempotency-replayed"] == "true"
    assert db.execute("SELECT state,decision_principal FROM approval_requests").fetchone() == ("approved", "human:local-approver")
    conflict = client.post("/api/v1/approvals/ap/decision", json={**BODY, "decision": "rejected"}, headers=headers)
    assert conflict.status_code == 409


def test_binding_mismatch_and_bad_bodies_are_rejected_without_leaking_token(system):
    client, db, approver, _ = system
    headers = {**HEADERS, "X-Approver-Token": approver}
    url = "/api/v1/approvals/ap/decision"
    assert client.post(url, json={**BODY, "target": "other"}, headers=headers).status_code == 409
    assert client.post(url, json={**BODY, "extra": 1}, headers=headers).status_code == 422
    assert client.post(url, json={**BODY, "decision": "expired"}, headers=headers).status_code == 422
    assert client.post(url, json={**BODY, "candidate_sha256": "x"}, headers=headers).status_code == 422
    assert client.post("/api/v1/approvals/missing/decision", json=BODY, headers=headers).status_code == 403
    response = client.post(url, json={**BODY, "decision": "expired"}, headers=headers)
    assert approver not in response.text
    assert db.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)


def test_rotation_revokes_old_token(system):
    client, db, approver, state = system
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        provision_approver_token(fd, rotate=True)
    finally:
        os.close(fd)
    assert client.post("/api/v1/approvals/ap/decision", json=BODY, headers={**HEADERS, "X-Approver-Token": approver}).status_code == 403


def test_cli_approver_init_prints_once_and_refuses_replacement(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    assert cli_main(["--state-dir", str(state), "approver-init"]) == 0
    token = capsys.readouterr().out.strip()
    assert (state / "approver.token").read_text() == token and len(token) == 43
    assert (state / "approver.token").stat().st_mode & 0o777 == 0o600
    assert cli_main(["--state-dir", str(state), "approver-init"]) == 1
    assert (state / "approver.token").read_text() == token
    assert cli_main(["--state-dir", str(state), "approver-init", "--rotate"]) == 0
    assert capsys.readouterr().out.strip() != token


def test_cli_decision_sends_token_in_header_not_argv(tmp_path, monkeypatch, capsys):
    calls = []
    def fake_request(state_dir, endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return {"approval_id": "ap", "state": "approved", "state_version": 1}
    monkeypatch.setattr("synapse.cli.__main__._request", fake_request)
    monkeypatch.setenv("SYNAPSE_APPROVER_TOKEN", "t" * 43)
    code = cli_main(["--state-dir", str(tmp_path), "approval-decide", "ap", "--decision", "approved",
                     "--expected-version", "0", "--operation", "push", "--target", "repo",
                     "--candidate-sha256", DIGEST, "--policy-version", "1",
                     "--idempotency-key", "k", "--correlation-id", "c"])
    assert code == 0
    endpoint, kwargs = calls[0]
    assert endpoint == "/api/v1/approvals/ap/decision" and kwargs["approver_token"] == "t" * 43
    assert json.loads(kwargs["body"])["decision"] == "approved"
    assert "t" * 43 not in capsys.readouterr().out


def test_reason_containing_the_approver_token_is_refused_and_not_persisted(system):
    client, db, approver, _ = system
    response = client.post("/api/v1/approvals/ap/decision", json={**BODY, "reason": "pasted " + approver},
                           headers={**HEADERS, "X-Approver-Token": approver})
    assert response.status_code == 422 and approver not in response.text
    assert db.execute("SELECT state,decision_reason FROM approval_requests").fetchone() == ("requested", None)
    assert approver not in "".join(str(r) for r in db.execute("SELECT details_json FROM audit_entries"))


def test_cli_malformed_token_is_never_echoed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SYNAPSE_APPROVER_TOKEN", "SECRETtoken-with-bad\x0bchars")
    code = cli_main(["--state-dir", str(tmp_path), "approval-decide", "ap", "--decision", "approved",
                     "--expected-version", "0", "--operation", "push", "--target", "repo",
                     "--candidate-sha256", DIGEST, "--policy-version", "1",
                     "--idempotency-key", "k", "--correlation-id", "c"])
    captured = capsys.readouterr()
    assert code == 1 and "SECRET" not in captured.out + captured.err


def _denials(db):
    return db.execute("SELECT principal,action,resource,correlation_id,details_json FROM audit_entries WHERE decision='denied' ORDER BY sequence").fetchall()


def test_denied_attempts_are_audited_without_credentials(system):
    client, db, approver, _ = system
    url = "/api/v1/approvals/ap/decision"
    secret = "z" * 43
    assert client.post(url, json=BODY, headers=HEADERS).status_code == 401
    assert client.post(url, json=BODY, headers={**HEADERS, "X-Approver-Token": "short"}).status_code == 401
    assert client.post(url, json=BODY, headers={**HEADERS, "X-Approver-Token": secret}).status_code == 403
    assert client.post(url, json=BODY, headers={**HEADERS, "X-Approver-Token": TOKEN}).status_code == 403
    assert client.post(url, json={**BODY, "policy_version": "2"}, headers={**HEADERS, "X-Approver-Token": approver}).status_code == 409
    rows = _denials(db)
    reasons = [json.loads(r[4])["reason"] for r in rows]
    # Missing, malformed, wrong and operator-as-approver tokens are indistinguishable.
    assert reasons == ["unauthenticated"] * 4 + ["conflict"]
    assert [r[0] for r in rows] == ["unauthenticated"] * 4 + ["human:local-approver"]
    assert [r[2] for r in rows] == ["unauthenticated"] * 4 + ["ap"]
    assert all(r[1] == "approval.decide" and r[3] == "trace" for r in rows)
    dump = json.dumps(db.execute("SELECT * FROM audit_entries").fetchall())
    for text in (secret, TOKEN, approver, "short"):
        assert text not in dump
    assert db.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)
    assert db.execute("SELECT count(*) FROM audit_entries WHERE decision='allowed'").fetchone() == (0,)


def test_self_approval_denial_is_audited(system):
    client, db, approver, _ = system
    db.execute("BEGIN IMMEDIATE")
    write_approval_request(db, approval_id="own", run_id="run", operation="push", target="repo",
                           candidate_sha256=DIGEST, policy_version="1", expires_at="2099-01-01T00:00:00+00:00",
                           requester_subject="local-approver", requester_kind="human")
    db.execute("COMMIT")
    r = client.post("/api/v1/approvals/own/decision", json=BODY, headers={**HEADERS, "X-Approver-Token": approver})
    assert r.status_code == 403 and r.json()["message"] == "Approval access denied"
    assert [(x[0], x[2], json.loads(x[4])) for x in _denials(db)] == [
        ("human:local-approver", "own", {"http_status": 403, "reason": "self_approval"})]


def test_denial_audit_is_bounded_per_window(system):
    from synapse.application import approval_service as svc
    client, db, _, _ = system
    for _ in range(svc.DENIAL_MAX_PER_WINDOW + 15):
        assert client.post("/api/v1/approvals/ap/decision", json=BODY, headers=HEADERS).status_code == 401
    rows = _denials(db)
    assert len(rows) == svc.DENIAL_MAX_PER_WINDOW + 1
    assert json.loads(rows[-1][4])["reason"] == "rate_limited"


def test_denial_audit_write_failure_does_not_change_response(system):
    client, db, approver, _ = system
    db.execute("CREATE TRIGGER no_denied BEFORE INSERT ON audit_entries WHEN NEW.decision='denied' BEGIN SELECT RAISE(ABORT,'x'); END")
    assert client.post("/api/v1/approvals/ap/decision", json=BODY, headers=HEADERS).status_code == 401
    assert _denials(db) == []


def test_operator_token_failures_log_warning_without_secret(system, caplog):
    import logging
    client, _, _, _ = system
    bad = "Bearer " + "q" * 40
    with caplog.at_level(logging.WARNING):
        r = client.post("/api/v1/approvals/ap/decision", json=BODY, headers={"Authorization": bad})
    assert r.status_code == 401
    recs = [x for x in caplog.records if x.levelno == logging.WARNING and "Operator authentication failed" in x.getMessage()]
    assert recs
    assert "q" * 40 not in caplog.text and TOKEN not in caplog.text


def test_dropped_denials_are_counted_in_the_next_written_row(tmp_path):
    from synapse.application import approval_service as svc
    now = [0.0]
    with closing(open_database(tmp_path / "db")) as db:
        apply_migrations(db, ROOT / "migrations")
        service = svc.ApprovalService(db, object(), clock=lambda: now[0])
        written = [service.record_denied("unauthenticated", correlation_id="c%d" % i, http_status=401)
                   for i in range(svc.DENIAL_MAX_PER_WINDOW + 7)]
        # MAX rows plus one rate_limited marker are written; the other 6 attempts are dropped
        assert written.count(True) == svc.DENIAL_MAX_PER_WINDOW + 1 and written.count(False) == 6
        now[0] += svc.DENIAL_WINDOW_SECONDS  # next window
        assert service.record_denied("unauthenticated", correlation_id="next", http_status=401) is True
        assert service.record_denied("unauthenticated", correlation_id="after", http_status=401) is True
        rows = [json.loads(r[0]) for r in db.execute(
            "SELECT details_json FROM audit_entries WHERE decision='denied' ORDER BY rowid DESC LIMIT 2")]
        assert rows[1].get("suppressed_before") == 6  # "next" reports the hidden attempts
        assert "suppressed_before" not in rows[0]  # and only once


def test_suppressed_counters_stay_bounded(tmp_path):
    from synapse.application import approval_service as svc
    with closing(open_database(tmp_path / "db")) as db:
        apply_migrations(db, ROOT / "migrations")
        service = svc.ApprovalService(db, object(), clock=lambda: 0.0)
        for i in range(svc.DENIAL_MAX_SUPPRESSED_BUCKETS + 30):
            for _ in range(svc.DENIAL_MAX_PER_WINDOW + 2):
                service.record_denied("not_permitted", correlation_id="c", http_status=403, principal="p%d" % i)
        assert len(service._denial_suppressed) <= svc.DENIAL_MAX_SUPPRESSED_BUCKETS
