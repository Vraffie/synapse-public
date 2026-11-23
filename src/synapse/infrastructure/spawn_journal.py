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
  kept as ``*.unverified`` for the operator.

Linux only (``/proc``); local single machine. Files are 0600 in a 0700 directory, written atomically with fsync,
and contain only ids, pid, start ticks and boot id: never a prompt, path or credential.
"""
from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from synapse.infrastructure.mock_process_identity import ProcessIdentity, _boot_id, _stat

_KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
_MAX_ENTRIES = 1000
_MAX_FILE_BYTES = 4096


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
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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
        """Prove or enforce that no process from a previous daemon survives. Call under the ownership lock."""
        dead = killed = unverified = 0
        for path in sorted(self._dir.glob("*.json"))[:_MAX_ENTRIES]:
            verdict = self._fence_one(path, kill_wait_seconds)
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
        for stray in self._dir.glob("*.tmp"):
            stray.unlink(missing_ok=True)  # an interrupted write never completed: no pid was recorded in it
        return FenceReport(dead, killed, unverified)

    def _fence_one(self, path: Path, wait: float) -> str:
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                return "unverified"
            body = json.loads(path.read_text())
            if not isinstance(body, dict) or body.get("v") != 1:
                return "unverified"
            if body.get("state") == "starting":
                return "unverified"  # a spawn may have happened with no pid recorded: cannot be proven dead
            pid, ticks, boot = body.get("pid"), body.get("start_ticks"), body.get("boot_id")
            if type(pid) is not int or type(ticks) is not int or pid <= 1 or ticks <= 0 or not isinstance(boot, str):
                return "unverified"
        except (OSError, ValueError):
            return "unverified"
        try:
            if boot != _boot_id():
                return "dead"  # a reboot ended every process of the previous boot
            try:
                state, _, group, session, start = _stat(pid)
            except FileNotFoundError:
                return "dead" if not _group_members(pid) else self._kill_group(pid, wait)
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

    def _kill_group(self, pgid: int, wait: float) -> str:
        """SIGKILL the group that the recorded leader led, then check no live member remains."""
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
