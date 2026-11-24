"""Multi-repository runs, daemon side (ADR 026 slice 1).

* ``resolve_run_repos``: turn a run request's ``repos: [{repo_id, baseline_ref}]`` into pinned bindings, before any
  state change: each repository must be an object entry of the operator registry, the ref one of its
  ``baseline_refs``, and the ref must resolve to a commit (``WorktreeManager.check_base``). The bindings are then
  written in the transaction that starts the run (``AgentRuntime.start``).
* ``AttemptWorkspaces``: the blocking hooks the agent engine calls in a worker thread to create an attempt's
  worktrees (all or none, ordered locks), discard them, and commit each write-access worktree.
* ``resolve_commit_intents``: startup recovery of a commit phase interrupted by a crash (ADR 026 D6). Never re-runs a
  commit; it only records what git shows: ``not_done`` (the branch did not move), ``done`` (exactly one commit on top
  of the recorded HEAD whose tree is the worktree's index) or ``unknown`` (anything else).
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from synapse.daemon.agent_config import RepoEntry
from synapse.infrastructure.worktrees import AttemptRepo, WorktreeError, WorktreeManager
from synapse.persistence.agent_tasks import load_task_specs
from synapse.persistence.repo_bindings import (
    AttemptWorktree, RepoBinding, commit_result, dangling_intents, load_run_repos, record_removal)

_LOG = logging.getLogger(__name__)
MAX_RUN_REPOS = 16


class RepoRequestError(ValueError):
    """A multi-repository run request is refused before any state change (fixed message, no paths)."""


@dataclass(frozen=True, slots=True)
class RepoRegistry:
    """The operator's object entries (read once at daemon start; never written by the daemon)."""
    entries: tuple[RepoEntry, ...] = ()

    def get(self, repo_id: str) -> RepoEntry | None:
        for e in self.entries:
            if e.id == repo_id:
                return e
        return None


def resolve_run_repos(registry: RepoRegistry, manager: WorktreeManager,
                      requested: Sequence[Mapping[str, str]]) -> list[RepoBinding]:
    """Pin each requested repository's baseline ref to a commit SHA (blocking git; no state change).

    Raises ``RepoRequestError`` for an unregistered repository, a ref that is not one of the entry's
    ``baseline_refs``, or a ref/repository git refuses. All repositories are resolved before anything is written,
    so a failure leaves no binding and no worktree.
    """
    out: list[RepoBinding] = []
    for r in sorted(requested, key=lambda r: r["repo_id"]):
        entry = registry.get(r["repo_id"])
        if entry is None:
            raise RepoRequestError("repo_not_registered: a requested repo_id is not a repository object in the operator registry")
        ref = r.get("baseline_ref") or entry.default_baseline_ref
        if ref is None or ref not in entry.baseline_refs:
            raise RepoRequestError("baseline_ref_not_allowed: a baseline_ref is not one of that repository's baseline_refs")
        try:
            sha = manager.check_base(entry.path, ref)
        except ValueError:
            raise RepoRequestError("baseline_ref_unresolved: a baseline_ref does not resolve to a commit") from None
        except WorktreeError:
            raise RepoRequestError("repo_unusable: a repository cannot be used for an agent run") from None
        out.append(RepoBinding(entry.id, entry.path, ref, sha, entry.protected_paths, entry.entry_sha256))
    return out


