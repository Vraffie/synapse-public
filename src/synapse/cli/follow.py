"""``agentctl events --follow``: tail the event log with a cursor that survives restarts.

Events are durable in the daemon, so recovery is just "ask again from the last sequence I handled":
the cursor file holds that one integer. Delivery to stdout is at-least-once: a page is printed (one JSON
object per line) and only then is the cursor saved, so a crash in between replays that page and never
skips one. Transient failures (daemon down or restarting, HTTP 5xx) are retried with backoff; anything
else (bad token, 4xx, a response that goes backwards) stops with an error instead of guessing.
"""
from __future__ import annotations

from collections.abc import Callable
import http.client
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from typing import IO
from urllib.parse import urlencode

CURSOR_NAME = "events.cursor"
MAX_SEQUENCE = 9223372036854775807
MAX_BACKOFF_SECONDS = 10.0
DEFAULT_MAX_ERRORS = 60


class FollowError(RuntimeError):
    """Following cannot continue safely (the message never includes a response body)."""


def read_cursor(path: Path) -> int | None:
    """The saved cursor, or None when there is none. A present but unsafe or corrupt file raises."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FollowError(f"cursor file {path} cannot be opened safely ({exc.strerror}); fix or delete it") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise FollowError(f"cursor file {path} must be a private, user-owned regular file; fix or delete it")
        data = stream.read(33)
    text = data.decode("ascii", "replace").strip()
    if len(data) > 32 or not text.isascii() or not text.isdigit() or int(text) > MAX_SEQUENCE:
        raise FollowError(f"cursor file {path} is corrupt; delete it or pass --after")
    return int(text)


def write_cursor(path: Path, sequence: int) -> None:
    """Atomic replace, mode 0600, fsynced file and directory."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".events-cursor-")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(f"{sequence}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _transient(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    if isinstance(status, int):  # DaemonHTTPError: a 5xx may clear, a 4xx (token, request) will not
        return status >= 500
    return isinstance(exc, (ConnectionError, FileNotFoundError, TimeoutError, http.client.HTTPException)) or (
        isinstance(exc, OSError) and not isinstance(exc, PermissionError))


def follow_events(fetch: Callable[[str], object], *, cursor_file: Path, after: int | None, limit: int,
                  interval: float, stop_when_idle: bool, out: IO[str] = sys.stdout, err: IO[str] = sys.stderr,
                  sleep: Callable[[float], None] = time.sleep, max_errors: int = DEFAULT_MAX_ERRORS) -> int:
    """Print events after the cursor until interrupted (or idle, with ``stop_when_idle``). Returns an exit code."""
    try:
        saved = read_cursor(cursor_file)
    except FollowError as exc:
        print(f"agentctl: {exc}", file=err)
        return 1
    cursor = after if after is not None else (saved or 0)
    failures, delay = 0, 0.5
    try:
        while True:
            try:
                page = fetch("/api/v1/events?" + urlencode({"after": cursor, "limit": limit}))
            except Exception as exc:
                if not _transient(exc):
                    print(f"agentctl: {type(exc).__name__}: {exc}", file=err)
                    return 1
                failures += 1
                if failures > max_errors:
                    print(f"agentctl: giving up after {max_errors} failed attempts: {type(exc).__name__}", file=err)
                    return 1
                print(f"agentctl: daemon unavailable ({type(exc).__name__}); retrying", file=err)
                sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
                continue
            failures, delay = 0, 0.5
            events = page.get("events") if isinstance(page, dict) else None
            if not isinstance(events, list):
                print("agentctl: unexpected events response", file=err)
                return 1
            last = cursor
            lines = []
            for event in events:
                sequence = event.get("sequence") if isinstance(event, dict) else None
                if type(sequence) is not int or sequence <= last:
                    print("agentctl: the daemon returned events out of order; stopping without moving the cursor", file=err)
                    return 1
                last = sequence
                lines.append(json.dumps(event, ensure_ascii=False))
            if lines:
                try:
                    out.write("\n".join(lines) + "\n")
                    out.flush()
                except BrokenPipeError:
                    return 1  # the reader went away: do not claim the page was delivered
                write_cursor(cursor_file, last)
                cursor = last
                continue  # the page may not have been the last one
            if stop_when_idle:
                return 0
            sleep(interval)
    except KeyboardInterrupt:
        return 0
