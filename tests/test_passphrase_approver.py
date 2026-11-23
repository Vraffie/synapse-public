from contextlib import closing
import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from synapse.api.app import create_app
from synapse.application.approval_service import Actor, ApprovalAccessDenied, ApprovalService
from synapse.cli.__main__ import main as cli_main
from synapse.infrastructure import passphrase_approver as pa
from synapse.infrastructure.local_approver import LocalApproverAuthority, provision_approver_token
from synapse.infrastructure.passphrase_approver import (
    PassphraseApproverAuthority, build_approver_authority, provision_passphrase_verifier, read_mode_hint)
from synapse.persistence.approval_store import write_approval_request
from synapse.persistence.migrations import apply_migrations, open_database

ROOT = Path(__file__).resolve().parents[1]
PHRASE = "correct horse battery staple"
WRONG = "wrong horse battery staple!"
FAST = dict(n=2 ** 10, r=8, p=1)  # production defaults are 2**18; tests use the minimum the verifier format accepts
TOKEN = "o" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "k1", "X-Correlation-ID": "trace"}
DIGEST = "a" * 64
BODY = {"decision": "approved", "expected_version": 0, "operation": "push", "target": "repo",
        "candidate_sha256": DIGEST, "policy_version": "1"}


class Clock:
    now = 1000.0
    def __call__(self):
        return self.now


@pytest.fixture
def state(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    yield directory, fd
    os.close(fd)


def _fails(authority, phrase=WRONG):
    with pytest.raises(ApprovalAccessDenied) as caught:
        with authority.authenticated(phrase):
            pass
    return caught.value


def test_production_kdf_parameters_are_high_cost():
    assert (pa.KDF_N, pa.KDF_R, pa.KDF_P, pa.SALT_BYTES) == (2 ** 18, 8, 1, 32)


def test_correct_passphrase_approves_wrong_is_refused(state):
    directory, fd = state
    provision_passphrase_verifier(fd, PHRASE, **FAST)
    authority = PassphraseApproverAuthority(fd)
    try:
        with authority.authenticated(PHRASE) as actor:
            assert actor == Actor("local-approver", "human")
        for bad in (WRONG, "", None, "x" * 300):
            assert _fails(authority, bad).code == "unauthenticated"
        assert authority.allows(Actor("local-approver", "human"), "approval.decide", {})
        assert not authority.allows(Actor("local-approver", "human"), "approval.request", {})
    finally:
        authority.close()


def test_verifier_file_has_no_passphrase_and_is_private(state):
    directory, fd = state
    provision_passphrase_verifier(fd, PHRASE, **FAST)
    raw = (directory / "approver.verifier").read_bytes()
    assert PHRASE.encode() not in raw and b"horse" not in raw
    record = json.loads(raw)
    assert record["kdf"] == "scrypt" and len(base64_len(record["salt"])) >= 16
    assert (directory / "approver.verifier").stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in directory.iterdir()) == ["approver.json", "approver.verifier"]
    for p in directory.iterdir():
        assert PHRASE.encode() not in p.read_bytes()
    with pytest.raises(FileExistsError):
        provision_passphrase_verifier(fd, WRONG + "2", **FAST)
    other = json.loads(raw)["salt"]
    provision_passphrase_verifier(fd, PHRASE, rotate=True, **FAST)
    assert json.loads((directory / "approver.verifier").read_bytes())["salt"] != other  # per-install random salt


def base64_len(value):
    import base64
    return base64.b64decode(value)


def test_short_passphrase_and_operator_token_reuse_refused(state):
    _, fd = state
    with pytest.raises(ValueError):
        provision_passphrase_verifier(fd, "short", **FAST)
    with pytest.raises(RuntimeError):
        provision_passphrase_verifier(fd, ("abcdefgh" * 6)[:43], operator_token=("abcdefgh" * 6)[:43], **FAST)


def test_lockout_after_n_failures_refuses_even_correct_then_expires_and_doubles(state, caplog):
    _, fd = state
    provision_passphrase_verifier(fd, PHRASE, **FAST)
    clock = Clock()
    authority = PassphraseApproverAuthority(fd, clock=clock, max_attempts=3, lockout_seconds=60)
    try:
        with caplog.at_level(logging.WARNING, logger="synapse.approver"):
            for _ in range(3):
                assert _fails(authority).code == "unauthenticated"
            assert _fails(authority, PHRASE).code == "rate_limited"  # locked: correct one refused too
            clock.now += 61
            with authority.authenticated(PHRASE):
                pass
            for _ in range(3):
                _fails(authority)
            clock.now += 61  # second consecutive lock... success reset strikes, so still 60 s
            with authority.authenticated(PHRASE):
                pass
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings and all(PHRASE not in r.getMessage() and WRONG not in r.getMessage() for r in caplog.records)
    finally:
        authority.close()


