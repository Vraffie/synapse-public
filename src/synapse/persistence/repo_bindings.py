"""Multi-repository run storage (ADR 026 slice 1, migration 0018).

* ``run_repo_bindings``: the registry entries a run uses, with each baseline ref pinned to a commit SHA, written in
  the transaction that starts the run (``bind_run_repos``). Insert-only.
* ``attempt_worktrees``: one row per (attempt, repository) worktree, written after all of an attempt's worktrees
  exist and before its single spawn (``record_attempt_worktrees``). Insert-only.
* ``attempt_repo_commits``: the host-side commit phase, append-only: an ``intent`` before each git commit, then one
  result (``done``/``unchanged``/``failed``; ``not_done``/``unknown`` only from startup recovery). Triggers refuse a second intent
  for the same (attempt, repository) and any result without its intent.

Events and logs carry ids, SHAs, counts and fixed reason codes only, never repository paths or changed file names.
Every writer here runs inside the caller's transaction.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from synapse.domain.contracts import validate_id

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")
ACCESS_MODES = ("write", "read")


@dataclass(frozen=True, slots=True)
class RepoBinding:
    repo_id: str
    path: str
    baseline_ref: str
    baseline_sha: str
    protected_paths: tuple[str, ...]
    entry_sha256: str
    writer_task_id: str | None = None  # the one task that may write this repository in the run (None: read-only)


@dataclass(frozen=True, slots=True)
class AttemptWorktree:
    repo_id: str
    access: str
    path: str
    branch: str | None
    baseline_sha: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _need_txn(db: sqlite3.Connection, what: str) -> None:
    if not db.in_transaction:
        raise RuntimeError("%s requires a transaction" % what)


def _sha(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("%s must be a lowercase hex commit id" % what)
    return value


def bindings_digest(bindings: Iterable[RepoBinding]) -> str:
    """``bindings_sha256`` (ADR 026 D1): canonical JSON of this run's bindings sorted by repo_id, each
    ``{repo_id, entry_sha256, baseline_ref, baseline_sha, writer_task_id}``. The task specs (and so any approval)
    and later the manifest carry it; ``registry_sha256`` (the whole registry) is only in the startup audit event."""
    body = [{"repo_id": b.repo_id, "entry_sha256": b.entry_sha256, "baseline_ref": b.baseline_ref,
             "baseline_sha": b.baseline_sha, "writer_task_id": b.writer_task_id}
            for b in sorted(bindings, key=lambda b: b.repo_id)]
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bind_run_repos(db: sqlite3.Connection, run_id: str, bindings: Sequence[RepoBinding]) -> str:
    """Insert the run's bindings (caller's transaction, normally the one that starts the run). Returns the digest."""
    _need_txn(db, "Binding run repositories")
    validate_id(run_id)
    if not bindings:
        raise ValueError("a multi-repository run needs at least one repository binding")
    ids = [b.repo_id for b in bindings]
    if len(set(ids)) != len(ids):
        raise ValueError("repository bindings must be unique per run")
    digest = bindings_digest(bindings)
    now = _now()
    for b in sorted(bindings, key=lambda b: b.repo_id):
        validate_id(b.repo_id)
        _sha(b.baseline_sha, "baseline_sha")
        db.execute("INSERT INTO run_repo_bindings(run_id,repo_id,path,baseline_ref,baseline_sha,writer_task_id,"
                   "protected_paths_json,entry_sha256,bindings_sha256,bound_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (run_id, b.repo_id, b.path, b.baseline_ref, b.baseline_sha, b.writer_task_id,
                    json.dumps(list(b.protected_paths), separators=(",", ":")), b.entry_sha256, digest, now))
    return digest


def load_run_repos(db: sqlite3.Connection, run_id: str) -> dict[str, RepoBinding]:
    """The run's bindings keyed by repo_id (empty for a legacy single-repository run)."""
    rows = db.execute("SELECT repo_id,path,baseline_ref,baseline_sha,protected_paths_json,entry_sha256,writer_task_id "
                      "FROM run_repo_bindings WHERE run_id=? ORDER BY repo_id", (run_id,)).fetchall()
    return {r[0]: RepoBinding(r[0], r[1], r[2], r[3], tuple(json.loads(r[4])), r[5], r[6]) for r in rows}


def record_attempt_worktrees(db: sqlite3.Connection, attempt_id: str, trees: Sequence[AttemptWorktree]) -> None:
    _need_txn(db, "Recording attempt worktrees")
    validate_id(attempt_id)
    now = _now()
    for t in sorted(trees, key=lambda t: t.repo_id):
        if t.access not in ACCESS_MODES:
            raise ValueError("access must be write or read")
        db.execute("INSERT INTO attempt_worktrees(attempt_id,repo_id,access,path,branch,baseline_sha,created_at) "
                   "VALUES(?,?,?,?,?,?,?)", (attempt_id, t.repo_id, t.access, t.path, t.branch,
                                             _sha(t.baseline_sha, "baseline_sha"), now))


def load_attempt_worktrees(db: sqlite3.Connection, attempt_id: str) -> dict[str, AttemptWorktree]:
    rows = db.execute("SELECT repo_id,access,path,branch,baseline_sha FROM attempt_worktrees WHERE attempt_id=? "
                      "ORDER BY repo_id", (attempt_id,)).fetchall()
    return {r[0]: AttemptWorktree(*r) for r in rows}


