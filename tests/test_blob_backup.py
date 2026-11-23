"""Blob backup: consistent with a database backup, verified, fail-closed, restorable."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path

import pytest

from synapse.application.blob_backup import (
    MANIFEST_NAME, BlobBackupError, backup_blobs, restore_blobs, verify_blob_backup,
)
from synapse.cli.__main__ import main
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.persistence.artifact_store import publish_artifact
from synapse.persistence.backup import create_backup, restore_backup
from synapse.persistence.migrations import apply_migrations, open_database

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
BLOBS = {b"alpha": "a1", b"beta" * 5000: "a2", b"": "a3"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def publish(db, store, data, name):
    publish_artifact(db, store, chunks=[data], artifact_id=name, media_type="text/plain",
                     provenance={}, principal="t", idempotency_key=name)


@pytest.fixture
def env(tmp_path):
    state = tmp_path / "state"
    (state / "artifacts").mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    store = ArtifactBlobStore(state / "artifacts", max_bytes=1 << 20)
    with closing(open_database(state / "synapse.sqlite")) as db:
        apply_migrations(db, MIGRATIONS)
        for data, name in BLOBS.items():
            publish(db, store, data, name)
        publish(db, store, b"alpha", "a-dup")  # two artifact rows, one blob
    db_backup = out / "db.sqlite"
    create_backup(state, db_backup, MIGRATIONS)
    return state, out, db_backup, store


def test_backup_verify_and_restore_round_trip(env, tmp_path):
    state, out, db_backup, store = env
    info = backup_blobs(state, db_backup, out / "blobs")
    assert info.blob_count == 3 and info.total_bytes == 5 + 20000
    assert verify_blob_backup(out / "blobs", db_backup) == info
    assert oct((out / "blobs").stat().st_mode & 0o777) == "0o700"
    assert all(p.stat().st_mode & 0o077 == 0 for p in (out / "blobs").iterdir())
    # Restore into a fresh state directory.
    fresh = tmp_path / "fresh"
    (fresh / "artifacts").mkdir(parents=True, mode=0o700)
    fresh.chmod(0o700)
    restore_backup(fresh, db_backup, MIGRATIONS)
    assert restore_blobs(fresh, out / "blobs", db_backup) == info
    restored = ArtifactBlobStore(fresh / "artifacts", max_bytes=1 << 20)
    for data in BLOBS:
        assert restored.read(sha(data)) == data


def test_blobs_published_after_the_db_snapshot_are_not_copied(env):
    state, out, db_backup, store = env
    store.put([b"later"])
    backup_blobs(state, db_backup, out / "blobs")
    assert not (out / "blobs" / sha(b"later")).exists()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "wrong-size"])
def test_source_problem_fails_closed_and_leaves_nothing(env, damage):
    state, out, db_backup, store = env
    target = state / "artifacts" / sha(b"alpha")
    if damage == "missing":
        target.unlink()
    elif damage == "corrupt":
        target.write_bytes(b"alphX")
    else:
        target.write_bytes(b"alpha!")
    with pytest.raises(BlobBackupError, match="blob"):
        backup_blobs(state, db_backup, out / "blobs")
    assert not (out / "blobs").exists()


def test_existing_destination_is_refused(env):
    state, out, db_backup, _ = env
    (out / "blobs").mkdir()
    with pytest.raises(BlobBackupError, match="already exists"):
        backup_blobs(state, db_backup, out / "blobs")


def test_verify_detects_tampering_and_mismatched_database(env, tmp_path):
    state, out, db_backup, _ = env
    backup_blobs(state, db_backup, out / "blobs")
    blob = out / "blobs" / sha(b"alpha")
    blob.write_bytes(b"alphX")
    with pytest.raises(BlobBackupError, match="corrupt"):
        verify_blob_backup(out / "blobs", db_backup)
    blob.write_bytes(b"alpha")
    verify_blob_backup(out / "blobs", db_backup)
    # A different database backup (here: one with another artifact) is refused.
    with closing(open_database(state / "synapse.sqlite")) as db:
        publish(db, store := ArtifactBlobStore(state / "artifacts", max_bytes=1 << 20), b"new", "a9")
    other = out / "other.sqlite"
    create_backup(state, other, MIGRATIONS)
    with pytest.raises(BlobBackupError, match="different database backup"):
        verify_blob_backup(out / "blobs", other)
    (out / "blobs" / MANIFEST_NAME).unlink()
    with pytest.raises(BlobBackupError, match="manifest missing"):
        verify_blob_backup(out / "blobs", db_backup)


def test_manifest_listing_extra_or_fewer_blobs_is_refused(env):
    state, out, db_backup, _ = env
    backup_blobs(state, db_backup, out / "blobs")
    path = out / "blobs" / MANIFEST_NAME
    manifest = json.loads(path.read_text())
    del manifest["blobs"][sha(b"alpha")]
    path.write_text(json.dumps(manifest))
    with pytest.raises(BlobBackupError, match="does not match"):
        verify_blob_backup(out / "blobs", db_backup)


def test_restore_refuses_unverified_backup_and_repairs_a_corrupt_target(env, tmp_path):
    state, out, db_backup, _ = env
    backup_blobs(state, db_backup, out / "blobs")
    (out / "blobs" / sha(b"alpha")).write_bytes(b"alphX")
    with pytest.raises(BlobBackupError):
        restore_blobs(state, out / "blobs", db_backup)
    (out / "blobs" / sha(b"alpha")).write_bytes(b"alpha")
    damaged = state / "artifacts" / sha(b"alpha")
    damaged.write_bytes(b"alphX")
    restore_blobs(state, out / "blobs", db_backup)
    assert damaged.read_bytes() == b"alpha"


def test_restore_needs_an_artifacts_directory(env, tmp_path):
    state, out, db_backup, _ = env
    backup_blobs(state, db_backup, out / "blobs")
    with pytest.raises(BlobBackupError, match="artifacts directory"):
        restore_blobs(tmp_path / "nowhere", out / "blobs", db_backup)


def test_symlinked_database_backup_is_refused(env, tmp_path):
    state, out, db_backup, _ = env
    link = out / "link.sqlite"
    link.symlink_to(db_backup)
    with pytest.raises(OSError):
        backup_blobs(state, link, out / "blobs")


def test_cli_round_trip_and_errors(env, capsys, tmp_path):
    state, out, db_backup, _ = env
    assert main(["--state-dir", str(state), "blobs-backup", str(out / "b"), "--db-backup", str(db_backup)]) == 0
    assert "3 blobs" in capsys.readouterr().out
    assert main(["--state-dir", str(state), "blobs-verify", str(out / "b"), "--db-backup", str(db_backup)]) == 0
    assert main(["--state-dir", str(state), "blobs-restore", str(out / "b"), "--db-backup", str(db_backup)]) == 0
    assert main(["--state-dir", str(state), "blobs-backup", str(out / "b"), "--db-backup", str(db_backup)]) == 1
    assert "already exists" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["db?x=1", "db%41", "a b#c", "with%3Fq"])
def test_odd_backup_file_names_read_the_right_file(env, name):
    import shutil
    state, out, db_backup, _ = env
    # Decoys that the old unescaped URI would have opened instead of the named file.
    decoy_db = out / "dbA"
    shutil.copy(db_backup, decoy_db)
    with closing(open_database(state / "synapse.sqlite")) as db:
        publish(db, ArtifactBlobStore(state / "artifacts", max_bytes=1 << 20), b"extra", "a-extra")
    other = out / "other.sqlite"
    create_backup(state, other, MIGRATIONS)  # one more artifact than db_backup
    odd = out / name
    shutil.copy(other, odd)
    odd.chmod(0o600)
    for decoy in (out / "db", decoy_db):
        shutil.copy(db_backup, decoy)
    info = backup_blobs(state, odd, out / "blobs")
    assert info.blob_count == 4  # read the odd file (with the extra blob), not the decoy
    assert verify_blob_backup(out / "blobs", odd) == info
    # The manifest belongs to `odd`; the decoys hold a different blob set and must be refused.
    with pytest.raises(BlobBackupError, match="different database backup"):
        verify_blob_backup(out / "blobs", db_backup)
    with pytest.raises(BlobBackupError, match="different database backup"):
        verify_blob_backup(out / "blobs", out / "db")


def test_group_or_other_accessible_database_backup_is_refused(env):
    state, out, db_backup, _ = env
    assert db_backup.stat().st_mode & 0o077 == 0  # agentctl backup creates 0600 files
    db_backup.chmod(0o644)
    with pytest.raises(BlobBackupError, match="no group/other access"):
        backup_blobs(state, db_backup, out / "blobs")


def test_cli_reports_blob_integrity_errors_cleanly(env, monkeypatch, capsys):
    from synapse.infrastructure.artifact_blobs import ArtifactIntegrityError
    import synapse.application.blob_backup as bb
    state, out, db_backup, _ = env

    def boom(*args, **kwargs):
        raise ArtifactIntegrityError("blob content does not match its address")

    monkeypatch.setattr(bb, "verify_blob_backup", boom)
    assert main(["--state-dir", str(state), "blobs-verify", str(out / "blobs"), "--db-backup", str(db_backup)]) == 1
    assert "ArtifactIntegrityError" in capsys.readouterr().err
