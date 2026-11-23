"""Durable desktop event cursor, bound to one daemon's event log.

The file lives in the daemon's private state directory and holds ``SEQUENCE EVENT_ID``. Binding the cursor to the
event id means a recreated or restored database is detected instead of silently skipping events. No Qt here.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import NamedTuple

CURSOR_NAME = "desktop-events.cursor"
MAX_CURSOR_BYTES = 256
MAX_SEQUENCE = 2**62
EVENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class Cursor(NamedTuple):
    sequence: int
    event_id: str


class CursorError(Exception):
    """The saved cursor cannot be trusted; the message is safe to show (it never contains file contents)."""


def valid_event_id(value) -> bool:
    return isinstance(value, str) and EVENT_ID.fullmatch(value) is not None


def cursor_path(state_dir: Path) -> Path:
    return Path(state_dir) / CURSOR_NAME


def read_cursor(path: Path) -> Cursor | None:
    """Return the saved cursor, None when no file exists, or raise CursorError (unsafe or corrupt)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CursorError(f"cursor file cannot be opened ({type(exc).__name__})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CursorError("cursor file is not a private, user-owned regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_CURSOR_BYTES + 1)
    except OSError as exc:
        raise CursorError(f"cursor file cannot be read ({type(exc).__name__})") from None
    finally:
        os.close(fd)
    try:
        parts = data.decode("ascii").split()
        if len(data) > MAX_CURSOR_BYTES or len(parts) != 2 or not parts[0].isascii() or not parts[0].isdigit():
            raise ValueError
        sequence = int(parts[0])
        if not 1 <= sequence <= MAX_SEQUENCE or not valid_event_id(parts[1]):
            raise ValueError
    except ValueError:  # includes UnicodeDecodeError
        raise CursorError("cursor file is corrupt") from None
    return Cursor(sequence, parts[1])


def write_cursor(path: Path, cursor: Cursor) -> None:
    """Atomic, user-private write: temp file in the same directory, fsync, chmod 0600, rename, fsync the directory."""
    if not valid_event_id(cursor.event_id) or not 1 <= cursor.sequence <= MAX_SEQUENCE:
        raise ValueError("cursor is not persistable")
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".cursor-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(f"{cursor.sequence} {cursor.event_id}\n".encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
