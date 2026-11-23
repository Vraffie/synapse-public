from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
import threading

import pytest

from synapse.cli.__main__ import main
from synapse.infrastructure.daemon_lock import daemon_lock
from synapse.persistence.backup import BackupError, create_backup, restore_backup, verify_backup
from synapse.persistence.migrations import apply_migrations, open_database

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
TOKEN = "T" * 43


@pytest.fixture
def state(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    with closing(open_database(state / "synapse.sqlite")) as db:
        apply_migrations(db, MIGRATIONS)
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
    (state / "operator.token").write_text(TOKEN)
    (state / "approver.token").write_text(TOKEN)
    dest = tmp_path / "out"
    dest.mkdir(mode=0o700)
    return state, dest


def _workflows(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT count(*) FROM workflow_versions").fetchone()[0]


def test_backup_is_private_verified_and_token_free(state):
    state_dir, out = state
    info = create_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    assert info.schema_version >= 9
    target = out / "b.sqlite"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in out.iterdir()] == ["b.sqlite"]  # temp and sidecars cleaned up
    assert TOKEN.encode() not in target.read_bytes()
    assert verify_backup(target, MIGRATIONS).schema_version == info.schema_version
    assert _workflows(target) == 1


def test_backup_consistent_with_concurrent_writes(state):
    state_dir, out = state
    stop = threading.Event()
    errors = []

    def writer():
        try:
            with closing(open_database(state_dir / "synapse.sqlite")) as db:
                i = 0
                while not stop.is_set():
                    i += 1
                    db.execute("INSERT INTO workflow_versions VALUES(?,?,?,?,?)", (f"w{i}", "1", "{}", "h", "now"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for n in range(3):
            create_backup(state_dir, out / f"b{n}.sqlite", MIGRATIONS)
    finally:
        stop.set()
        thread.join()
    assert not errors
    for n in range(3):
        assert verify_backup(out / f"b{n}.sqlite", MIGRATIONS)
        assert _workflows(out / f"b{n}.sqlite") >= 1


def test_existing_destination_not_overwritten(state):
    state_dir, out = state
    (out / "b.sqlite").write_bytes(b"precious")
    with pytest.raises(BackupError, match="already exists"):
        create_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    assert (out / "b.sqlite").read_bytes() == b"precious"
    assert [p.name for p in out.iterdir()] == ["b.sqlite"]


def test_symlink_destination_refused(state, tmp_path):
    state_dir, out = state
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    (out / "link").symlink_to(victim)
    (out / "dangling").symlink_to(tmp_path / "nowhere")
    for name in ("link", "dangling"):
        with pytest.raises(BackupError):
            create_backup(state_dir, out / name, MIGRATIONS)
    assert victim.read_bytes() == b"keep" and not (tmp_path / "nowhere").exists()
    with pytest.raises(BackupError):
        verify_backup(out / "link", MIGRATIONS)


def test_destination_directory_must_be_safe(state, tmp_path):
    state_dir, _ = state
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o777)
    with pytest.raises(BackupError, match="user-owned"):
        create_backup(state_dir, open_dir / "b", MIGRATIONS)
    with pytest.raises(BackupError):
        create_backup(state_dir, tmp_path / "missing" / "b", MIGRATIONS)


def test_corrupt_backup_rejected(state):
    state_dir, out = state
    good = out / "good.sqlite"
    create_backup(state_dir, good, MIGRATIONS)
    data = bytearray(good.read_bytes())
    for offset in range(4096 * 2, len(data), 97):
        data[offset] ^= 0xFF
    bad = out / "bad.sqlite"
    bad.write_bytes(bytes(data))
    with pytest.raises(BackupError):
        verify_backup(bad, MIGRATIONS)
    junk = out / "junk"
    junk.write_bytes(b"not a database")
    with pytest.raises(BackupError, match="SQLite"):
        verify_backup(junk, MIGRATIONS)


def test_newer_version_backup_rejected_and_restore_leaves_state(state):
    state_dir, out = state
    newer = out / "newer.sqlite"
    create_backup(state_dir, newer, MIGRATIONS)
    with closing(sqlite3.connect(newer)) as db:
        db.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (9999, 'x')")
        db.commit()
    with pytest.raises(BackupError):
        verify_backup(newer, MIGRATIONS)
    before = (state_dir / "synapse.sqlite").read_bytes()
    with pytest.raises(BackupError, match="newer"):
        restore_backup(state_dir, newer, MIGRATIONS)
    assert (state_dir / "synapse.sqlite").read_bytes() == before


def test_restore_keeps_replaced_db_aside_and_not_tokens(state):
    state_dir, out = state
    create_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    with closing(open_database(state_dir / "synapse.sqlite")) as db:
        db.execute("INSERT INTO workflow_versions VALUES('wf2','1','{}','hash','now')")
    aside = restore_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    assert _workflows(state_dir / "synapse.sqlite") == 1
    assert _workflows(aside) == 2
    assert stat.S_IMODE((state_dir / "synapse.sqlite").stat().st_mode) == 0o600
    assert (state_dir / "operator.token").read_text() == TOKEN
    assert not [p for p in state_dir.iterdir() if p.name.startswith(".")]
    # Daemon-style open still works after restore.
    with closing(open_database(state_dir / "synapse.sqlite")) as db:
        assert apply_migrations(db, MIGRATIONS) == []


def test_restore_refused_while_daemon_lock_held(state):
    state_dir, out = state
    create_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    before = (state_dir / "synapse.sqlite").read_bytes()
    with daemon_lock(state_dir / "synapse.sqlite.lock"):
        with pytest.raises(BackupError, match="daemon is running"):
            restore_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    assert (state_dir / "synapse.sqlite").read_bytes() == before
    assert not [p for p in state_dir.iterdir() if "pre-restore" in p.name]


def test_backup_allowed_while_daemon_lock_held(state):
    state_dir, out = state
    with daemon_lock(state_dir / "synapse.sqlite.lock"):
        create_backup(state_dir, out / "b.sqlite", MIGRATIONS)


def test_cli_roundtrip_and_errors(state, capsys):
    state_dir, out = state
    base = ["--state-dir", str(state_dir)]
    assert main(base + ["backup", str(out / "b.sqlite")]) == 0
    assert main(base + ["backup", str(out / "b.sqlite")]) == 1
    assert main(base + ["backup-verify", str(out / "b.sqlite")]) == 0
    with daemon_lock(state_dir / "synapse.sqlite.lock"):
        assert main(base + ["restore", str(out / "b.sqlite")]) == 1
    assert main(base + ["restore", str(out / "b.sqlite")]) == 0
    text = capsys.readouterr()
    assert TOKEN not in text.out + text.err
    assert "daemon is running" in text.err


# --- F8: schema fingerprint and restore neutralisation -------------------------------------

def _mutate(path, *statements):
    with closing(sqlite3.connect(path)) as db:
        for statement in statements:
            db.execute(statement)
        db.commit()


def _triggers(path):
    with closing(sqlite3.connect(path)) as db:
        return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]


def test_backup_without_triggers_is_refused(state):
    # Review experiment: copy a DB, drop every trigger -> used to verify cleanly.
    state_dir, out = state
    bad = out / "notrig.sqlite"
    create_backup(state_dir, bad, MIGRATIONS)
    names = _triggers(bad)
    assert len(names) > 30
    _mutate(bad, *[f'DROP TRIGGER "{n}"' for n in names])
    with pytest.raises(BackupError, match="missing: trigger"):
        verify_backup(bad, MIGRATIONS)
    before = (state_dir / "synapse.sqlite").read_bytes()
    with pytest.raises(BackupError, match="schema does not match"):
        restore_backup(state_dir, bad, MIGRATIONS)
    assert (state_dir / "synapse.sqlite").read_bytes() == before


@pytest.mark.parametrize("statement,fragment", [
    ("CREATE TRIGGER evil AFTER INSERT ON runs BEGIN SELECT 1; END", "unexpected: trigger evil"),
    ("CREATE VIEW v AS SELECT 1", "unexpected: view v"),
    ("CREATE INDEX extra_idx ON runs(state)", "unexpected: index extra_idx"),
    ("DROP TRIGGER approval_no_delete", "missing: trigger approval_no_delete"),
])
def test_schema_object_tampering_refused(state, statement, fragment):
    state_dir, out = state
    bad = out / "bad.sqlite"
    create_backup(state_dir, bad, MIGRATIONS)
    _mutate(bad, statement)
    with pytest.raises(BackupError, match=fragment):
        verify_backup(bad, MIGRATIONS)


def test_modified_trigger_body_refused(state):
    state_dir, out = state
    bad = out / "mod.sqlite"
    create_backup(state_dir, bad, MIGRATIONS)
    _mutate(bad, "DROP TRIGGER approval_no_delete",
            "CREATE TRIGGER approval_no_delete BEFORE DELETE ON approval_requests BEGIN SELECT 1; END")
    with pytest.raises(BackupError, match="modified: trigger approval_no_delete"):
        verify_backup(bad, MIGRATIONS)


def test_older_backup_checked_against_its_own_version(state, tmp_path):
    older = tmp_path / "older.sqlite"
    with closing(open_database(older)) as db:
        apply_migrations(db, MIGRATIONS, limit=12)
        db.execute("PRAGMA journal_mode = DELETE")
    assert verify_backup(older, MIGRATIONS).schema_version == 12


def _approvals_fixture(state_dir):
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with closing(open_database(state_dir / "synapse.sqlite")) as db:
        db.execute("INSERT INTO runs VALUES('run','wf','1','running',0,'now','now')")
        db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,max_concurrency,created_at) VALUES('mock','mock','Mock','mock',6,'now')")
        db.execute("INSERT INTO tasks(id,run_id,node_id,state,state_version,assigned_profile_id,attempt_count,created_at,updated_at) VALUES('t','run','t','running',3,'mock',1,'now','now')")
        for aid, st in (("ap-approved", "approved"), ("ap-requested", "requested"), ("ap-rejected", "rejected")):
            db.execute("INSERT INTO approval_requests(id,run_id,operation,target,candidate_manifest_sha256,policy_version,state,expires_at,requested_at,decided_at,decision_principal,requester_subject,requester_kind) "
                       "VALUES(?,'run','push','repo',?,'1','requested',?,'now',NULL,NULL,'agent-1','agent')", (aid, "a" * 64, future))
        db.execute("INSERT INTO task_approval_gates(task_id,approval_id,run_id,created_at) VALUES('t','ap-approved','run','now')")
        db.execute("UPDATE tasks SET state='awaiting_approval' WHERE id='t'")
        db.execute("UPDATE approval_requests SET state='approved',decided_at='now',decision_principal='human:h',state_version=1 WHERE id='ap-approved'")
        db.execute("UPDATE approval_requests SET state='rejected',decided_at='now',decision_principal='human:h',state_version=1 WHERE id='ap-rejected'")