class AttemptWorkspaces:
    """Engine hooks over ``WorktreeManager`` (blocking; the engine calls them in a worker thread)."""

    def __init__(self, manager: WorktreeManager) -> None:
        self.manager = manager

    @staticmethod
    def _repos(repos: Sequence[Mapping[str, str]], bindings: Mapping[str, RepoBinding]) -> list[AttemptRepo]:
        return [AttemptRepo(r["repo_id"], bindings[r["repo_id"]].path, bindings[r["repo_id"]].baseline_sha,
                            r["access"]) for r in repos]

    def prepare(self, run_id: str, task_id: str, attempt_no: int, repos: Sequence[Mapping[str, str]],
                bindings: Mapping[str, RepoBinding]) -> tuple[str, list[AttemptWorktree]]:
        wanted = self._repos(repos, bindings)
        paths = self.manager.create_attempt(run_id, task_id, attempt_no, wanted)
        branch = self.manager.attempt_branch(run_id, task_id, attempt_no)
        trees = [AttemptWorktree(r.repo_id, r.access, paths[r.repo_id], branch if r.access == "write" else None,
                                 r.baseline_sha) for r in sorted(wanted, key=lambda r: r.repo_id)]
        return self.manager.attempt_dir(run_id, task_id, attempt_no), trees

    def discard(self, run_id: str, task_id: str, attempt_no: int, repos: Sequence[Mapping[str, str]],
                bindings: Mapping[str, RepoBinding]) -> None:
        self.manager.discard_attempt(run_id, task_id, attempt_no, self._repos(repos, bindings))

    def recheck(self, run_id: str, task_id: str, attempt_no: int, trees: Sequence[AttemptWorktree],
                bindings: Mapping[str, RepoBinding]) -> str | None:
        """Spawn-time recheck of every recorded worktree, in repo_id order; the first refusal code or None."""
        if not trees:
            return "worktree_mismatch"
        for t in sorted(trees, key=lambda t: t.repo_id):
            b = bindings.get(t.repo_id)
            if b is None or t.baseline_sha != b.baseline_sha:
                return "worktree_mismatch"
            problem = self.manager.attempt_tree_problem(run_id, task_id, attempt_no, t.repo_id, b.path, t.path,
                                                        b.baseline_sha)
            if problem is not None:
                return problem
        return None

    def commit(self, run_id: str, task_id: str, attempt_no: int, repo_id: str, binding: RepoBinding, message: str,
               expected_head: str) -> dict:
        return self.manager.commit_attempt(run_id, task_id, attempt_no, repo_id, binding.path, message,
                                           expected_head=expected_head, protected=binding.protected_paths)

    def cleanup_never_spawned(self, db: sqlite3.Connection, attempt_ids: Sequence[str]) -> dict[str, int]:
        """ADR 026 D4 step 8: remove (never forced) the worktrees of attempts ADR 024 re-queued as provably never
        spawned, recording each removal (``never_spawned``). A dirty or moved tree is kept and reported (logged by
        count). Works from the run's bindings and the task's declared repositories, so trees created before a crash
        that kept their rows from being recorded are found too. Returns counts per outcome."""
        counts: dict[str, int] = {}
        for attempt_id in attempt_ids:
            row = db.execute("SELECT t.run_id,a.task_id,a.attempt_no,t.node_id FROM task_attempts a "
                             "JOIN tasks t ON t.id=a.task_id WHERE a.id=?", (attempt_id,)).fetchone()
            if row is None:
                continue
            run_id, task_id, attempt_no, node = row
            bindings = load_run_repos(db, run_id)
            specs = load_task_specs(db, run_id) or {}
            spec = specs.get(node)
            if not bindings or spec is None or "repos" not in spec:
                continue
            try:
                outcome = self.manager.remove_attempt_clean(run_id, task_id, attempt_no, self._repos(spec["repos"],
                                                                                                    bindings))
            except (WorktreeError, ValueError, OSError):
                _LOG.warning("never-spawned attempt %s: worktrees could not be checked; kept", attempt_id)
                counts["error"] = counts.get("error", 0) + 1
                continue
            db.execute("BEGIN IMMEDIATE")
            try:
                for repo_id, what in sorted(outcome.items()):
                    counts[what] = counts.get(what, 0) + 1
                    if what == "removed":
                        record_removal(db, attempt_id, repo_id, "never_spawned", False)
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        kept = sum(v for k, v in counts.items() if k.startswith("kept") or k == "error")
        if kept:
            _LOG.warning("never-spawned attempt worktrees kept for the operator: %d", kept)
        return counts


def resolve_commit_intents(db: sqlite3.Connection, manager: WorktreeManager | None) -> dict[str, int]:
    """Startup: give every dangling commit intent exactly one result row. Returns counts per state.

    Runs after the fence and before run recovery. With no manager (repos not configured) every dangling intent is
    ``unknown`` (``manager_unavailable``): nothing is guessed.
    """
    counts = {"done": 0, "not_done": 0, "unknown": 0}
    for intent in dangling_intents(db):
        state, sha, reason = "unknown", None, "unverifiable"
        if manager is None or intent.repo_path is None or intent.worktree is None:
            reason = "manager_unavailable" if manager is None else "not_recorded"
        else:
            try:
                head = manager.attempt_branch_head(intent.run_id, intent.task_id, intent.attempt_no, intent.repo_path)
                if head == intent.head_before:
                    state, reason = "not_done", None
                else:
                    landed = manager.attempt_commit_landed(intent.run_id, intent.task_id, intent.attempt_no,
                                                           intent.repo_id, intent.repo_path, intent.head_before)
                    if landed is not None:
                        state, sha, reason = "done", landed, None
            except (WorktreeError, ValueError, OSError):
                _LOG.warning("commit intent of attempt %s could not be checked", intent.attempt_id)
        db.execute("BEGIN IMMEDIATE")
        try:
            commit_result(db, intent.attempt_id, intent.repo_id, intent.head_before, state=state,
                          candidate_sha=sha, reason=reason)
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        counts[state] += 1
    if any(counts.values()):
        _LOG.warning("resolved interrupted repository commits: %s", counts)
    return counts
