"""Passphrase-verifier approver authority (opt-in alternative to the approver token file).

The daemon stores only a verifier: a salted scrypt hash of the passphrase in
``approver.verifier``. The passphrase is typed by a person per decision and is
never written to disk, taken from the environment or from argv.

Honest limits: a process running as the same OS user can read the verifier and
brute-force it offline. The slow KDF raises that cost; it is NOT a boundary
against same-user code. Online guessing through the daemon is rate limited with
a lockout. See docs/operations/approver-passphrase.md.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
import hmac
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator
import unicodedata

from synapse.application.approval_service import Actor, ApprovalAccessDenied
from synapse.infrastructure.local_approver import HUMAN_SUBJECT, LocalApproverAuthority, _ACTIONS

VERIFIER_FILE = "approver.verifier"
CONFIG_FILE = "approver.json"
MIN_LENGTH = 16  # also the ApprovalService threshold above which a credential may not appear in a reason
MAX_LENGTH = 256

# scrypt: N=2**18 (256 MiB with r=8), r=8, p=1, 32-byte key, 32-byte random per-install salt (~1 s per guess).
KDF_N, KDF_R, KDF_P, KDF_DKLEN, SALT_BYTES = 2 ** 18, 8, 1, 32, 32
MAX_ATTEMPTS = 5          # consecutive failures before lockout
LOCKOUT_SECONDS = 900     # fixed lockout: deliberately NOT escalating, so a same-user process that burns
                          # attempts can delay the human by at most 15 minutes per 5 failures, never lock them out
UNLOCK_FILE = "approver.unlock"  # marker written by `agentctl approver-unlock`; resets the counter only
PREAUTH_TTL = 30.0
MIN_DISTINCT = 6

log = logging.getLogger("synapse.approver")


def normalise(passphrase: str) -> bytes:
    return unicodedata.normalize("NFKC", passphrase).encode("utf-8")


def passphrase_shape_problem(passphrase: object) -> str | None:
    """Type and length (measured AFTER NFKC normalisation, so 'e' + 8 composed chars cannot pass)."""
    if not isinstance(passphrase, str):
        return "passphrase must be text"
    if not MIN_LENGTH <= len(unicodedata.normalize("NFKC", passphrase)) <= MAX_LENGTH:
        return f"passphrase must be {MIN_LENGTH} to {MAX_LENGTH} characters after Unicode normalisation"
    return None


def passphrase_problem(passphrase: object) -> str | None:
    """Full policy for choosing a passphrase (provisioning); verification only needs the shape check."""
    problem = passphrase_shape_problem(passphrase)
    if problem:
        return problem
    if len(set(unicodedata.normalize("NFKC", passphrase))) < MIN_DISTINCT:
        return f"passphrase is too weak: use at least {MIN_DISTINCT} different characters (not a repeated character)"
    return None


def _derive(secret: bytes, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(secret, salt=salt, n=n, r=r, p=p, dklen=KDF_DKLEN, maxmem=256 * n * r + (1 << 20))


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _check(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        raise PermissionError("approver file must be a private, user-owned regular file with one link")


def _write_atomic(dir_fd: int, name: str, data: bytes, *, replace: bool) -> None:
    temp = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        if replace:
            os.rename(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            os.link(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
            os.unlink(temp, dir_fd=dir_fd)
        os.fsync(dir_fd)
    except BaseException:
        try:
            os.unlink(temp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        raise


def _read_small(dir_fd: int, name: str, limit: int = 4096) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise ApprovalAccessDenied("Approval access denied") from None
    try:
        try:
            _check(fd)
        except PermissionError:
            raise ApprovalAccessDenied("Approval access denied") from None
        return os.read(fd, limit + 1)[:limit]
    finally:
        os.close(fd)


def provision_passphrase_verifier(dir_fd: int, passphrase: str, *, operator_token: str | None = None,
                                  rotate: bool = False, n: int = KDF_N, r: int = KDF_R, p: int = KDF_P) -> None:
    """Write the verifier and select passphrase mode; the passphrase itself is never stored.

    Refuses to replace an existing verifier unless rotate is True. Any plain
    approver.token is removed afterwards so the old secret stops working.
    ``n``/``r``/``p`` exist for tests; production callers use the defaults.
    """
    problem = passphrase_problem(passphrase)
    if problem:
        raise ValueError(problem)
    secret = normalise(passphrase)
    if operator_token is not None and hmac.compare_digest(secret, operator_token.encode()):
        raise RuntimeError("Approver passphrase must differ from the operator token")
    salt = secrets.token_bytes(SALT_BYTES)
    record = {"version": 1, "kdf": "scrypt", "n": n, "r": r, "p": p, "salt": _b64(salt),
              "hash": _b64(_derive(secret, salt, n, r, p))}
    _write_atomic(dir_fd, VERIFIER_FILE, json.dumps(record, sort_keys=True).encode("ascii"), replace=rotate)
    _write_atomic(dir_fd, CONFIG_FILE, b'{"authority":"passphrase"}', replace=True)
    try:
        os.unlink("approver.token", dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    os.fsync(dir_fd)


def _load_verifier(raw: bytes | None):
    """Return (n, r, p, salt, hash) or None if absent or malformed (fail closed)."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        n, r, p = data["n"], data["r"], data["p"]
        salt, digest = base64.b64decode(data["salt"], validate=True), base64.b64decode(data["hash"], validate=True)
        if (data["version"] != 1 or data["kdf"] != "scrypt" or any(type(x) is not int for x in (n, r, p))
                or n < 2 ** 10 or n > 2 ** 20 or n & (n - 1) or not 1 <= r <= 16 or not 1 <= p <= 4
                or n * r > 2 ** 21 or not 16 <= len(salt) <= 64 or len(digest) != KDF_DKLEN):
            return None
        return n, r, p, salt, digest
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None