def test_restore_revokes_resurrected_approvals_and_fails_gated_task_closed(state):
    from synapse.persistence.task_approval_gate import resolve_task_approval_gate
    state_dir, out = state
    _approvals_fixture(state_dir)
    create_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    restore_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    with closing(open_database(state_dir / "synapse.sqlite")) as db:
        states = dict(db.execute("SELECT id,state FROM approval_requests"))
        assert states == {"ap-approved": "revoked", "ap-requested": "revoked", "ap-rejected": "rejected"}
        who, reason = db.execute("SELECT decision_principal,decision_reason FROM approval_requests WHERE id='ap-approved'").fetchone()
        assert who == "system:restore" and "restore" in reason
        assert db.execute("SELECT count(*) FROM audit_entries WHERE action='approval.restore_revoke'").fetchone()[0] == 2
        # The gated task is still parked (never silently resumed) ...
        assert db.execute("SELECT state FROM tasks WHERE id='t'").fetchone()[0] == "awaiting_approval"
        # ... and cannot resume on the resurrected approval: gate resolution fails it closed.
        out_state = resolve_task_approval_gate(db, task_id="t", expected_task_version=db.execute("SELECT state_version FROM tasks WHERE id='t'").fetchone()[0], principal="mock-engine")
        assert out_state == "failed"
    # The source backup file is untouched (still contains the approved approval).
    assert dict(sqlite3.connect(out / "b.sqlite").execute("SELECT id,state FROM approval_requests"))["ap-approved"] == "approved"


