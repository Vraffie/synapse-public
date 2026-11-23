"""Read a plugin package once, into memory, and hash exactly those bytes.

The entrypoint that is executed and the files the plugin may read later are the bytes that were
hashed, so there is no window between "verified" and "used" for them. Symlinks, special files,
oversized or too many files, too many or too deep directories are refused.

Every primitive this verification path uses (SHA-256, the os calls, the stat tests) is bound once,
when this module is first imported, to a private name. Another plugin that later replaces
``hashlib.sha256`` or ``os.open`` therefore does not change how a package is hashed. This is a
narrow defence, not isolation: an in-process plugin can still reach this module's own globals (the
loader's tripwire watches them while ``register()`` runs; see docs/operations/plugin-framework.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256 as _sha256
from os import (O_CLOEXEC as _O_CLOEXEC, O_NOFOLLOW as _O_NOFOLLOW, O_NONBLOCK as _O_NONBLOCK,
                O_RDONLY as _O_RDONLY, close as _close, fspath as _fspath, fstat as _fstat, lstat as _lstat,
                open as _open, read as _read, scandir as _scandir)
from os.path import join as _join
from pathlib import Path
from stat import S_ISDIR as _S_ISDIR, S_ISLNK as _S_ISLNK, S_ISREG as _S_ISREG

from synapse.plugins.safe_text import bounded as _bounded

MAX_FILES = 500
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_DIRS = 64
MAX_DEPTH = 8
MAX_ENTRIES = 2000  # every directory entry seen, including skipped ones; checked while iterating
_SKIP_DIRS = frozenset({"__pycache__"})
_OPEN_FLAGS = _O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC


class PackageError(ValueError):
    """The package directory cannot be read safely. Messages carry only bounded, escaped names."""


@dataclass(frozen=True, slots=True)
class PackageSnapshot:
    files: dict[str, bytes]
    sha256: str


def _read_regular(path: str, expected: tuple[int, int], rel: str) -> bytes:
    """Open without following links and without blocking; check the opened fd itself, then read it."""
    fd = _open(path, _OPEN_FLAGS)
    try:
        info = _fstat(fd)
        if not _S_ISREG(info.st_mode):  # e.g. swapped for a FIFO or device after the directory scan
            raise PackageError(f"special file in package: {_bounded(rel)}")
        if (info.st_dev, info.st_ino) != expected:
            raise PackageError(f"package changed while it was read: {_bounded(rel)}")
        if info.st_size > MAX_FILE_BYTES:
            raise PackageError(f"package too large at {_bounded(rel)}")
        chunks, size = [], 0
        while size <= MAX_FILE_BYTES:
            chunk = _read(fd, min(1024 * 1024, MAX_FILE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)
    finally:
        _close(fd)


def snapshot_package(directory: Path) -> PackageSnapshot:
    root = _fspath(directory)
    try:
        info = _lstat(root)
    except OSError as exc:
        raise PackageError("package directory not readable") from exc
    if not _S_ISDIR(info.st_mode):
        raise PackageError("package path is not a real directory")
    files: dict[str, bytes] = {}
    total = entries = 0
    dirs = 1
    pending: list[tuple[str, str, int]] = [(root, "", 0)]
    while pending:
        current, prefix, depth = pending.pop()
        with _scandir(current) as listing:
            for entry in listing:  # iterated lazily, so a huge directory is refused without being listed
                entries += 1
                if entries > MAX_ENTRIES:
                    raise PackageError("too many directory entries in package")
                name = entry.name
                rel = prefix + name
                try:
                    rel.encode("utf-8")
                except UnicodeEncodeError:
                    raise PackageError("file name in package is not valid UTF-8") from None
                st = entry.stat(follow_symlinks=False)
                mode = st.st_mode
                if _S_ISLNK(mode):
                    raise PackageError(f"symlink in package: {_bounded(rel)}")
                if _S_ISDIR(mode):
                    if name in _SKIP_DIRS:
                        continue
                    dirs += 1
                    if dirs > MAX_DIRS:
                        raise PackageError("too many directories in package")
                    if depth + 1 > MAX_DEPTH:
                        raise PackageError("package directories nested too deep")
                    pending.append((_join(current, name), rel + "/", depth + 1))
                    continue
                if not _S_ISREG(mode):
                    raise PackageError(f"special file in package: {_bounded(rel)}")
                if len(files) >= MAX_FILES:
                    raise PackageError("too many files in package")
                data = _read_regular(_join(current, name), (st.st_dev, st.st_ino), rel)
                total += len(data)
                if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise PackageError(f"package too large at {_bounded(rel)}")
                files[rel] = data
    digest = _sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8") + b"\x00" + _sha256(files[rel]).hexdigest().encode() + b"\n")
    return PackageSnapshot(files, digest.hexdigest())