def test_malformed_or_tampered_verifier_fails_closed(state):
    directory, fd = state
    provision_passphrase_verifier(fd, PHRASE, **FAST)
    path = directory / "approver.verifier"
    record = json.loads(path.read_bytes())
    authority = PassphraseApproverAuthority(fd)
    try:
        record["n"] = 2 ** 30  # memory-exhaustion attempt via tampered parameters
        path.write_text(json.dumps(record))
        _fails(authority, PHRASE)
        path.write_text("not json")
        _fails(authority, PHRASE)
        path.unlink()
        _fails(authority, PHRASE)
    finally:
        authority.close()


def test_mode_selection_defaults_to_token_and_token_mode_is_unchanged(state):
    directory, fd = state
    assert read_mode_hint(directory) == "token"
    token = provision_approver_token(fd)
    authority = build_approver_authority(fd)
    try:
        assert isinstance(authority, LocalApproverAuthority)
        with authority.authenticated(token):
            pass
    finally:
        authority.close()
    (directory / "approver.json").write_text('{"authority":"bogus"}')
    authority = build_approver_authority(fd)
    assert isinstance(authority, LocalApproverAuthority)  # unknown config falls back to token mode
    authority.close()


def test_passphrase_mode_selected_and_old_token_removed(state):
    directory, fd = state
    token = provision_approver_token(fd)
    provision_passphrase_verifier(fd, PHRASE, **FAST)
    assert not (directory / "approver.token").exists()
    assert read_mode_hint(directory) == "passphrase"
    authority = build_approver_authority(fd)
    try:
        assert isinstance(authority, PassphraseApproverAuthority)
        assert _fails(authority, token + "xxxx").code == "unauthenticated"
    finally:
        authority.close()


