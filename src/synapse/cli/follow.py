"""``agentctl events --follow``: tail the event log with a cursor that survives restarts.

Events are durable in the daemon, so recovery is just "ask again from the last sequence I handled":
the cursor file holds that sequence and the id of the event at it, which binds it to one database. Delivery to stdout is at-least-once: a page is printed (one JSON
object per line) and only then is the cursor saved, so a crash in between replays that page and never
skips one. Transient failures (daemon down or restarting, HTTP 5xx) are retried with backoff; anything
else (bad token, 4xx, a response that goes backwards) stops with an error instead of guessing.
"""
from __future__ import annotations

from collections.abc import Callable
import http.client
import json
import os
import re
import signal
from pathlib import Path
import stat
import sys
import tempfile
import time
from typing import IO, NamedTuple
from urllib.parse import urlencode

CURSOR_NAME = "events.cursor"
MAX_SEQUENCE = 9223372036854775807
MAX_BACKOFF_SECONDS = 10.0
DEFAULT_MAX_ERRORS = 60
MAX_CURSOR_BYTES = 256
MIN_INTERVAL, MAX_INTERVAL = 0.05, 60.0
_EVENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class FollowError(RuntimeError):
    """Following cannot continue safely (the message never includes a response body)."""


class Cursor(NamedTuple):
    """The last handled event: its sequence and its id (None only for a legacy file written before ids were saved)."""
    sequence: int
    event_id: str | None


def read_cursor(path: Path) -> Cursor | None:
    """The saved cursor, or None when there is none. A present but unsafe or corrupt file raises.

    Format: ``SEQUENCE EVENT_ID`` on one line. A bare ``SEQUENCE`` (previous version) is read as legacy.
    """
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
        data = stream.read(MAX_CURSOR_BYTES + 1)
    fields = data.decode("ascii", "replace").split()
    if (len(data) > MAX_CURSOR_BYTES or not 1 <= len(fields) <= 2 or not fields[0].isascii() or not fields[0].isdigit()
            or int(fields[0]) > MAX_SEQUENCE or (len(fields) == 2 and not _EVENT_ID.fullmatch(fields[1]))):
        raise FollowError(f"cursor file {path} is corrupt; delete it or pass --after")
    return Cursor(int(fields[0]), fields[1] if len(fields) == 2 else None)


def write_cursor(path: Path, sequence: int, event_id: str) -> None:
    """Atomic replace, mode 0600, fsynced file and directory."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".events-cursor-")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(f"{sequence} {event_id}\n")
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


class _Stop(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


def _quiet_stdout(out: IO[str]) -> None:
    """After a BrokenPipe, point fd 1 at /dev/null so the interpreter's flush at exit does not print a second error."""
    if out is sys.stdout:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass


