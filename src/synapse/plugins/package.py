"""Read a plugin package once, into memory, and hash exactly those bytes.

The entrypoint that is executed and the files the plugin may read later are the bytes that were
hashed, so there is no window between "verified" and "used" for them. Symlinks, special files,
oversized or too many files are refused.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat

MAX_FILES = 500
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
_SKIP_DIRS = frozenset({"__pycache__"})


class PackageError(ValueError):
    """The package directory cannot be read safely."""


@dataclass(frozen=True, slots=True)
class PackageSnapshot:
    files: dict[str, bytes]
    sha256: str


def snapshot_package(directory: Path) -> PackageSnapshot:
    root = Path(directory)
    try:
        info = os.lstat(root)
    except OSError as exc:
        raise PackageError("package directory not readable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise PackageError("package path is not a real directory")
    files: dict[str, bytes] = {}
    total = 0
    pending = [root]
    while pending:
        current = pending.pop()
        for entry in sorted(os.scandir(current), key=lambda e: e.name):
            rel = Path(entry.path).relative_to(root).as_posix()
            try:
                rel.encode("utf-8")
            except UnicodeEncodeError:
                raise PackageError("file name in package is not valid UTF-8") from None
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                raise PackageError(f"symlink in package: {rel}")
            if stat.S_ISDIR(mode):
                if entry.name not in _SKIP_DIRS:
                    pending.append(Path(entry.path))
                continue
            if not stat.S_ISREG(mode):
                raise PackageError(f"special file in package: {rel}")
            if len(files) >= MAX_FILES:
                raise PackageError("too many files in package")
            fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise PackageError(f"package too large at {rel}")
            files[rel] = data
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8") + b"\x00" + hashlib.sha256(files[rel]).hexdigest().encode() + b"\n")
    return PackageSnapshot(files, digest.hexdigest())