def read_mode_hint(state_dir: Path) -> str:
    """UX-only: which prompt a client should show. Not a security decision (the daemon decides)."""
    try:
        data = json.loads((Path(state_dir) / CONFIG_FILE).read_bytes()[:256])
        return "passphrase" if data.get("authority") == "passphrase" else "token"
    except (OSError, ValueError, AttributeError):
        return "token"


class PassphraseApproverAuthority:
    """ApprovalAuthority whose only human is whoever knows the passphrase behind the verifier.

    Lockout policy (see docs/operations/approver-identity.md): MAX_ATTEMPTS wrong passphrases that
    actually reached the KDF lock the approver for a fixed LOCKOUT_SECONDS. Malformed or missing
    credentials are not counted. The state is in memory; a daemon restart or the operator's
    `agentctl approver-unlock` (a marker file, no passphrase, approves nothing) clears it.
    """

    def __init__(self, dir_fd: int, *, clock=time.monotonic, max_attempts: int = MAX_ATTEMPTS,
                 lockout_seconds: float = LOCKOUT_SECONDS, wall=time.time):
        self._dir_fd = os.dup(dir_fd)
        self._clock = clock
        self._wall = wall
        self._max_attempts = max_attempts
        self._lockout = lockout_seconds
        self._failures = 0
        self._locked_until = 0.0
        self._lock = threading.Lock()  # serialises verification so the counter stays correct across threads
        self._pre: dict[bytes, tuple[float, str | None, str | None]] = {}
        self._events: list[tuple[str, dict]] = []
        self._drop_unlock_marker()  # a marker left from before this start must not pre-authorise a reset

    def _drop_unlock_marker(self) -> None:
        try:
            os.unlink(UNLOCK_FILE, dir_fd=self._dir_fd)
        except OSError:
            pass

    def close(self) -> None:
        os.close(self._dir_fd)

    def locked_until_text(self) -> str | None:
        """Wall-clock 'HH:MM' (UTC) the lockout ends, or None when not locked."""
        remaining = self._locked_until - self._clock()
        if remaining <= 0:
            return None
        return (datetime.fromtimestamp(self._wall(), timezone.utc) + timedelta(seconds=remaining)).strftime("%H:%M")

    def pop_events(self) -> list[tuple[str, dict]]:
        """Audit events (lockout started / operator unlock) for the application layer to persist."""
        with self._lock:
            events, self._events = self._events, []
        return events

    def _consume_unlock_marker(self) -> None:
        try:
            fd = os.open(UNLOCK_FILE, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._dir_fd)
        except OSError:
            return
        try:
            _check(fd)  # private, user-owned regular file only
        except PermissionError:
            return
        finally:
            os.close(fd)
        try:
            os.unlink(UNLOCK_FILE, dir_fd=self._dir_fd)
        except OSError:
            return
        self._failures, self._locked_until = 0, 0.0
        self._events.append(("approver.unlock", {}))
        log.warning("approver lockout reset by the operator unlock marker")

    def _attempt(self, credential: object) -> tuple[str | None, str | None]:
        """Verify once. Returns (None, None) on success, else (code, locked_until HH:MM or None)."""
        with self._lock:
            if self._clock() < self._locked_until:
                self._consume_unlock_marker()
            now = self._clock()
            if now < self._locked_until:
                log.warning("approver passphrase attempt refused: locked out for %d more seconds",
                            int(self._locked_until - now))
                return "rate_limited", self.locked_until_text()
            if (not isinstance(credential, str) or passphrase_shape_problem(credential) is not None):
                return "unauthenticated", None  # never reached verification: not counted
            verifier = _load_verifier(_read_small(self._dir_fd, VERIFIER_FILE))
            if verifier is None:
                return "unauthenticated", None  # nothing to verify against: not counted
            n, r, p, salt, digest = verifier
            if hmac.compare_digest(_derive(normalise(credential), salt, n, r, p), digest):
                self._failures = 0
                return None, None
            self._failures += 1
            log.warning("approver passphrase attempt failed (%d of %d before lockout)",
                        self._failures, self._max_attempts)
            if self._failures >= self._max_attempts:
                self._failures = 0
                self._locked_until = self._clock() + self._lockout
                self._drop_unlock_marker()  # only a marker written during this lockout counts
                until = self.locked_until_text()
                self._events.append(("approver.lockout", {"seconds": int(self._lockout), "until_utc": until}))
                log.warning("approver passphrase locked out for %d seconds after repeated failures", int(self._lockout))
                return "unauthenticated", until
            return "unauthenticated", None

    def preauthenticate(self, credential: object) -> None:
        """Blocking verification meant for a worker thread (asyncio.to_thread). The outcome is kept for
        this exact credential for a few seconds and consumed by the next ``authenticated`` call, so the
        KDF never runs twice for one request and never on the event loop."""
        result = self._attempt(credential)
        key = hashlib.sha256(str(credential).encode("utf-8", "surrogatepass")).digest()
        with self._lock:
            now = self._clock()
            self._pre = {k: v for k, v in self._pre.items() if v[0] > now}
            self._pre[key] = (now + PREAUTH_TTL, *result)

    @contextmanager
    def authenticated(self, credential: object) -> Iterator[Actor]:
        key = hashlib.sha256(str(credential).encode("utf-8", "surrogatepass")).digest()
        with self._lock:
            cached = self._pre.pop(key, None)
        if cached is not None and cached[0] > self._clock():
            code, until = cached[1], cached[2]
        else:
            code, until = self._attempt(credential)
        if code is not None:
            denied = ApprovalAccessDenied("Approval access denied", code=code)
            denied.locked_until = until
            raise denied
        yield Actor(HUMAN_SUBJECT, "human")

    def allows(self, actor: Actor, action: str, resource: dict) -> bool:
        return actor.kind == "human" and actor.subject == HUMAN_SUBJECT and action in _ACTIONS


def build_approver_authority(dir_fd: int):
    """Select the authority from ``approver.json`` (default and on any fault: token mode)."""
    try:
        raw = _read_small(dir_fd, CONFIG_FILE, 256)
        mode = json.loads(raw).get("authority") if raw is not None else None
    except (ApprovalAccessDenied, ValueError, AttributeError):
        mode = None
    if mode == "passphrase":
        return PassphraseApproverAuthority(dir_fd)
    return LocalApproverAuthority(dir_fd)