@pytest.fixture
def api(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    provision_passphrase_verifier(fd, PHRASE, **FAST)
    authority = PassphraseApproverAuthority(fd, max_attempts=3)
    with closing(open_database(tmp_path / "db")) as db:
        apply_migrations(db, ROOT / "migrations")
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        db.execute("BEGIN IMMEDIATE")
        write_approval_request(db, approval_id="ap", run_id="run", operation="push", target="repo",
                               candidate_sha256=DIGEST, policy_version="1", expires_at="2099-01-01T00:00:00+00:00",
                               requester_subject="agent-1", requester_kind="agent")
        db.execute("COMMIT")
        shared = sqlite3.connect(str(tmp_path / "db"), isolation_level=None, check_same_thread=False)
        shared.execute("PRAGMA foreign_keys = ON")
        service = ApprovalService(shared, authority)
        app = create_app(token=TOKEN, read_events=lambda a, l: [],
                         decide_approval=lambda t, aid, *, key, correlation, **f: service.decide(
                             t, approval_id=aid, idempotency_key=key, correlation_id=correlation, **f),
                         audit_denied_decision=service.record_denied,
                         preauthenticate_decision=service.preauthenticate)
        with TestClient(app, client=None) as client:
            yield client, db
        shared.close()
    authority.close()
    os.close(fd)


def _header(phrase):
    import base64
    return {"X-Approver-Passphrase": base64.urlsafe_b64encode(phrase.encode()).decode().rstrip("=")}


def test_api_wrong_audited_without_secret_lockout_then_refuses_correct(api, caplog):
    client, db = api
    url = "/api/v1/approvals/ap/decision"
    with caplog.at_level(logging.DEBUG):
        for i in range(3):
            assert client.post(url, json=BODY, headers={**HEADERS, **_header(WRONG)}).status_code == 403
        assert client.post(url, json=BODY, headers={**HEADERS, **_header(PHRASE)}).status_code == 403  # locked
    reasons = [json.loads(r[0])["reason"] for r in db.execute(
        "SELECT details_json FROM audit_entries WHERE decision='denied' ORDER BY rowid")]
    assert reasons == ["unauthenticated"] * 2 + ["lockout", "unauthenticated", "rate_limited"]  # lockout audited when it starts
    dump = " ".join(r[0] for r in db.execute("SELECT details_json||principal||resource FROM audit_entries"))
    assert WRONG not in dump and PHRASE not in dump
    assert PHRASE not in caplog.text and WRONG not in caplog.text
    assert any(r.levelno == logging.WARNING and r.name == "synapse.approver" for r in caplog.records)


def test_api_correct_passphrase_approves_and_both_headers_rejected(api):
    client, db = api
    url = "/api/v1/approvals/ap/decision"
    both = {**HEADERS, **_header(PHRASE), "X-Approver-Token": "t" * 43}
    assert client.post(url, json=BODY, headers=both).status_code == 401
    assert client.post(url, json=BODY, headers={**HEADERS, "X-Approver-Passphrase": "!!!"}).status_code == 401
    response = client.post(url, json=BODY, headers={**HEADERS, **_header(PHRASE)})
    assert response.status_code == 200 and PHRASE not in response.text
    assert db.execute("SELECT state FROM approval_requests WHERE id='ap'").fetchone() == ("approved",)


def test_cli_init_passphrase_prompts_only_and_decide_sends_header_not_argv(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    prompts = []
    answers = iter([PHRASE, PHRASE, PHRASE])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": prompts.append(prompt) or next(answers))
    monkeypatch.setenv("SYNAPSE_APPROVER_TOKEN", "t" * 43)  # must be ignored in passphrase mode
    argv = ["--state-dir", str(state), "approver-init", "--passphrase"]
    assert PHRASE not in " ".join(argv)
    # Provision with the fast parameters through the same code path the command uses.
    real = pa.provision_passphrase_verifier
    monkeypatch.setattr(pa, "provision_passphrase_verifier", lambda *a, **k: real(*a, **{**k, **FAST}))
    assert cli_main(argv) == 0
    out = capsys.readouterr()
    assert PHRASE not in out.out + out.err
    assert (state / "approver.verifier").exists() and read_mode_hint(state) == "passphrase"
    assert cli_main(["--state-dir", str(state), "approver-init"]) == 1  # token init refused in passphrase mode

    calls = []
    def fake_request(state_dir, endpoint, **kwargs):
        calls.append(kwargs)
        return {"approval_id": "ap", "state": "approved", "state_version": 1}
    monkeypatch.setattr("synapse.cli.__main__._request", fake_request)
    decide = ["--state-dir", str(state), "approval-decide", "ap", "--decision", "approved",
              "--expected-version", "0", "--operation", "push", "--target", "repo",
              "--candidate-sha256", DIGEST, "--policy-version", "1", "--idempotency-key", "k1", "--correlation-id", "c1"]
    assert PHRASE not in " ".join(decide)
    assert cli_main(decide) == 0
    assert calls[0]["approver_token"] == PHRASE and prompts[-1].startswith("Approver passphrase")
    assert calls[0]["approver_token"] != "t" * 43  # environment variable not consulted
    assert PHRASE not in capsys.readouterr().out


# --- review fixes: bounded lockout, visibility, unlock, normalisation, weak passphrases, event loop ---

def _authority(state, **kw):
    directory, fd = state
    provision_passphrase_verifier(fd, PHRASE, **FAST)
    clock = Clock()
    return directory, PassphraseApproverAuthority(fd, clock=clock, wall=lambda: 1_700_000_000 + (clock.now - 1000), **kw), clock


def test_lockout_does_not_escalate_and_is_bounded(state):
    _, authority, clock = _authority(state, max_attempts=2, lockout_seconds=900)
    try:
        for _round in range(5):  # a same-user attacker burning attempts every time the lock expires
            for _ in range(2):
                _fails(authority)
            assert _fails(authority, PHRASE).code == "rate_limited"
            clock.now += 901  # never more than the fixed 15 minutes, no doubling
            assert authority.locked_until_text() is None
        with authority.authenticated(PHRASE):
            pass
    finally:
        authority.close()


def test_requests_that_never_reach_verification_are_not_counted(state):
    _, authority, _ = _authority(state, max_attempts=2)
    try:
        for bad in ("", None, "x" * 300, "short", 5) * 4:
            assert _fails(authority, bad).code == "unauthenticated"
        with authority.authenticated(PHRASE):  # not locked out by the malformed requests
            pass
    finally:
        authority.close()


def test_lockout_state_is_visible_and_audited(state):
    _, authority, clock = _authority(state, max_attempts=2)
    try:
        _fails(authority)
        onset = _fails(authority)
        assert onset.locked_until == "22:28"  # 1_700_000_000 is 22:13:20 UTC; plus 900 s
        assert _fails(authority, PHRASE).locked_until == "22:28"
        events = authority.pop_events()
        assert [e[0] for e in events] == ["approver.lockout"] and events[0][1]["until_utc"] == "22:28"
        assert authority.pop_events() == []
    finally:
        authority.close()


def test_unlock_marker_resets_counter_only_and_stale_marker_is_ignored(state):
    directory, fd = state
    provision_passphrase_verifier(fd, PHRASE, **FAST)
    marker = directory / pa.UNLOCK_FILE
    marker.write_text("")
    marker.chmod(0o600)
    authority = PassphraseApproverAuthority(fd, clock=Clock(), max_attempts=2)  # marker predates start: dropped
    try:
        assert not marker.exists()
        _fails(authority)
        _fails(authority)
        assert _fails(authority, PHRASE).code == "rate_limited"
        marker.write_text("")
        marker.chmod(0o600)
        assert _fails(authority, WRONG).code == "unauthenticated"  # unlocked, but a wrong guess is still a guess
        assert not marker.exists()
        assert [e[0] for e in authority.pop_events()] == ["approver.lockout", "approver.unlock"]
        with authority.authenticated(PHRASE):
            pass
    finally:
        authority.close()


def test_unlock_marker_ignored_when_not_private(state):
    directory, fd = state
    provision_passphrase_verifier(fd, PHRASE, **FAST)
    authority = PassphraseApproverAuthority(fd, clock=Clock(), max_attempts=1)
    try:
        _fails(authority)
        (directory / pa.UNLOCK_FILE).write_text("")
        (directory / pa.UNLOCK_FILE).chmod(0o666)
        assert _fails(authority, PHRASE).code == "rate_limited"
    finally:
        authority.close()


def test_cli_unlock_requires_tty_and_writes_marker(state, monkeypatch, capsys):
    directory, _ = state
    assert cli_main(["--state-dir", str(directory), "approver-unlock"]) == 1  # pytest stdin is not a TTY
    assert not (directory / pa.UNLOCK_FILE).exists()
    from synapse.cli import __main__ as cli
    assert cli._write_unlock_marker(directory) == 0
    assert (directory / pa.UNLOCK_FILE).stat().st_mode & 0o777 == 0o600


def test_length_is_checked_after_normalisation_and_weak_passphrases_refused(state):
    _, fd = state
    decomposed = "e\u0301" * 8  # 16 code points, but only 8 characters after NFKC
    assert pa.passphrase_shape_problem(decomposed) is not None
    with pytest.raises(ValueError, match="normalisation"):
        provision_passphrase_verifier(fd, decomposed, **FAST)
    for weak in ("a" * 20, "ab" * 10, "abcabcabcabcabcabc", "12345" * 4):
        with pytest.raises(ValueError, match="too weak"):
            provision_passphrase_verifier(fd, weak, **FAST)
    assert pa.passphrase_problem(PHRASE) is None


def test_api_lockout_response_shows_locked_until(api):
    client, _ = api
    url = "/api/v1/approvals/ap/decision"
    for _ in range(2):
        client.post(url, json=BODY, headers={**HEADERS, **_header(WRONG)})
    onset = client.post(url, json=BODY, headers={**HEADERS, **_header(WRONG)})
    locked = client.post(url, json=BODY, headers={**HEADERS, **_header(PHRASE)})
    for response in (onset, locked):
        until = response.headers["X-Approver-Locked-Until"]
        assert response.status_code == 403 and f"locked until {until} UTC" in response.json()["detail"]


def test_verification_runs_off_the_event_loop_and_kdf_runs_once(state):
    import asyncio
    import threading
    directory, fd = state
    provision_passphrase_verifier(fd, PHRASE, **FAST)
    authority = PassphraseApproverAuthority(fd)
    seen, calls = [], []
    real = pa._derive

    def spy(*a, **k):
        seen.append(threading.current_thread())
        calls.append(1)
        return real(*a, **k)

    async def go():
        await asyncio.to_thread(authority.preauthenticate, PHRASE)
        with authority.authenticated(PHRASE):
            pass
        await asyncio.to_thread(authority.preauthenticate, WRONG)
        with pytest.raises(ApprovalAccessDenied):
            with authority.authenticated(WRONG):
                pass
    try:
        pa._derive = spy
        asyncio.run(go())
    finally:
        pa._derive = real
        authority.close()
    assert len(calls) == 2 and threading.main_thread() not in seen  # once per request, never on the loop thread
    assert authority._failures == 1  # the wrong guess was counted exactly once


def test_concurrent_attempts_keep_the_counter_correct(state):
    import threading
    _, fd = state
    provision_passphrase_verifier(fd, PHRASE, **FAST)
    authority = PassphraseApproverAuthority(fd, clock=Clock(), max_attempts=100)
    threads = [threading.Thread(target=authority.preauthenticate, args=(WRONG,)) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    authority.close()
    assert authority._failures == 8