def commit_intent(db: sqlite3.Connection, attempt_id: str, repo_id: str, head_before: str) -> None:
    _need_txn(db, "Recording a commit intent")
    db.execute("INSERT INTO attempt_repo_commits(attempt_id,repo_id,state,head_before,at) VALUES(?,?,'intent',?,?)",
               (attempt_id, repo_id, _sha(head_before, "head_before"), _now()))


def commit_result(db: sqlite3.Connection, attempt_id: str, repo_id: str, head_before: str, *, state: str,
                  candidate_sha: str | None = None, reason: str | None = None, paths: int | None = None) -> None:
    """One result row for the (attempt, repo) intent; ``reason`` is a fixed code, never a path or git output."""
    _need_txn(db, "Recording a commit result")
    if state not in ("done", "unchanged", "failed", "not_done", "unknown"):
        raise ValueError("invalid commit result state")
    if reason is not None and not _REASON.fullmatch(reason):
        raise ValueError("commit result reason must be a fixed code")
    db.execute("INSERT INTO attempt_repo_commits(attempt_id,repo_id,state,head_before,candidate_sha,reason,paths,at) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (attempt_id, repo_id, state, _sha(head_before, "head_before"),
                None if candidate_sha is None else _sha(candidate_sha, "candidate_sha"), reason, paths, _now()))


def commit_states(db: sqlite3.Connection, attempt_id: str) -> dict[str, str]:
    """Latest commit-phase state per repo_id for the attempt."""
    rows = db.execute("SELECT repo_id,state FROM attempt_repo_commits WHERE attempt_id=? ORDER BY seq", (attempt_id,))
    out: dict[str, str] = {}
    for repo_id, state in rows:
        out[repo_id] = state
    return out


@dataclass(frozen=True, slots=True)
class DanglingIntent:
    attempt_id: str
    repo_id: str
    head_before: str
    run_id: str
    task_id: str
    attempt_no: int
    repo_path: str | None
    worktree: AttemptWorktree | None


def dangling_intents(db: sqlite3.Connection) -> list[DanglingIntent]:
    """Every commit intent without a result (a crash during the commit phase), oldest first."""
    rows = db.execute(
        "SELECT c.attempt_id,c.repo_id,c.head_before,t.run_id,a.task_id,a.attempt_no FROM attempt_repo_commits c "
        "JOIN task_attempts a ON a.id=c.attempt_id JOIN tasks t ON t.id=a.task_id WHERE c.state='intent' "
        "AND NOT EXISTS (SELECT 1 FROM attempt_repo_commits r WHERE r.attempt_id=c.attempt_id AND r.repo_id=c.repo_id "
        "AND r.state<>'intent') ORDER BY c.seq").fetchall()
    out = []
    for attempt_id, repo_id, head, run_id, task_id, attempt_no in rows:
        binding = load_run_repos(db, run_id).get(repo_id)
        out.append(DanglingIntent(attempt_id, repo_id, head, run_id, task_id, attempt_no,
                                  None if binding is None else binding.path,
                                  load_attempt_worktrees(db, attempt_id).get(repo_id)))
    return out


def check_workspace_specs(specs: Mapping[str, Mapping[str, Any]], bindings: Sequence[RepoBinding] | None,
                          task_ids: Mapping[str, str] | None = None) -> None:
    """Pure cross-check of a run's task specs against its bindings (raises ValueError).

    A run is either legacy (no ``repos`` spec, no bindings) or multi-repository (every spec declares ``repos`` and
    bindings exist). Every declared repository must be bound; exactly the binding's ``writer_task_id`` (when
    ``task_ids`` maps nodes to task ids) and at most one task may declare ``write`` on a repository.
    """
    multi = [n for n, s in specs.items() if "repos" in s]
    if not bindings:
        if multi:
            raise ValueError("a multi-repository task needs the run's repository bindings")
        return
    if len(multi) != len(specs):
        raise ValueError("every task of a multi-repository run must declare its repositories")
    bound = {b.repo_id: b for b in bindings}
    if len(bound) != len(bindings):
        raise ValueError("repository bindings must be unique per run")
    writers: dict[str, str] = {}
    for node, spec in specs.items():
        for r in spec["repos"]:
            b = bound.get(r["repo_id"])
            if b is None:
                raise ValueError("a task repository is not bound to the run")
            if r["access"] == "write":
                if r["repo_id"] in writers:
                    raise ValueError("at most one task per run may write a repository")
                writers[r["repo_id"]] = node
    if task_ids is not None:
        for b in bindings:
            node = writers.get(b.repo_id)
            if b.writer_task_id != (None if node is None else task_ids.get(node)):
                raise ValueError("a binding's writer task differs from the task that declares write")


def record_removal(db: sqlite3.Connection, attempt_id: str, repo_id: str, reason: str, forced: bool) -> None:
    _need_txn(db, "Recording a worktree removal")
    if not _REASON.fullmatch(reason):
        raise ValueError("removal reason must be a fixed code")
    db.execute("INSERT INTO attempt_worktree_removals(attempt_id,repo_id,reason,forced,removed_at) VALUES(?,?,?,?,?)",
               (attempt_id, repo_id, reason, 1 if forced else 0, _now()))


def entry_digest(entry: Mapping[str, Any]) -> str:
    """SHA-256 of one registry entry's canonical JSON (what ``entry_sha256`` records)."""
    return hashlib.sha256(json.dumps(dict(entry), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
