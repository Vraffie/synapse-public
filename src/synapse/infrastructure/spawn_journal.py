"""Durable record of agent processes the daemon spawned, so a restart can PROVE or ENFORCE that none survive.

Today's recovery attests ``no_workers_survive=True`` without checking. A detached agent (sandbox off, or a
sandbox that failed to tie it to the daemon) can outlive a crashed daemon and keep editing a worktree while
recovery marks its attempt unknown. This journal closes that for every spawn the daemon makes:

* ``begin`` writes an intent file BEFORE the process exists (a crash right after leaves "starting, no pid":
  unverifiable, reported as such, never assumed dead);
* ``record`` adds pid, start ticks and boot id right after ``Popen`` (the child is its own session leader, so
  its pid is its process group id);
* ``finish`` removes the file once the owner has reaped the process;
* ``fence_all`` runs at daemon startup, under the ownership lock, before any recovery: for every leftover entry it
  proves the original process is gone (other boot, no such pid, pid reused, or only zombies) or kills its whole
  process group and checks nothing is left. Anything it cannot prove is reported ``unverified`` and the file is
  kept as ``*.unverified``, which counts as unverified at EVERY startup until the operator deletes it.

Limits (stated, not hidden): this is a PROCESS-GROUP fence. A descendant that left the group (``setsid`` /
``setpgid``, or a detached helper) is not found, so ``all_clear`` means "no member of the recorded group survives",
not "no descendant survives". Sandbox runs are additionally tied to the daemon by ``--die-with-parent`` and a PID
namespace; a cgroup or subreaper would be needed for a full guarantee (future work, ADR 023). Journal files are
attacker-writable when the agent runs unsandboxed as the same uid, so the fence refuses to signal the daemon's own
group or session, and an ambiguous group is reported instead of killed.

Linux only (``/proc``); local single machine. Files are 0600 in a 0700 directory, written atomically with fsync,
and contain only ids, pid, start ticks and boot id: never a prompt, path or credential.
"""
from __future__ import annotations

import json
import os
import signal
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from synapse.infrastructure.mock_process_identity import ProcessIdentity, _boot_id, _stat

_KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
_MAX_ENTRIES = 1000
_MAX_FILE_BYTES = 4096
_FENCE_BUDGET_SECONDS = 60.0  # total time the fence may spend waiting for killed groups


@dataclass(frozen=True, slots=True)
class FenceReport:
    verified_dead: int = 0   # original process provably gone (boot changed, pid absent, pid reused, zombies only)
    killed: int = 0          # was alive; its process group was killed and checked empty
    unverified: int = 0      # could not be proven dead: starting-without-pid, malformed entry, group would not die

    @property
    def all_clear(self) -> bool:
        return self.unverified == 0

    @property
    def total(self) -> int:
        return self.verified_dead + self.killed + self.unverified


