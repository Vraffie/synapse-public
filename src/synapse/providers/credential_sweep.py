"""Sweep scratch HOMEs left behind by a SIGKILLed daemon (Claude CLI and Codex adapters).

Adapters call `write_marker(root)` right after creating a per-run scratch root. At daemon startup,
before any run, `sweep_startup(state_dir)` removes scratch roots whose owner is gone.

Safety rules: only the adapters' scratch bases are scanned (never followed if symlinks); only
direct children that are real directories owned by the current user, named like a uuid4 hex and
carrying a valid marker are candidates; an entry is skipped when its owner pid is alive and is
this daemon (same boot id) or the same process incarnation (matching /proc start time). Files are
overwritten with zeros (credential-named files up to 1 MiB only) then unlinked (best effort); nothing is ever followed through a symlink.
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterable
from uuid import uuid4

log = logging.getLogger(__name__)

MARKER_NAME = ".synapse-scratch-owner"
MARKER_MAGIC = "synapse-scratch-v1"
BOOT_ID = uuid4().hex  # one per daemon process
_ENTRY = re.compile(r"[0-9a-f]{32}")
_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


def _start_time(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return fh.read().rsplit(b")", 1)[1].split()[19].decode()
    except (OSError, IndexError):
        return None


def write_marker(root: str | os.PathLike, *, pid: int | None = None, boot_id: str | None = None) -> None:
    pid = os.getpid() if pid is None else pid
    data = json.dumps({"magic": MARKER_MAGIC, "pid": pid, "boot_id": boot_id or BOOT_ID,
                       "start": _start_time(pid)}).encode()
    fd = os.open(os.path.join(root, MARKER_NAME), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_marker(dir_fd: int) -> dict | None:
    try:
        fd = os.open(MARKER_NAME, _FLAGS | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size > 4096:
            return None
        meta = json.loads(os.read(fd, 4096))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    if not isinstance(meta, dict) or meta.get("magic") != MARKER_MAGIC or type(meta.get("pid")) is not int \
            or not isinstance(meta.get("boot_id"), str):
        return None
    return meta


MAX_SWEEP_DEPTH = 64  # open fds held at once; a deeper chain is left in place (logged), never recursed into
MAX_SHRED_BYTES = 1 << 20  # zero-fill only credential-named files up to this size; others are just unlinked
_CREDENTIAL_NAME = re.compile(r"cred|auth|token|secret|key|passw|session", re.IGNORECASE)


def _shred_file(name: str, dir_fd: int) -> None:
    fd = None
    if _CREDENTIAL_NAME.search(name):
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        except OSError:
            fd = None
    if fd is not None:
        try:
            info = os.fstat(fd)
            if (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                    and info.st_size <= MAX_SHRED_BYTES):
                left = info.st_size
                while left > 0:
                    left -= os.write(fd, b"\0" * min(left, 65536))
                os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
    os.unlink(name, dir_fd=dir_fd)


def _remove_children(dir_fd: int) -> None:
    """Remove everything under ``dir_fd`` iteratively (bounded fds, no recursion).

    A per-entry failure is logged and skipped. Raises OSError only if the tree is deeper than
    ``MAX_SWEEP_DEPTH`` (the caller then leaves the scratch dir in place).
    """
    # frame: [fd, entries, index, parent_fd, name_in_parent]; frame 0's fd belongs to the caller
    def frame(fd: int, parent: int | None, name: str | None) -> list:
        with os.scandir(fd) as it:
            return [fd, list(it), 0, parent, name]

    stack = [frame(dir_fd, None, None)]
    too_deep = False
    try:
        while stack:
            top = stack[-1]
            fd, entries = top[0], top[1]
            if top[2] >= len(entries):
                stack.pop()
                if top[3] is not None:
                    os.close(fd)
                    try:
                        os.rmdir(top[4], dir_fd=top[3])
                    except OSError as exc:
                        log.warning("credential sweep: could not remove %s: %s", top[4], exc)
                continue
            entry = entries[top[2]]
            top[2] += 1
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    _shred_file(entry.name, fd)
                    continue
                if len(stack) > MAX_SWEEP_DEPTH:
                    too_deep = True
                    continue
                sub = os.open(entry.name, _FLAGS | os.O_DIRECTORY, dir_fd=fd)
                try:
                    if os.fstat(sub).st_dev != os.fstat(fd).st_dev:
                        os.close(sub)
                        continue  # never cross a mount point
                    os.fchmod(sub, 0o700)
                    stack.append(frame(sub, fd, entry.name))
                except BaseException:
                    if not stack or stack[-1][0] != sub:
                        os.close(sub)
                    raise
            except Exception as exc:  # one bad entry must not abort the sweep
                log.warning("credential sweep: could not clean %s: %s", entry.name, exc)
    finally:
        for fr in stack[1:]:
            try:
                os.close(fr[0])
            except OSError:
                pass
    if too_deep:
        raise OSError("scratch tree deeper than %d levels; left in place" % MAX_SWEEP_DEPTH)


def _owner_is_live(meta: dict, boot_id: str, pid_alive: Callable[[int], bool]) -> bool:
    pid = meta["pid"]
    if pid <= 0 or not pid_alive(pid):
        return False
    if meta["boot_id"] == boot_id:
        return True  # this daemon's own run
    recorded = meta.get("start")
    current = _start_time(pid)
    # Another live process incarnation (e.g. a second daemon sharing /tmp): leave it alone.
    return recorded is None or current is None or recorded == current


def sweep_scratch(roots: Iterable[str | os.PathLike], *, boot_id: str = BOOT_ID,
                  pid_alive: Callable[[int], bool] = _pid_alive) -> list[str]:
    """Remove stale scratch dirs directly under each root; return the removed paths."""
    removed: list[str] = []
    for root in roots:
        try:
            rfd = os.open(root, _FLAGS | os.O_DIRECTORY)
        except OSError:
            continue  # missing, symlink or not a directory
        try:
            if os.fstat(rfd).st_uid != os.geteuid():
                continue
            with os.scandir(rfd) as it:
                entries = list(it)
            for entry in entries:
                if not _ENTRY.fullmatch(entry.name) or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    efd = os.open(entry.name, _FLAGS | os.O_DIRECTORY, dir_fd=rfd)
                except OSError:
                    continue
                try:
                    info = os.fstat(efd)
                    meta = _read_marker(efd) if info.st_uid == os.geteuid() else None
                    if meta is None or _owner_is_live(meta, boot_id, pid_alive):
                        continue
                    os.fchmod(efd, 0o700)
                    _remove_children(efd)
                except Exception as exc:
                    log.warning("credential sweep: could not clean %s: %s", entry.name, exc)
                    continue
                finally:
                    os.close(efd)
                try:
                    os.rmdir(entry.name, dir_fd=rfd)
                    removed.append(os.path.join(os.fspath(root), entry.name))
                except OSError as exc:
                    log.warning("credential sweep: could not remove %s: %s", entry.name, exc)
        finally:
            os.close(rfd)
    return removed


def scratch_roots(state_dir: str | os.PathLike) -> list[str]:
    """The adapters' scratch bases: Claude under the temp dir, Codex under the state dir."""
    return [os.path.join(os.path.realpath(tempfile.gettempdir()), f"synapse-agent-{os.getuid()}"),
            os.path.join(os.fspath(state_dir), "codex-run")]


def sweep_startup(state_dir: str | os.PathLike) -> list[str]:
    """Best effort: never raises (a failed sweep must not stop the daemon from starting)."""
    try:
        return sweep_scratch(scratch_roots(state_dir))
    except Exception:
        log.exception("credential sweep failed; continuing startup")
        return []
