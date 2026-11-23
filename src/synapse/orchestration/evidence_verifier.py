"""Evidence verifier: a stronger, opt-in alternative to ``process_outcome_verifier`` (ADR 025 part 2).

``process_outcome_verifier`` only confirms the engine observed a ``succeeded`` process. ``EvidenceVerifier`` keeps
the same interface (``async (task_id, candidate) -> bool``) and, for the nodes that request it, additionally checks
evidence the daemon gathers itself, host side:

1. the process-outcome check passes (it is always run first; evidence never replaces it);
2. the candidate bytes are exactly the summary artifact recorded for the task's current completed attempt
   (SHA-256 match, run/task/attempt provenance);
3. the worktree still sits on the recorded HEAD (the agent did not commit or reset) when the spec records one;
4. the worktree has changes (``require_changes``) and every changed path, tracked, untracked or ignored (so no
   .gitignore can hide one), is inside the node's ``allowed_paths``, is not a symlink and is a plain relative path;
5. an optional check command (exact absolute executable + argument list, no shell, hardened environment, rlimits,
   wall-clock timeout, bounded output, whole process group killed on expiry) exits 0, AND HEAD plus every changed
   path's content are identical afterwards (the check ran with write access to the worktree).

Every evaluation of a configured node publishes an evidence artifact (bounded) and a ``verification.evidence``
event with ids and reason codes only. Nodes without a policy are delegated unchanged to the fallback verifier, so
the default behaviour of the engine does not change.

SECURITY LIMIT: a test command executes code the agent wrote. ``CheckCommand`` refuses any argv[0] that is not an
operator-allowlisted sandbox launcher unless ``experimental_unsandboxed=True`` (default off); this module does not
verify what a launcher confines. Unsandboxed output is not kept in the evidence artifact. A pass is evidence, not proof.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import os
import resource
import signal
import stat
import sqlite3
import subprocess
import threading
import time
from typing import Any, Protocol

from synapse.domain.contracts import EventEnvelope, encode_payload, new_id, validate_id
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.persistence.artifact_store import publish_artifact

EVIDENCE_VERIFIER_ID = "builtin.agent.evidence-verifier.v1"
EVIDENCE_EVENT = "verification.evidence"
EVIDENCE_KIND = "verification_evidence"
MAX_ARGV = 64
MAX_ARG_BYTES = 4096
MAX_CHECK_TIMEOUT = 3600.0
MAX_CHECK_OUTPUT = 16 * 1024 * 1024
EVIDENCE_TAIL_BYTES = 4096
MAX_LISTED_PATHS = 50
MAX_LISTED_PATH_CHARS = 256
CHECK_PATH = "/usr/local/bin:/usr/bin:/bin"


def _hardened_env() -> dict[str, str]:
    """Same shape as WorktreeManager's hardened git env: no inherited secrets, no user/system git config."""
    return {"PATH": CHECK_PATH, "HOME": "/nonexistent", "LC_ALL": "C", "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_ASKPASS": "/bin/false", "PYTHONDONTWRITEBYTECODE": "1"}


