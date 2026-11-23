"""Local human approver authority backed by a separate private token file.

The approver token is distinct from the operator bearer token and is meant to
be typed or pasted by a person at decision time, never stored by clients. The
file is re-read on every authentication so deleting or rotating it revokes
access immediately.

Limitation: processes running as the same OS user can read the file, so this
is a deliberate speed bump, not isolation from same-user agents. Stronger
proof of presence (OS confirmation, hardware key) can replace this authority
without changing ApprovalService.
"""
from __future__ import annotations

from contextlib import contextmanager
import hmac
import os
import re
import secrets
import stat
from typing import Iterator

from synapse.application.approval_service import Actor, ApprovalAccessDenied

TOKEN_FILE = "approver.token"
HUMAN_SUBJECT = "local-approver"
_TOKEN = re.compile(rb"[A-Za-z0-9_-]{43}")
_ACTIONS = frozenset({"approval.decide", "approval.revoke"})


def _check(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        raise PermissionError("Approver token must be a private, user-owned regular file with one link")


def provision_approver_token(dir_fd: int, *, operator_token: str | None = None, rotate: bool = False) -> str:
    """Create (or rotate) the approver token atomically and return it once.

    Written to a temporary file, fsynced, then linked/renamed into place so a
    crash never leaves an empty or partial token. Refuses to replace an
    existing token unless rotate is True.
    """
    token = secrets.token_urlsafe(32)
    if operator_token is not None and hmac.compare_digest(token.encode(), operator_token.encode()):
        raise RuntimeError("Approver token must differ from the operator token")
    temp = f".{TOKEN_FILE}.{secrets.token_hex(8)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        try:
            os.write(fd, token.encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        if rotate:
            os.rename(temp, TOKEN_FILE, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            os.link(temp, TOKEN_FILE, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
            os.unlink(temp, dir_fd=dir_fd)
        os.fsync(dir_fd)
    except BaseException:
        try:
            os.unlink(temp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        raise
    return token


class LocalApproverAuthority:
    """ApprovalAuthority whose only human is whoever holds the approver token."""

    def __init__(self, dir_fd: int):
        self._dir_fd = os.dup(dir_fd)

    def close(self) -> None:
        os.close(self._dir_fd)

    def _current(self) -> bytes | None:
        try:
            fd = os.open(TOKEN_FILE, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._dir_fd)
        except FileNotFoundError:
            return None
        except OSError:
            # A symlink, permission or similar fault fails closed as access denied, not a 500.
            raise ApprovalAccessDenied("Approval access denied") from None
        try:
            try:
                _check(fd)
            except PermissionError:
                raise ApprovalAccessDenied("Approval access denied") from None
            data = os.read(fd, 44)
        finally:
            os.close(fd)
        return data if _TOKEN.fullmatch(data) else None

    @contextmanager
    def authenticated(self, credential: object) -> Iterator[Actor]:
        expected = self._current()
        supplied = credential.encode("utf-8", "replace") if isinstance(credential, str) else b""
        if expected is None or not hmac.compare_digest(supplied, expected):
            raise ApprovalAccessDenied("Approval access denied")
        yield Actor(HUMAN_SUBJECT, "human")

    def allows(self, actor: Actor, action: str, resource: dict) -> bool:
        # Only humans decide; requesting stays with workflow/agent authorities.
        return actor.kind == "human" and actor.subject == HUMAN_SUBJECT and action in _ACTIONS
