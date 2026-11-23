"""Private content-addressed artifact blob storage on a local filesystem."""
from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import re
import stat
from uuid import uuid4


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BlobReference:
    sha256: str
    byte_size: int
    repaired: bool = False


class ArtifactIntegrityError(RuntimeError):
    """A stored blob does not match its content address."""


class ArtifactBlobStore:
    """Store immutable blobs under their SHA-256 digest.

    The caller owns metadata, access authorization and database transactions.
    The root must already exist and reside on a local filesystem.
    """

    def __init__(self, root: str | Path, *, max_bytes: int) -> None:
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a nonnegative integer")
        self._root = Path(root)
        self._max_bytes = max_bytes

    def with_max_bytes(self, max_bytes: int) -> "ArtifactBlobStore":
        """Return a store over the same directory with a different size bound."""
        return ArtifactBlobStore(self._root, max_bytes=max_bytes)

    def _open_root(self) -> int:
        fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            os.close(fd)
            raise PermissionError("Artifact directory must be private and user-owned")
        return fd

    def put(self, chunks: Iterable[bytes]) -> BlobReference:
        root_fd = self._open_root()
        temp_name = ".tmp-" + uuid4().hex
        created = False
        try:
            fd = os.open(
                temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600, dir_fd=root_fd,
            )
            created = True
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(fd, "wb") as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("Artifact chunks must be bytes")
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise ValueError("Artifact exceeds maximum size")
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            name = digest.hexdigest()
            repaired = False
            try:
                os.link(temp_name, name, src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
            except FileExistsError:
                for _ in range(3):
                    try:
                        existing = self._read(root_fd, name)
                        if len(existing) != size:
                            raise ArtifactIntegrityError("Existing artifact size does not match")
                        break
                    except ArtifactIntegrityError:
                        quarantine = ".corrupt-" + name + "-" + uuid4().hex
                        try:
                            os.link(name, quarantine, src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            # A concurrent repair may have replaced the resolved inode.
                            continue
                        os.fsync(root_fd)
                        os.replace(temp_name, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                        created = False
                        repaired = True
                        _LOG.warning("Repaired corrupt artifact %s; preserved as %s", name, quarantine)
                        break
                else:
                    raise ArtifactIntegrityError("Could not verify or repair existing artifact")
            if created:
                os.unlink(temp_name, dir_fd=root_fd)
                created = False
            os.fsync(root_fd)
            return BlobReference(name, size, repaired)
        finally:
            try:
                if created:
                    os.unlink(temp_name, dir_fd=root_fd)
            finally:
                os.close(root_fd)

    def read(self, sha256: str) -> bytes:
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            raise ValueError("Invalid SHA-256 digest")
        root_fd = self._open_root()
        try:
            return self._read(root_fd, sha256)
        finally:
            os.close(root_fd)

    def _read(self, root_fd: int, name: str) -> bytes:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=root_fd)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise PermissionError("Artifact must be a private, user-owned regular file")
            if info.st_size > self._max_bytes:
                raise ArtifactIntegrityError("Stored artifact exceeds maximum size")
            data = stream.read(self._max_bytes + 1)
        if len(data) > self._max_bytes or hashlib.sha256(data).hexdigest() != name:
            raise ArtifactIntegrityError("Stored artifact does not match its digest")
        return data