# --- review follow-ups: old schemas, fail-closed ordering, audit rows -------------------------

def _simple_approvals(state_dir, *states):
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with closing(open_database(state_dir / "synapse.sqlite")) as db:
        db.execute("INSERT INTO runs VALUES('run','wf','1','running',0,'now','now')")
        for i, st in enumerate(states):
            db.execute("INSERT INTO approval_requests(id,run_id,operation,target,candidate_manifest_sha256,policy_version,state,expires_at,requested_at,decided_at,decision_principal,requester_subject,requester_kind) "
                       "VALUES(?,'run','push','repo',?,'1','requested',?,'now',NULL,NULL,'agent-1','agent')", (f"ap{i}", "a" * 64, future))
            if st == "approved":
                db.execute("UPDATE approval_requests SET state='approved',decided_at='now',decision_principal='human:h',state_version=1 WHERE id=?", (f"ap{i}",))


def _audit_rows(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT resource FROM audit_entries WHERE action='approval.restore_revoke' ORDER BY resource").fetchall()


def test_restore_v2_schema_backup_with_open_approvals(state):
    state_dir, out = state
    old = out / "v2.sqlite"
    with closing(open_database(old)) as db:
        apply_migrations(db, MIGRATIONS, limit=2)
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','running',0,'now','now')")
        for aid, st in (("ap-req", "requested"), ("ap-appr", "approved")):
            db.execute("INSERT INTO approval_requests(id,run_id,operation,target,candidate_manifest_sha256,policy_version,state,expires_at,requested_at) "
                       "VALUES(?,'run','push','repo',?,'1',?,'2999-01-01T00:00:00+00:00','now')", (aid, "a" * 64, st))
        db.execute("PRAGMA journal_mode = DELETE")
    assert verify_backup(old, MIGRATIONS).schema_version == 2
    restore_backup(state_dir, old, MIGRATIONS)
    with closing(sqlite3.connect(state_dir / "synapse.sqlite")) as db:
        assert dict(db.execute("SELECT id,state FROM approval_requests")) == {"ap-req": "revoked", "ap-appr": "revoked"}
        assert db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 2
    assert _audit_rows(state_dir / "synapse.sqlite") == [("ap-appr",), ("ap-req",)]
    with closing(open_database(state_dir / "synapse.sqlite")) as db:  # the daemon upgrades it afterwards
        assert apply_migrations(db, MIGRATIONS)


@pytest.mark.parametrize("only", ["requested", "approved"])
def test_restore_single_approval_writes_exactly_one_audit_row(state, only):
    state_dir, out = state
    _simple_approvals(state_dir, only)
    create_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    restore_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    assert _audit_rows(state_dir / "synapse.sqlite") == [("ap0",)]
    with closing(sqlite3.connect(state_dir / "synapse.sqlite")) as db:
        assert db.execute("SELECT state FROM approval_requests").fetchone()[0] == "revoked"


def test_neutralise_failure_leaves_live_db_untouched(state, monkeypatch):
    import synapse.persistence.backup as backup
    state_dir, out = state
    _simple_approvals(state_dir, "approved")
    create_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    before = (state_dir / "synapse.sqlite").read_bytes()

    def boom(path):
        raise BackupError("could not neutralise approvals: injected")
    monkeypatch.setattr(backup, "neutralise_approvals", boom)
    with pytest.raises(BackupError, match="neutralise"):
        restore_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    assert (state_dir / "synapse.sqlite").read_bytes() == before
    assert not [p for p in state_dir.iterdir() if p.name.startswith(".") or "pre-restore" in p.name]


def test_post_neutralise_verify_failure_leaves_live_db_untouched(state, monkeypatch):
    import synapse.persistence.backup as backup
    state_dir, out = state
    _simple_approvals(state_dir, "approved")
    create_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    before = (state_dir / "synapse.sqlite").read_bytes()
    real, calls = backup._verify_database, []

    def verify(path, migrations):
        calls.append(path)
        if len(calls) == 3:  # 1 = backup (verify_backup), 2 = private copy, 3 = after neutralise
            raise BackupError("injected post-neutralise failure")
        return real(path, migrations)
    monkeypatch.setattr(backup, "_verify_database", verify)
    with pytest.raises(BackupError, match="injected"):
        restore_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    assert len(calls) == 3
    assert (state_dir / "synapse.sqlite").read_bytes() == before
    assert not [p for p in state_dir.iterdir() if p.name.startswith(".") or "pre-restore" in p.name]
