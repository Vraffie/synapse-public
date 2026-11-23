"""Exclusive, process-scoped daemon lock for local Linux filesystems."""
from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import stat
from collections.abc import Iterator


class DaemonAlreadyRunning(RuntimeError):
    """Another daemon owns the lock."""


@contextmanager
def daemon_lock(path: str | Path) -> Iterator[None]:
    """Hold a nonblocking exclusive lock for the daemon lifetime.

    All daemon instances must use the same lock path in a user-owned directory.
    Keep the lock file in place after release; deleting it can split ownership.
    """
    lock_path = Path(path)
    directory_fd = os.open(
        lock_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    try:
        directory = os.fstat(directory_fd)
        if directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
            raise PermissionError("Daemon lock directory must be user-owned and private")
        fd = os.open(
            lock_path.name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600, dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise PermissionError("Daemon lock must be a user-owned, private regular file with one link")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise DaemonAlreadyRunning("Another daemon owns the lock") from exc
            raise
        yield
    finally:
        os.close(fd)
