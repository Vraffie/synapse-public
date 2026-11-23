"""Safe local backup, verification and offline restore of the daemon database.

Scope: the SQLite database only. ``operator.token``, ``approver.token`` and the
``artifacts/`` blob directory are never read or copied; tokens are re-provisioned
after a restore.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat

from synapse.infrastructure.daemon_lock import DaemonAlreadyRunning, daemon_lock
from synapse.persistence.audit_chain import append_audit
from synapse.persistence.migrations import MIGRATION_RE, MigrationError, apply_migrations

DB_NAME = "synapse.sqlite"
_SIDECARS = ("-wal", "-shm")
_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")


class BackupError(RuntimeError):
    """Backup, verification or restore refused or failed."""


@dataclass(frozen=True)
class BackupInfo:
    schema_version: int
    size: int


def code_schema_version(migrations: Path) -> int:
    """Highest migration version known to this code."""
    versions = [int(m.group("version")) for p in migrations.glob("*.sql")
                if (m := MIGRATION_RE.fullmatch(p.name))]
    if not versions:
        raise BackupError(f"no migrations found in {migrations}")
    return max(versions)


def _schema_objects(db: sqlite3.Connection) -> dict[tuple[str, str], tuple[str, str | None]]:
    """(type, name) -> (table, whitespace-normalised SQL) for every non-internal schema object."""
    objects = {}
    for kind, name, table, sql in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"):
        objects[(kind, name)] = (table, " ".join(sql.split()) if sql else None)
    return objects


def schema_fingerprint(migrations: Path, version: int) -> dict[tuple[str, str], tuple[str, str | None]]:
    """Expected tables/indexes/triggers/views after applying migrations 1..``version`` to a scratch DB."""
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as scratch:
        apply_migrations(scratch, migrations, limit=version)
        return _schema_objects(scratch)


def _check_schema(db: sqlite3.Connection, migrations: Path, version: int) -> None:
    expected = schema_fingerprint(migrations, version)
    actual = _schema_objects(db)
    missing = sorted(f"{k[0]} {k[1]}" for k in expected.keys() - actual.keys())
    extra = sorted(f"{k[0]} {k[1]}" for k in actual.keys() - expected.keys())
    changed = sorted(f"{k[0]} {k[1]}" for k in expected.keys() & actual.keys() if expected[k] != actual[k])
    if missing or extra or changed:
        parts = [f"{label}: {', '.join(names[:5])}{' ...' if len(names) > 5 else ''}"
                 for label, names in (("missing", missing), ("unexpected", extra), ("modified", changed)) if names]
        raise BackupError("schema does not match migrations 1.." + str(version) + " (" + "; ".join(parts) + ")")


def _open_dir(path: Path, *, nofollow: bool) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | (os.O_NOFOLLOW if nofollow else 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise BackupError(f"cannot open directory {path}: {exc.strerror}") from exc
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        os.close(fd)
        raise BackupError(f"directory {path} must be user-owned and not group/other-writable")
    return fd


def _ro_uri(path: Path) -> str:
    return "file:" + str(path).replace("%", "%25").replace("?", "%3f").replace("#", "%23") + "?mode=ro"


def verify_backup(path: Path, migrations: Path) -> BackupInfo:
    """Check integrity and schema version of a backup file without modifying it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise BackupError(f"cannot open backup {path}: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BackupError("backup must be a regular file")
        if info.st_uid != os.geteuid():
            raise BackupError("backup must be user-owned")
        with open(fd, "rb", closefd=False) as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise BackupError("not a SQLite database")
    finally:
        os.close(fd)
    return _verify_database(path, migrations)


def _verify_database(path: Path, migrations: Path) -> BackupInfo:
    code_version = code_schema_version(migrations)
    try:
        with closing(sqlite3.connect(_ro_uri(path), uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BackupError("integrity_check failed")
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise BackupError("foreign_key_check failed")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
                raise BackupError("backup has no schema_migrations table")
            applied = sorted(r[0] for r in db.execute("SELECT version FROM schema_migrations"))
            if applied and applied[-1] > code_version:
                raise BackupError(f"backup schema version {applied[-1]} is newer than this code ({code_version})")
            if not applied or applied != list(range(1, len(applied) + 1)):
                raise BackupError("backup migration history is empty or not contiguous")
            _check_schema(db, migrations, applied[-1])
    except (sqlite3.Error, UnicodeError, MigrationError) as exc:
        raise BackupError(f"backup is not a valid database: {exc}") from exc
    return BackupInfo(applied[-1], path.stat().st_size)


RESTORE_PRINCIPAL = "system:restore"
RESTORE_REASON = "restored from backup: approvals are not carried across a restore"


def neutralise_approvals(path: Path) -> int:
    """Revoke every still-``requested`` or ``approved`` approval in the (private, offline) DB at ``path``.

    A snapshot can be older than the approvals' real history: an approval consumed after the
    snapshot reappears as approved with an unresolved gate. Revocation is terminal and a
    parked task whose gate approval is revoked fails closed on the next gate pass.
    Writes one audit row per approval. Returns how many approvals were revoked.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        with closing(sqlite3.connect(str(path), isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                ids = [r[0] for r in db.execute(
                    "SELECT id FROM approval_requests WHERE state IN ('requested','approved') ORDER BY id")]
                # state_version exists only from migration 0003; a v1/v2 backup is neutralised as-is
                # (the daemon migrates it on next open) rather than altered here.
                has_version = any(r[1] == "state_version" for r in db.execute("PRAGMA table_info(approval_requests)"))
                bump = "state_version=state_version+1," if has_version else ""
                for approval_id in ids:
                    db.execute("UPDATE approval_requests SET state='revoked'," + bump +
                               "decided_at=?,decision_principal=?,decision_reason=? WHERE id=?",
                               (now, RESTORE_PRINCIPAL, RESTORE_REASON, approval_id))
                    append_audit(db, RESTORE_PRINCIPAL, "approval.restore_revoke", approval_id, "allowed", None,
                                 '{"reason":"restore"}', now)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
    except sqlite3.Error as exc:
        raise BackupError(f"could not neutralise approvals: {exc}") from exc
    return len(ids)


def _create_temp(dir_fd: int, stem: str) -> str:
    name = f".{stem}.tmp-{secrets.token_hex(8)}"
    os.close(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=dir_fd))
    return name


def _remove_temp(dir_fd: int, name: str) -> None:
    for tail in ("", "-journal", "-wal", "-shm"):
        try:
            os.unlink(name + tail, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


def _snapshot(source: Path, target: Path) -> None:
    """Consistent online copy of ``source`` into the empty file ``target``."""
    try:
        with closing(sqlite3.connect(_ro_uri(source), uri=True, timeout=30)) as src, \
                closing(sqlite3.connect(str(target), isolation_level=None)) as dst:
            src.backup(dst)
            # One self-contained file: no -wal/-shm sidecars needed to read it.
            dst.execute("PRAGMA journal_mode = DELETE")
    except sqlite3.Error as exc:
        raise BackupError(f"snapshot failed: {exc}") from exc


def _fsync_name(dir_fd: int, name: str) -> None:
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_backup(state_dir: Path, dest: Path, migrations: Path) -> BackupInfo:
    """Write a verified online snapshot of the state DB to a new 0600 file.

    Safe while the daemon runs (SQLite online backup API on a read-only
    connection). Never overwrites ``dest`` and never follows a symlink at it.
    """
    os.close(_open_dir(state_dir, nofollow=True))
    database = state_dir / DB_NAME
    try:
        info = database.lstat()
    except OSError as exc:
        raise BackupError(f"cannot read database: {exc.strerror}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise BackupError("database must be a regular file")
    name = dest.name
    if not _SAFE_NAME.fullmatch(name) or name in (".", ".."):
        raise BackupError("destination file name must be 1-200 characters of A-Za-z0-9._-")
    try:
        parent = dest.parent.resolve(strict=True)
    except OSError as exc:
        raise BackupError(f"destination directory unavailable: {exc.strerror}") from exc
    dir_fd = _open_dir(parent, nofollow=False)
    tmp = None
    try:
        try:
            os.lstat(name, dir_fd=dir_fd)  # lstat: a dangling symlink also counts as existing
        except FileNotFoundError:
            pass
        else:
            raise BackupError("destination already exists; refusing to overwrite")
        tmp = _create_temp(dir_fd, name)
        _snapshot(database, parent / tmp)
        _verify_database(parent / tmp, migrations)
        _fsync_name(dir_fd, tmp)
        try:  # link(2) never replaces an existing name, unlike rename(2)
            os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        except FileExistsError:
            raise BackupError("destination already exists; refusing to overwrite") from None
        os.fsync(dir_fd)
        return _verify_database(parent / name, migrations)
    finally:
        if tmp is not None:
            _remove_temp(dir_fd, tmp)
        os.close(dir_fd)


def restore_backup(state_dir: Path, backup: Path, migrations: Path) -> Path | None:
    """Replace the state DB from a verified backup. Offline only.

    Holds the daemon ownership lock throughout and refuses if it is held. The
    previous database (and any WAL/SHM) is kept under a ``.pre-restore-*`` name.
    Tokens and artifacts in the state directory are not touched. Returns the
    aside path, or None if there was no previous database.
    """
    state_fd = _open_dir(state_dir, nofollow=True)
    try:
        if os.fstat(state_fd).st_mode & 0o077:
            raise BackupError("state directory must be private")
        lock = daemon_lock(state_dir / (DB_NAME + ".lock"))
        try:
            lock.__enter__()
        except DaemonAlreadyRunning:
            raise BackupError("daemon is running (ownership lock held); stop it before restoring") from None
        try:
            verify_backup(backup, migrations)
            tmp = _create_temp(state_fd, DB_NAME)
            try:
                _snapshot(backup, state_dir / tmp)  # fresh private copy; source untouched
                _verify_database(state_dir / tmp, migrations)
                neutralise_approvals(state_dir / tmp)
                _verify_database(state_dir / tmp, migrations)
                _fsync_name(state_fd, tmp)
                aside = None
                try:
                    existing = os.lstat(DB_NAME, dir_fd=state_fd)
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    if not stat.S_ISREG(existing.st_mode):
                        raise BackupError("existing database is not a regular file")
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    aside = f"{DB_NAME}.pre-restore-{stamp}-{secrets.token_hex(3)}"
                    for tail in ("",) + _SIDECARS:
                        try:
                            os.link(DB_NAME + tail, aside + tail, src_dir_fd=state_fd,
                                    dst_dir_fd=state_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            if tail == "":
                                raise
                    for tail in ("",) + _SIDECARS:
                        try:
                            os.unlink(DB_NAME + tail, dir_fd=state_fd)
                        except FileNotFoundError:
                            pass
                os.link(tmp, DB_NAME, src_dir_fd=state_fd, dst_dir_fd=state_fd, follow_symlinks=False)
                os.fsync(state_fd)
                return state_dir / aside if aside else None
            finally:
                _remove_temp(state_fd, tmp)
        finally:
            lock.__exit__(None, None, None)
    finally:
        os.close(state_fd)