def follow_events(fetch: Callable[[str], object], *, cursor_file: Path, after: int | None, limit: int,
                  interval: float, stop_when_idle: bool, out: IO[str] = sys.stdout, err: IO[str] = sys.stderr,
                  sleep: Callable[[float], None] = time.sleep, max_errors: int = DEFAULT_MAX_ERRORS,
                  cursor_file_explicit: bool = True) -> int:
    """Print events after the cursor until interrupted (or idle, with ``stop_when_idle``). Returns an exit code.

    ``cursor_file_explicit``: the caller named the cursor file, so ``after`` may rewind it.
    """
    if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        print(f"agentctl: --interval must be between {MIN_INTERVAL:g} and {MAX_INTERVAL:g} seconds", file=err)
        return 2
    try:
        saved = read_cursor(cursor_file)
    except FollowError as exc:
        print(f"agentctl: {exc}", file=err)
        return 1
    if after is not None and saved is not None and after < saved.sequence and not cursor_file_explicit:
        print(f"agentctl: --after {after} is below the saved cursor {saved.sequence} in {cursor_file}; "
              "pass --cursor-file to replay into a separate file (the saved cursor is never moved backwards)", file=err)
        return 2
    failures, delay = 0, 0.5

    def get(after_sequence: int, page_limit: int) -> list:
        """One page, retrying transient failures; raises _Stop with the exit code on anything else."""
        nonlocal failures, delay
        while True:
            try:
                page = fetch("/api/v1/events?" + urlencode({"after": after_sequence, "limit": page_limit}))
            except Exception as exc:
                if not _transient(exc):
                    print(f"agentctl: {type(exc).__name__}: {exc}", file=err)
                    raise _Stop(1) from None
                failures += 1
                if failures > max_errors:
                    print(f"agentctl: giving up after {max_errors} failed attempts: {type(exc).__name__}", file=err)
                    raise _Stop(1) from None
                print(f"agentctl: daemon unavailable ({type(exc).__name__}); retrying", file=err)
                sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
                continue
            failures, delay = 0, 0.5
            events = page.get("events") if isinstance(page, dict) else None
            if not isinstance(events, list):
                print("agentctl: unexpected events response", file=err)
                raise _Stop(1)
            return events

    def save(sequence: int, event_id: str) -> bool:
        try:
            write_cursor(cursor_file, sequence, event_id)
        except OSError as exc:
            print(f"agentctl: cannot save the cursor to {cursor_file} ({exc.strerror or type(exc).__name__}); "
                  f"events up to sequence {sequence} were already printed", file=err)
            return False
        return True

    previous_term = None
    try:
        previous_term = signal.signal(signal.SIGTERM, signal.default_int_handler)  # like Ctrl-C: the cursor is saved
    except ValueError:  # not the main thread
        pass
    try:
        if after is not None:
            cursor = after
        elif saved is None:
            cursor = 0
        else:
            cursor = saved.sequence
            # The log must still be the one this cursor came from: the event AT the cursor has to be there.
            head = get(cursor - 1, 1) if cursor > 0 else []
            at = head[0] if head and isinstance(head[0], dict) else None
            if cursor > 0 and (at is None or at.get("sequence") != cursor):
                print(f"agentctl: the saved cursor {cursor} in {cursor_file} is ahead of the event log (the database "
                      "was recreated or restored?); delete the cursor file or pass --after to start over", file=err)
                return 1
            if cursor > 0 and saved.event_id is not None and at.get("id") != saved.event_id:
                print(f"agentctl: the event at sequence {cursor} is not the one the cursor in {cursor_file} was saved "
                      "for (the database was recreated or restored?); delete the cursor file or pass --after to "
                      "start over", file=err)
                return 1
            if cursor > 0 and saved.event_id is None:
                print(f"agentctl: {cursor_file} is in the legacy format (no event id); accepting it once and "
                      "rewriting it with the id of the event at the cursor", file=err)
                if not isinstance(at.get("id"), str) or not _EVENT_ID.fullmatch(at["id"]) or not save(cursor, at["id"]):
                    return 1
        while True:
            events = get(cursor, limit)
            last, last_id = cursor, None
            lines = []
            for event in events:
                sequence = event.get("sequence") if isinstance(event, dict) else None
                event_id = event.get("id") if isinstance(event, dict) else None
                if type(sequence) is not int or sequence <= last:
                    print("agentctl: the daemon returned events out of order; stopping without moving the cursor", file=err)
                    return 1
                if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
                    print("agentctl: unexpected events response (event without a usable id)", file=err)
                    return 1
                last, last_id = sequence, event_id
                lines.append(json.dumps(event, ensure_ascii=False))
            if lines:
                try:
                    out.write("\n".join(lines) + "\n")
                    out.flush()
                except BrokenPipeError:
                    _quiet_stdout(out)
                    return 1  # the reader went away: do not claim the page was delivered
                if not save(last, last_id):
                    return 1
                cursor = last
                continue  # the page may not have been the last one
            if stop_when_idle:
                return 0
            sleep(interval)
    except _Stop as stop:
        return stop.code
    except KeyboardInterrupt:
        return 0
    finally:
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
