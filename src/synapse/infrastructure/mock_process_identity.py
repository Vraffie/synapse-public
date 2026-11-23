"""Linux identity checks and conservative signaling of an owned mock child.

The caller must be the sole reaper of the child. This is not a recovery fence
for restarted supervisors, real providers or detached descendants.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import signal
import subprocess
import time


class ProcessStatus(StrEnum):
    OWNED = "owned"
    UNKNOWN = "unknown"
    SIGNALLED_GROUP = "signalled_group"


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    start_ticks: int
    boot_id: str
    supervisor_pid: int
    supervisor_start_ticks: int


def _boot_id() -> str:
    value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if not value:
        raise ValueError("empty boot identifier")
    return value


def _stat(pid: int) -> tuple[str, int, int, int, int]:
    """Return state, parent, process group, session and start ticks."""
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    close = raw.rfind(")")
    if close < 0 or not raw.startswith(f"{pid} ("):
        raise ValueError("invalid process stat")
    fields = raw[close + 1:].split()
    if len(fields) < 20:
        raise ValueError("truncated process stat")
    return fields[0], int(fields[1]), int(fields[2]), int(fields[3]), int(fields[19])


def _observation(identity: ProcessIdentity, *, allow_zombie: bool = False) -> ProcessStatus:
    if not isinstance(identity, ProcessIdentity):
        return ProcessStatus.UNKNOWN
    if any(type(x) is not int or x <= 0 for x in (identity.pid, identity.start_ticks,
                                                   identity.supervisor_pid, identity.supervisor_start_ticks)):
        return ProcessStatus.UNKNOWN
    try:
        if identity.boot_id != _boot_id() or identity.supervisor_pid != os.getpid():
            return ProcessStatus.UNKNOWN
        if _stat(identity.supervisor_pid)[4] != identity.supervisor_start_ticks:
            return ProcessStatus.UNKNOWN
        state, parent, group, session, start = _stat(identity.pid)
        if state in ("Z", "X") and not allow_zombie:
            return ProcessStatus.UNKNOWN
        if parent != identity.supervisor_pid or group != identity.pid or session != identity.pid or start != identity.start_ticks:
            return ProcessStatus.UNKNOWN
    except (OSError, ValueError, IndexError):
        return ProcessStatus.UNKNOWN
    return ProcessStatus.OWNED


def capture_mock_child(process: subprocess.Popen) -> ProcessIdentity:
    """Capture a newly spawned session-leading child before any other reaper.

    The process must be created by this supervisor with start_new_session=True.
    Caller owns the Popen handle exclusively for its entire lifetime.
    """
    if not isinstance(process, subprocess.Popen) or process.returncode is not None:
        raise ValueError("an unreaped child process is required")
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise ValueError("SIGCHLD must have the default disposition")
    try:
        supervisor_pid = os.getpid()
        own_start = _stat(supervisor_pid)[4]
        state, parent, group, session, start = _stat(process.pid)
        if state in ("Z", "X") or parent != supervisor_pid or group != process.pid or session != process.pid:
            raise ValueError("child must lead its own process session")
        return ProcessIdentity(process.pid, start, _boot_id(), supervisor_pid, own_start)
    except OSError as exc:
        raise ValueError("child identity is unavailable") from exc


def inspect_mock_child(identity: ProcessIdentity) -> ProcessStatus:
    """Return UNKNOWN on restart, reuse, exit, permission error or ambiguity."""
    return _observation(identity)


def stop_mock_child_group(process: subprocess.Popen, identity: ProcessIdentity, *, grace_seconds: float = 1.0) -> ProcessStatus:
    """Stop the original child's group, without signalling an ambiguous PID.

    The sole-reaper guarantee prevents PID reuse from the final identity check
    until both signals have been sent. No process may poll/wait this Popen or
    call waitpid for it concurrently; SIGCHLD must remain default. The leader
    stays unreaped through escalation even if it exits during the grace period.
    The call blocks for the whole grace period by design (the unreaped leader
    cannot reveal group exit); call it from a thread, never an event-loop coroutine.
    """
    if isinstance(grace_seconds, bool) or not isinstance(grace_seconds, (int, float)) or not 0 <= grace_seconds <= 30:
        raise ValueError("grace_seconds must be between zero and 30")
    if (not isinstance(process, subprocess.Popen) or not isinstance(identity, ProcessIdentity)
            or process.pid != identity.pid or process.returncode is not None
            or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
            or _observation(identity) != ProcessStatus.OWNED):
        return ProcessStatus.UNKNOWN
    try:
        os.killpg(identity.pid, signal.SIGTERM)
        # Do not wait/poll the leader here: its unreaped PID fences reuse.
        deadline = time.monotonic() + grace_seconds
        while (remaining := deadline - time.monotonic()) > 0:
            time.sleep(min(remaining, 0.05))
        if _observation(identity, allow_zombie=True) != ProcessStatus.OWNED:
            return ProcessStatus.UNKNOWN
        try:
            os.killpg(identity.pid, signal.SIGKILL)
        except ProcessLookupError:
            # No process group remains, but retain conservative result until
            # the exclusively owned leader has been reaped below.
            pass
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ProcessStatus.UNKNOWN
    return ProcessStatus.SIGNALLED_GROUP