@dataclass(frozen=True, slots=True)
class CheckCommand:
    """An exact executable and argument list. A shell string is refused by construction."""

    argv: tuple[str, ...]
    timeout_seconds: float = 300.0
    max_output_bytes: int = 64 * 1024
    # argv[0] must be one of these operator-allowlisted sandbox launchers (absolute paths; e.g. a bwrap wrapper)...
    sandbox_launchers: frozenset[str] = frozenset()
    # ...unless this explicit, default-off flag accepts running agent-written code unsandboxed as the daemon uid.
    experimental_unsandboxed: bool = False
    memory_limit_bytes: int | None = None  # RLIMIT_AS; opt-in (JS runtimes reserve address space)
    file_size_limit_bytes: int = 64 * 1024 * 1024  # RLIMIT_FSIZE

    @property
    def sandboxed(self) -> bool:
        return self.argv[0] in self.sandbox_launchers

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple) or not 1 <= len(self.argv) <= MAX_ARGV:
            raise ValueError("argv must be a tuple of 1..%d strings (no shell string)" % MAX_ARGV)
        for arg in self.argv:
            if not isinstance(arg, str) or "\x00" in arg or len(arg.encode("utf-8", "replace")) > MAX_ARG_BYTES:
                raise ValueError("every argv element must be a string without NUL, at most %d bytes" % MAX_ARG_BYTES)
        exe = self.argv[0]
        if not os.path.isabs(exe) or os.path.normpath(exe) != exe:
            raise ValueError("argv[0] must be an absolute, normalized executable path")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) \
                or not 0 < self.timeout_seconds <= MAX_CHECK_TIMEOUT:
            raise ValueError("timeout_seconds must be in (0, %d]" % MAX_CHECK_TIMEOUT)
        if type(self.max_output_bytes) is not int or not 1 <= self.max_output_bytes <= MAX_CHECK_OUTPUT:
            raise ValueError("max_output_bytes must be in [1, %d]" % MAX_CHECK_OUTPUT)
        if not isinstance(self.sandbox_launchers, frozenset) or any(
                not isinstance(x, str) or "\x00" in x or not os.path.isabs(x) or os.path.normpath(x) != x
                for x in self.sandbox_launchers):
            raise ValueError("sandbox_launchers must be a frozenset of absolute, normalized paths")
        if type(self.experimental_unsandboxed) is not bool:
            raise ValueError("experimental_unsandboxed must be bool")
        if not self.sandboxed and not self.experimental_unsandboxed:
            raise ValueError("argv[0] must be an operator-allowlisted sandbox launcher; running a check command "
                             "unsandboxed needs experimental_unsandboxed=True")
        if self.memory_limit_bytes is not None and (type(self.memory_limit_bytes) is not int
                                                    or self.memory_limit_bytes < 64 * 1024 * 1024):
            raise ValueError("memory_limit_bytes must be None or an int of at least 64 MiB")
        if type(self.file_size_limit_bytes) is not int or not 1024 <= self.file_size_limit_bytes <= 1 << 34:
            raise ValueError("file_size_limit_bytes must be in [1 KiB, 16 GiB]")


def _check_rel_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise ValueError("allowed path must be a non-empty relative path")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in path) or os.path.isabs(path):
        raise ValueError("allowed path must be a non-empty relative path")
    trimmed = path.rstrip("/")
    if not trimmed or os.path.normpath(trimmed) != trimmed or trimmed.split("/")[0] in ("..", ".", ".git") \
            or ".." in trimmed.split("/"):
        raise ValueError("allowed path must be normalized, inside the worktree and not under .git")
    return trimmed


@dataclass(frozen=True, slots=True)
class EvidencePolicy:
    """Per-node opt-in. ``allowed_paths`` are repo-relative files or directories (a directory covers its subtree)."""

    allowed_paths: tuple[str, ...]
    require_changes: bool = True
    check: CheckCommand | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_paths, tuple) or not 1 <= len(self.allowed_paths) <= 64:
            raise ValueError("allowed_paths must be a tuple of 1..64 relative paths")
        object.__setattr__(self, "allowed_paths", tuple(_check_rel_path(p) for p in self.allowed_paths))
        if type(self.require_changes) is not bool:
            raise ValueError("require_changes must be bool")
        if self.check is not None and type(self.check) is not CheckCommand:
            raise ValueError("check must be a CheckCommand")

    def allows(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in self.allowed_paths)


class WorktreeEvidence(Protocol):
    """Host-side facts about a task's worktree. Implementations raise on any doubt (truncation, unsafe repo)."""

    def path(self, run_id: str, task_id: str) -> str: ...
    def head(self, run_id: str, task_id: str) -> str: ...
    def changed_paths(self, run_id: str, task_id: str) -> tuple[str, ...]: ...


class GitWorktreeEvidence:
    """``WorktreeEvidence`` over a ``WorktreeManager``: its hardened git (scrubbed env, hooks/fsmonitor/pager off,
    dangerous repo config refused, attributes pinned to the empty tree), argv lists only, bounded output."""

    def __init__(self, manager: Any, *, max_status_bytes: int = 1 << 20):
        self._m, self._max = manager, max_status_bytes

    def _found(self, run_id: str, task_id: str) -> tuple[str, str]:
        found = self._m._existing(run_id, task_id)  # containment + symlink + backlink checks
        if found is None:
            raise ValueError("worktree does not exist")
        return found

    def path(self, run_id: str, task_id: str) -> str:
        return self._found(run_id, task_id)[0]

    def head(self, run_id: str, task_id: str) -> str:
        path, repo = self._found(run_id, task_id)
        return self._m._tree_git(repo, path, ["rev-parse", "--verify", "HEAD"])[1].strip()

    def changed_paths(self, run_id: str, task_id: str) -> tuple[str, ...]:
        path, repo = self._found(run_id, task_id)
        _, out, truncated = self._m._tree_git(repo, path, ["status", "--porcelain=v1", "-z", "--no-renames",
                                                           "--untracked-files=all", "--ignored=matching"],
                                              max_bytes=self._max)
        if truncated:
            raise ValueError("worktree status exceeds the evidence bound")
        paths = []
        for entry in out.split("\x00"):
            if not entry:
                continue
            # "!! path" entries are ignored files: listed on purpose, so a .gitignore the agent wrote (or the
            # repo's own, or info/exclude) cannot hide a file from the allowed-paths check.
            if len(entry) < 4 or entry[2] != " ":
                raise ValueError("unexpected git status entry")
            paths.append(entry[3:])
        return tuple(paths)