class SpawnJournal:
    def __init__(self, directory: Path | str):
        self._dir = Path(directory)
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._dir, 0o700)

    # ------------------------------------------------------------------ owner side
    def _path(self, key: str) -> Path:
        if not isinstance(key, str) or not key or len(key) > 128 or not set(key) <= _KEY_CHARS:
            raise ValueError("invalid journal key")
        return self._dir / (key + ".json")

    def _write(self, key: str, body: dict) -> None:
        final = self._path(key)
        tmp = final.with_name(final.name + ".tmp")
        tmp.unlink(missing_ok=True)  # removes a planted symlink itself, never its target
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(body, fh)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, final)
        dfd = os.open(self._dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def begin(self, key: str) -> None:
        """Intent: a process for ``key`` may exist from now on."""
        self._write(key, {"v": 1, "state": "starting"})

    def record(self, key: str, identity: ProcessIdentity) -> None:
        self._write(key, {"v": 1, "state": "running", "pid": identity.pid, "start_ticks": identity.start_ticks,
                          "boot_id": identity.boot_id})

    def finish(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def pending(self) -> list[str]:
        return sorted(p.name[:-5] for p in self._dir.glob("*.json"))

    # ------------------------------------------------------------------ startup side
    def fence_all(self, *, kill_wait_seconds: float = 5.0) -> FenceReport:
        """Prove or enforce that no member of a previous daemon's agent groups survives (see Limits above).

        Call under the ownership lock. Entries over the cap and ``*.unverified`` files left by earlier startups
        are counted as unverified every time, so an unresolved doubt never silently disappears.
        """
        dead = killed = 0
        unverified = len(list(self._dir.glob("*.unverified")))  # still unresolved from an earlier startup
        budget = time.monotonic() + _FENCE_BUDGET_SECONDS
        entries = sorted(self._dir.glob("*.json"))
        for path in entries[:_MAX_ENTRIES]:
            verdict = self._fence_one(path, min(kill_wait_seconds, max(0.0, budget - time.monotonic())))
            if verdict == "dead":
                dead += 1
                path.unlink(missing_ok=True)
            elif verdict == "killed":
                killed += 1
                path.unlink(missing_ok=True)
            else:
                unverified += 1
                try:
                    os.replace(path, path.with_suffix(".unverified"))
                except OSError:
                    pass
        unverified += max(0, len(entries) - _MAX_ENTRIES)  # never fenced: left in place, reported
        for stray in self._dir.glob("*.tmp"):
            stray.unlink(missing_ok=True)  # an interrupted write never completed: no pid was recorded in it
        return FenceReport(dead, killed, unverified)

    @staticmethod
    def _read_entry(path: Path) -> dict | None:
        """Bounded read of a regular file only: never follows a symlink, never blocks on a FIFO or device."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            raw = os.read(fd, _MAX_FILE_BYTES + 1)
        except OSError:
            return None
        finally:
            os.close(fd)
        if len(raw) > _MAX_FILE_BYTES:
            return None
        try:
            body = json.loads(raw)
        except ValueError:
            return None
        return body if isinstance(body, dict) and body.get("v") == 1 else None

    def _fence_one(self, path: Path, wait: float) -> str:
        body = self._read_entry(path)
        if body is None or body.get("state") == "starting":
            return "unverified"  # malformed, or a spawn that may have happened with no pid recorded
        pid, ticks, boot = body.get("pid"), body.get("start_ticks"), body.get("boot_id")
        if type(pid) is not int or type(ticks) is not int or pid <= 1 or ticks <= 0 or not isinstance(boot, str):
            return "unverified"
        if pid in (os.getpid(), os.getpgrp(), os.getsid(0)):
            return "unverified"  # a forged or stale entry pointing at the daemon's own group or session
        try:
            if boot != _boot_id():
                return "dead"  # a reboot ended every process of the previous boot
            try:
                state, _, group, session, start = _stat(pid)
            except FileNotFoundError:
                return self._orphans_of_gone_leader(pid, ticks, wait)
            if start != ticks:
                # The pid belongs to someone else now. If a live group still carries that id we cannot tell the
                # old agent's members from the new owner's: never signal it, report it.
                return "dead" if not _group_members(pid) else "unverified"
            if state in ("Z", "X") and not _group_members(pid):
                return "dead"
            if group != pid or session != pid:
                return "unverified"  # not the session leader we spawned: refuse to signal an ambiguous group
            return self._kill_group(pid, wait)
        except (OSError, ValueError, IndexError):
            return "unverified"

    def _orphans_of_gone_leader(self, pgid: int, recorded_ticks: int, wait: float) -> str:
        """The recorded leader is gone. Members left in that group are ours only if they look like our children:
        same session id (we spawn with start_new_session) and started no earlier than the recorded leader. Anything
        else is ambiguous (the id may belong to an unrelated group after pid wrap): report, never kill."""
        members = _group_members(pgid)
        if not members:
            return "dead"
        for member in members:
            try:
                _, _, _, session, start = _stat(member)
            except (OSError, ValueError, IndexError):
                return "unverified"
            if session != pgid or start < recorded_ticks:
                return "unverified"
        return self._kill_group(pgid, wait)

    def _kill_group(self, pgid: int, wait: float) -> str:
        """SIGKILL the group that the recorded leader led, then check no live member remains."""
        if wait <= 0:
            return "unverified"  # the fence's time budget is spent: do not start something it cannot verify
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return "unverified"
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not _group_members(pgid):
                return "killed"
            time.sleep(0.05)
        return "unverified"


def _group_members(pgid: int) -> list[int]:
    """Live (non-zombie) processes whose process group id is ``pgid``."""
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            state, _, group, _, _ = _stat(int(name))
        except (OSError, ValueError, IndexError):
            continue
        if group == pgid and state not in ("Z", "X"):
            found.append(int(name))
    return found
