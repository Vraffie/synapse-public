"""Remove a stale Unix socket left by an unclean exit.

Call only while holding exclusive daemon ownership. A path is reclaimed only
when it is a socket owned by this user and a connection attempt is refused.
Live listeners, regular files, symlinks, foreign owners and any ambiguous
probe result are left untouched and surface as EADDRINUSE.

All filesystem operations are performed relative to a directory file
descriptor that is opened once and verified (a real directory, owned by this
user or root, and not writable by others unless sticky). Swapping the parent
directory or any ancestor symlink after the descriptor is opened therefore
cannot redirect the lstat, the connect probe or the unlink to another file.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import socket
import stat

_PROC_FD = "/proc/self/fd"


def _refuse(message: str, path: Path) -> OSError:
    return OSError(errno.EADDRINUSE, message, str(path))


def _open_verified_dir(parent: Path, path: Path) -> int | None:
    """Open the parent directory and verify it. Returns None if it is missing."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    try:
        dir_fd = os.open(parent, flags)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise _refuse("Socket directory could not be opened", path) from exc
    try:
        info = os.fstat(dir_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (os.geteuid(), 0):
            raise _refuse("Socket directory is not trusted", path)
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not info.st_mode & stat.S_ISVTX:
            raise _refuse("Socket directory is writable by others", path)
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


def _probe_target(dir_fd: int, name: str, path: Path) -> str:
    """Path that connect() resolves relative to dir_fd (Linux /proc), else the path."""
    candidate = f"{_PROC_FD}/{dir_fd}/{name}"
    if os.path.isdir(f"{_PROC_FD}/{dir_fd}") and len(candidate.encode()) < 100:
        return candidate
    return str(path)


def reclaim_stale_socket(path: Path) -> bool:
    path = Path(path)
    name = path.name
    if name in ("", ".", ".."):
        raise _refuse("Invalid socket path", path)
    dir_fd = _open_verified_dir(path.parent, path)
    if dir_fd is None:
        return False
    try:
        try:
            info = os.lstat(name, dir_fd=dir_fd)
        except FileNotFoundError:
            return False
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise _refuse("Existing path is not a user-owned socket", path)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1)
        try:
            probe.connect(_probe_target(dir_fd, name, path))
        except ConnectionRefusedError:
            pass
        except OSError as exc:
            raise _refuse("Existing socket could not be probed", path) from exc
        else:
            raise _refuse("Another listener owns this socket", path)
        finally:
            probe.close()
        try:
            current = os.lstat(name, dir_fd=dir_fd)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISSOCK(
                current.st_mode
            ):
                raise _refuse("Socket path changed during reclaim", path)
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            return False
        return True
    finally:
        os.close(dir_fd)