@dataclass(slots=True)
class CheckResult:
    exit_code: int | None
    timed_out: bool = False
    output_exceeded: bool = False
    start_failed: bool = False
    tail: bytes = b""
    seconds: float = 0.0


def _limits(command: CheckCommand) -> list[tuple[int, int]]:
    out = [(resource.RLIMIT_CPU, int(command.timeout_seconds) + 1), (resource.RLIMIT_CORE, 0),
           (resource.RLIMIT_NOFILE, 1024), (resource.RLIMIT_FSIZE, command.file_size_limit_bytes)]
    if command.memory_limit_bytes is not None:
        out.append((resource.RLIMIT_AS, command.memory_limit_bytes))
    return out


def _preexec(limits: list[tuple[int, int]]) -> Callable[[], None]:
    def apply() -> None:  # runs in the forked child before exec: only getrlimit/setrlimit, no imports or locks
        for which, value in limits:
            hard = resource.getrlimit(which)[1]
            value = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(which, (value, value))
    return apply


def run_check(command: CheckCommand, cwd: str) -> CheckResult:
    """Run the command host side: argv only, no shell, hardened env, own process group, bounded output.

    The process group is SIGKILLed on timeout, on output overflow, and after a normal exit too (descendants that
    outlive the leader). The leader is observed with ``WNOWAIT`` so it stays an unreaped zombie, which keeps its
    pid and process-group id from being reused until the group kill has been sent."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(list(command.argv), cwd=cwd, env=_hardened_env(), shell=False,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                close_fds=True, start_new_session=True, preexec_fn=_preexec(_limits(command)))
    except (OSError, ValueError, subprocess.SubprocessError):
        return CheckResult(None, start_failed=True, seconds=time.monotonic() - started)
    tail = bytearray()
    total = 0
    overflow = threading.Event()

    def read() -> None:
        nonlocal total
        try:
            while True:
                chunk = proc.stdout.read1(65536)  # type: ignore[union-attr]
                if not chunk:
                    return
                total += len(chunk)
                tail.extend(chunk)
                del tail[:-EVIDENCE_TAIL_BYTES]
                if total > command.max_output_bytes:
                    overflow.set()
                    return
        except (OSError, ValueError):
            return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = started + command.timeout_seconds
    timed_out = False
    while True:
        if overflow.is_set():
            break
        try:
            exited = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except ChildProcessError:
            exited = True
        if exited is not None and not reader.is_alive():
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.02)
    _kill_group(proc)
    code = proc.wait()
    reader.join(1)
    with contextlib.suppress(OSError, ValueError):
        proc.stdout.close()  # type: ignore[union-attr]
    return CheckResult(code, timed_out=timed_out, output_exceeded=overflow.is_set(), tail=bytes(tail),
                       seconds=time.monotonic() - started)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


@dataclass(slots=True)
class _Evidence:
    reasons: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)


class EvidenceVerifier:
    """Drop-in ``verify`` callable for ``AgentWorkflowEngine`` (see module docstring)."""

    def __init__(self, db: sqlite3.Connection, blobs: ArtifactBlobStore, specs: Mapping[str, Mapping[str, Any]],
                 policies: Mapping[str, EvidencePolicy], evidence: WorktreeEvidence | None, *,
                 fallback: Callable[[str, bytes], Awaitable[bool]], fallback_id: str,
                 principal: str = "evidence-verifier"):
        for task_id, policy in policies.items():
            validate_id(task_id)
            if type(policy) is not EvidencePolicy:
                raise ValueError("policies must map task ids to EvidencePolicy")
            if task_id not in specs:
                raise ValueError("evidence policy for an unknown task")
        if policies and evidence is None:
            raise ValueError("an evidence policy needs a WorktreeEvidence source")
        validate_id(fallback_id)
        self.db, self.blobs, self.specs, self.policies = db, blobs, specs, dict(policies)
        self.evidence, self.fallback, self.fallback_id, self.principal = evidence, fallback, fallback_id, principal

    def verifier_id_for(self, task_id: str) -> str:
        return EVIDENCE_VERIFIER_ID if task_id in self.policies else self.fallback_id

    async def __call__(self, task_id: str, candidate: bytes) -> bool:
        base = await self.fallback(task_id, candidate)
        if type(base) is not bool:
            raise ValueError("Verifier must return bool")
        policy = self.policies.get(task_id)
        if policy is None:
            return base
        ev = _Evidence()
        if not base:
            ev.reasons.append("process_outcome_rejected")
        summary = self._summary(task_id, candidate, ev)
        if summary is not None and not ev.reasons:
            run_id = summary["run_id"]
            try:
                await asyncio.to_thread(self._worktree_checks, run_id, task_id, policy, ev)
            except Exception:
                ev.reasons.append("evidence_unavailable")
        self._record(task_id, candidate, summary, ev)
        return not ev.reasons

    def _summary(self, task_id: str, candidate: bytes, ev: _Evidence) -> dict | None:
        sha = hashlib.sha256(candidate).hexdigest()
        ev.facts["candidate_sha256"] = sha
        try:
            summary = json.loads(candidate)
        except ValueError:
            ev.reasons.append("candidate_invalid")
            return None
        if not isinstance(summary, dict) or not isinstance(summary.get("run_id"), str) \
                or not isinstance(summary.get("attempt_id"), str):
            ev.reasons.append("candidate_invalid")
            return None
        task = self.db.execute("SELECT run_id,attempt_count FROM tasks WHERE id=?", (task_id,)).fetchone()
        attempt = None if task is None else self.db.execute(
            "SELECT id FROM task_attempts WHERE task_id=? AND attempt_no=? AND state='completed'",
            (task_id, task[1])).fetchone()
        if task is None or task[0] != summary["run_id"] or attempt is None or attempt[0] != summary["attempt_id"]:
            ev.reasons.append("candidate_not_current_attempt")
            return None
        rows = [r for r in self.db.execute("SELECT id,sha256,provenance_json FROM artifacts WHERE sha256=?", (sha,))
                if _provenance(r[2]) == (summary["run_id"], task_id, summary["attempt_id"], "agent_summary")]
        if len(rows) != 1:
            ev.reasons.append("candidate_hash_mismatch")
            return None
        ev.facts["candidate_artifact_id"] = rows[0][0]
        return summary

    def _worktree_checks(self, run_id: str, task_id: str, policy: EvidencePolicy, ev: _Evidence) -> None:
        spec = self.specs[task_id]
        src = self.evidence
        path = os.path.realpath(src.path(run_id, task_id))
        if path != os.path.realpath(spec["worktree"]):
            ev.reasons.append("worktree_mismatch")
            return
        before = self._snapshot(run_id, task_id, path)
        head = before[0]
        ev.facts["head"] = head
        expected = spec.get("worktree_head")
        if isinstance(expected, str) and head != expected:
            ev.reasons.append("head_moved")
        changed = tuple(p for p, _ in before[1])
        ev.facts["changed_count"] = len(changed)
        ev.facts["changed_paths"] = [p[:MAX_LISTED_PATH_CHARS] for p in changed[:MAX_LISTED_PATHS]]
        if policy.require_changes and not changed:
            ev.reasons.append("no_changes")
        outside = [p for p in changed if not _plain(p) or not policy.allows(p)]
        if outside:
            ev.reasons.append("path_outside_allowed")
            ev.facts["outside_paths"] = [p[:MAX_LISTED_PATH_CHARS] for p in outside[:MAX_LISTED_PATHS]]
        links = [p for p in changed if _plain(p) and os.path.islink(os.path.join(path, p))]
        if links:
            ev.reasons.append("symlink_changed")
        if policy.check is not None and not ev.reasons:
            if not policy.check.sandboxed and not policy.check.experimental_unsandboxed:
                ev.reasons.append("check_not_sandboxed")  # unreachable via the constructor; fail closed anyway
                return
            result = run_check(policy.check, path)
            ev.facts["check"] = {"exit_code": result.exit_code, "timed_out": result.timed_out,
                                 "output_exceeded": result.output_exceeded, "start_failed": result.start_failed,
                                 "seconds": round(result.seconds, 3), "sandboxed": policy.check.sandboxed,
                                 # unsandboxed output is text from an unconfined agent-code process: not kept
                                 "output_tail": result.tail.decode("utf-8", "replace").replace("\x00", "�")
                                 if policy.check.sandboxed else None}
            # The check ran code with write access to the worktree: the evidence gathered before it must still hold.
            try:
                after = self._snapshot(run_id, task_id, path)
            except Exception:
                after = None
            if after != before:
                ev.reasons.append("check_modified_worktree")
                return
            if result.start_failed:
                ev.reasons.append("check_not_started")
            elif result.timed_out:
                ev.reasons.append("check_timed_out")
            elif result.output_exceeded:
                ev.reasons.append("check_output_exceeded")
            elif result.exit_code != 0:
                ev.reasons.append("check_failed")

    def _snapshot(self, run_id: str, task_id: str, path: str) -> tuple[str, tuple[tuple[str, str], ...]]:
        """HEAD plus every changed path (tracked, untracked, ignored) with a digest of what is there now."""
        head = self.evidence.head(run_id, task_id)
        changed = self.evidence.changed_paths(run_id, task_id)
        return head, tuple((p, _digest(path, p)) for p in changed)

    def _record(self, task_id: str, candidate: bytes, summary: dict | None, ev: _Evidence) -> None:
        run_id = summary["run_id"] if summary else self.db.execute("SELECT run_id FROM tasks WHERE id=?",
                                                                   (task_id,)).fetchone()[0]
        attempt_id = summary["attempt_id"] if summary else None
        body = {"v": 1, "verifier_id": EVIDENCE_VERIFIER_ID, "task_id": task_id, "attempt_id": attempt_id,
                "passed": not ev.reasons, "reasons": ev.reasons, **ev.facts}
        data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        artifact_id = new_id()
        ref = publish_artifact(self.db, self.blobs, chunks=[data], artifact_id=artifact_id,
                               media_type="application/json",
                               provenance={"run_id": run_id, "task_id": task_id, "attempt_id": attempt_id,
                                           "kind": EVIDENCE_KIND},
                               principal=self.principal, idempotency_key="evidence-" + artifact_id).response
        event = EventEnvelope.create(aggregate_type="task", aggregate_id=task_id, event_type=EVIDENCE_EVENT,
                                     payload={"task_id": task_id, "attempt_id": attempt_id,
                                              "candidate_sha256": ev.facts.get("candidate_sha256"),
                                              "evidence_artifact_id": artifact_id,
                                              "evidence_sha256": ref["sha256"], "passed": not ev.reasons,
                                              "reasons": ev.reasons})
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,"
                            "payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                            (event.id, event.aggregate_type, task_id, run_id, event.event_type, event.schema_version,
                             encode_payload(event.payload), event.occurred_at))
            self.db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise


def _digest(root: str, rel: str) -> str:
    """Content identity of one changed path, never following a symlink out of the worktree."""
    if not _plain(rel):
        return "invalid"
    full = os.path.join(root, rel)
    try:
        st = os.lstat(full)
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(st.st_mode):
        return "link:" + hashlib.sha256(os.fsencode(os.readlink(full))).hexdigest()
    if not stat.S_ISREG(st.st_mode):
        return "other:%o" % st.st_mode
    h = hashlib.sha256()
    with os.fdopen(os.open(full, os.O_RDONLY | os.O_NOFOLLOW), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "file:%o:%s" % (st.st_mode & 0o7777, h.hexdigest())


def _plain(path: str) -> bool:
    parts = path.split("/")
    return (bool(path) and not path.startswith("/") and "\x00" not in path and "\\" not in path
            and not any(ord(c) < 0x20 or ord(c) == 0x7f for c in path)
            and all(p not in ("", ".", "..") for p in parts) and parts[0] != ".git")


def _provenance(raw: str) -> tuple | None:
    try:
        p = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(p, dict):
        return None
    return (p.get("run_id"), p.get("task_id"), p.get("attempt_id"), p.get("kind"))


__all__ = ["CheckCommand", "CheckResult", "EVIDENCE_EVENT", "EVIDENCE_VERIFIER_ID", "EvidencePolicy",
           "EvidenceVerifier", "GitWorktreeEvidence", "WorktreeEvidence", "run_check"]
