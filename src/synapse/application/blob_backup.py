"""Backup, verification and restore of the artifact blobs a database backup references.

A database backup (``persistence.backup``) holds the ``artifacts`` metadata but not the
content-addressed blobs. This module copies exactly the blobs that a given, already
taken database backup references, so the two together form one consistent set.

Consistency rule: take the database backup first, then the blob backup from that
backup file, never from the live database. A blob is published before the transaction
that references it commits, so every blob a snapshot references already exists when
the snapshot is taken. Blobs created after the snapshot are not copied; they are
unreferenced by the restored database and harmless.

Every blob is read through ``ArtifactBlobStore`` (digest and size re-verified) and
written through it, so the copy is private (0600 files, 0700 directory) and verified.
A missing, corrupt or oversized blob fails the whole backup; nothing is half-written.
No credentials are involved: tokens are never stored in the blob directory.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
from urllib.parse import quote

from synapse.infrastructure.artifact_blobs import ArtifactBlobStore

MANIFEST_NAME = "MANIFEST.json"
MAX_BLOB_BYTES = 64 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BlobBackupError(RuntimeError):
    """A blob backup, verification or restore was refused."""


@dataclass(frozen=True, slots=True)
class BlobBackupInfo:
    blob_count: int
    total_bytes: int


def _regular_private_file(path: Path, what: str) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(fd)
        raise BlobBackupError(f"{what} must be a regular, user-owned file with no group/other access (mode 0600)")
    return fd


def _file_sha256(path: Path, what: str) -> str:
    fd = _regular_private_file(path, what)
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _references(db_backup: Path) -> dict[str, int]:
    """Return ``{sha256: byte_size}`` for every artifact the database backup references."""
    fd = _regular_private_file(db_backup, "database backup")
    os.close(fd)
    # Percent-encode the path: a raw '?' or '%' in a file name would otherwise be parsed as URI syntax.
    uri = f"file:{quote(str(db_backup.resolve()), safe='/')}?mode=ro&immutable=1"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        rows = connection.execute("SELECT sha256, byte_size FROM artifacts").fetchall()
    except sqlite3.Error as exc:
        raise BlobBackupError(f"cannot read artifacts from the database backup: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
    refs: dict[str, int] = {}
    for sha, size in rows:
        if not isinstance(sha, str) or not _SHA256.fullmatch(sha) or type(size) is not int or size < 0:
            raise BlobBackupError("database backup holds a malformed artifact row")
        if refs.setdefault(sha, size) != size:
            raise BlobBackupError(f"database backup records two sizes for blob {sha}")
    oversized = sorted(sha for sha, size in refs.items() if size > MAX_BLOB_BYTES)
    if oversized:
        raise BlobBackupError(f"blob larger than {MAX_BLOB_BYTES} bytes: {oversized[0]}")
    return refs


def _read_checked(store: ArtifactBlobStore, sha: str, size: int) -> bytes:
    try:
        data = store.read(sha)
    except FileNotFoundError:
        raise BlobBackupError(f"blob missing: {sha}") from None
    except (OSError, RuntimeError, ValueError) as exc:
        raise BlobBackupError(f"blob unreadable or corrupt: {sha} ({type(exc).__name__})") from exc
    if len(data) != size:
        raise BlobBackupError(f"blob size differs from the database record: {sha}")
    return data


def _problems(store: ArtifactBlobStore, refs: dict[str, int]) -> list[str]:
    found = []
    for sha, size in sorted(refs.items()):
        try:
            _read_checked(store, sha, size)
        except BlobBackupError as exc:
            found.append(str(exc))
    return found


def _fail(problems: list[str]) -> BlobBackupError:
    shown = "; ".join(problems[:5]) + (f"; and {len(problems) - 5} more" if len(problems) > 5 else "")
    return BlobBackupError(f"{len(problems)} blob problem(s): {shown}")


def _store(root: Path, refs: dict[str, int]) -> ArtifactBlobStore:
    return ArtifactBlobStore(root, max_bytes=max([1, *refs.values()]))


def backup_blobs(state_dir: Path, db_backup: Path, dest: Path) -> BlobBackupInfo:
    """Copy the blobs referenced by ``db_backup`` from ``state_dir/artifacts`` into a new ``dest``."""
    state_dir, db_backup, dest = Path(state_dir), Path(db_backup), Path(dest)
    refs = _references(db_backup)
    db_sha = _file_sha256(db_backup, "database backup")
    source = _store(state_dir / "artifacts", refs)
    problems = _problems(source, refs)
    if problems:
        raise _fail(problems)
    if not dest.parent.is_dir():
        raise BlobBackupError("destination parent directory does not exist")
    try:
        dest.mkdir(mode=0o700)  # refuses an existing path, including a symlink
    except FileExistsError:
        raise BlobBackupError("destination already exists") from None
    try:
        target = _store(dest, refs)
        total = 0
        for sha, size in sorted(refs.items()):
            target.put([_read_checked(source, sha, size)])
            total += size
        manifest = {"v": 1, "db_backup_sha256": db_sha, "blobs": dict(sorted(refs.items()))}
        fd = os.open(dest / MANIFEST_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        dir_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)  # only the directory this call created
        raise
    return BlobBackupInfo(len(refs), total)


def verify_blob_backup(backup_dir: Path, db_backup: Path) -> BlobBackupInfo:
    """Check that ``backup_dir`` holds every blob ``db_backup`` references, intact, for that exact file."""
    backup_dir, db_backup = Path(backup_dir), Path(db_backup)
    refs = _references(db_backup)
    try:
        fd = _regular_private_file(backup_dir / MANIFEST_NAME, "blob manifest")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            manifest = json.load(stream)
    except FileNotFoundError:
        raise BlobBackupError("blob manifest missing") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BlobBackupError("blob manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("v") != 1 or not isinstance(manifest.get("blobs"), dict):
        raise BlobBackupError("blob manifest has an unsupported shape")
    if manifest.get("db_backup_sha256") != _file_sha256(db_backup, "database backup"):
        raise BlobBackupError("blob backup was taken from a different database backup")
    if manifest["blobs"] != refs:
        raise BlobBackupError("blob manifest does not match the artifacts the database backup references")
    problems = _problems(_store(backup_dir, refs), refs)
    if problems:
        raise _fail(problems)
    return BlobBackupInfo(len(refs), sum(refs.values()))


def restore_blobs(state_dir: Path, backup_dir: Path, db_backup: Path) -> BlobBackupInfo:
    """Verify, then add the backed-up blobs to ``state_dir/artifacts`` (existing intact blobs are kept)."""
    info = verify_blob_backup(backup_dir, db_backup)
    refs = _references(Path(db_backup))
    source = _store(Path(backup_dir), refs)
    target_dir = Path(state_dir) / "artifacts"
    if not target_dir.is_dir():
        raise BlobBackupError("state directory has no artifacts directory (start the daemon once or create it, mode 0700)")
    target = _store(target_dir, refs)
    for sha, size in sorted(refs.items()):
        ref = target.put([_read_checked(source, sha, size)])
        if ref.sha256 != sha:
            raise BlobBackupError(f"restored blob has the wrong digest: {sha}")
    return info
