"""Candidate manifest v2 builder and verifier, daemon side (ADR 026 D2, D3, D5, D8, D9).

Built by the daemon on an operator request (or a workflow node), never by an agent:

1. **D2 baseline check**: every binding's ``baseline_ref`` is re-resolved and compared with the pinned SHA. A
   difference records ``baseline_moved`` for every manifest of the run and refuses; nothing is rebased.
2. **Sources** (D5/D6): for every writer task, ONE attempt (the latest completed one, or the one the operator
   names) whose commit phase recorded ``done`` or ``unchanged`` for every repository the task writes, cut from the
   pinned baselines. A ``repo_id`` outside ``run_repo_bindings`` or a candidate from a non-writer cannot appear.
3. **verify_manifest_candidates** (D3 enforcement point 2): the manifest is re-checked against the bindings and the
   commit rows, and every write repository's ``diff-tree baseline candidate`` re-runs the commit rule (XML only,
   protected paths, mode 100644, well-formed blobs from the object store); the candidate must descend from the
   baseline and the diff bytes must hash to ``diff_sha256``.
4. Publish the diffs and the canonical manifest as immutable artifacts, then index it in ``candidate_manifests``.

Step 1 runs again after publishing and before the row is indexed, so a baseline that moves during a build refuses
it. ``recheck`` repeats 1 and 3 for an existing manifest and records ``candidate_invalid`` when the candidate no
longer passes. It runs when a deploy approval is requested (``request_manifest_approval``) and MUST run in the
deploy pre-check (D9 step 2); the approval decision does NOT call it and refuses only on recorded invalidations. A git failure that proves nothing (git missing, timeout)
refuses without invalidating. Everything here is blocking (git) and must run off the event loop.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from synapse.application.candidate_manifest import DEFAULT_MAX_DIFF_BYTES, DIFF_PROVENANCE_KIND, _artifact_id
from synapse.application.candidate_manifest_v2 import (
    EMPTY_DIFF_SHA256, ManifestError, ManifestV2, RepoEntry, SourceAttempt, build_manifest_v2, passed_validations,
    publish_manifest_v2, read_manifest_v2_artifact)
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.infrastructure.worktrees import CommitRefused, WorktreeError, WorktreeManager
from synapse.persistence import candidate_manifests as store
from synapse.persistence.artifact_store import publish_artifact
from synapse.persistence.repo_bindings import RepoBinding, bindings_digest, load_attempt_worktrees, load_run_repos

PRINCIPAL = "daemon:manifest"
ATTEMPT_DONE = "completed"


class ManifestRefused(ValueError):
    """The manifest cannot be built or used. ``code`` is a fixed reason (``baseline_moved``, ``candidate_invalid``,
    ``manifest_invalidated``, ``no_changes``, ...); ``detail`` is a fixed sub-code, never a path or git output."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if detail is None else "%s:%s" % (code, detail))
        self.code, self.detail = code, detail


@dataclass(frozen=True, slots=True)
class _Source:
    attempt_id: str
    attempt_no: int


def _bindings(db: sqlite3.Connection, run_id: str) -> tuple[dict[str, RepoBinding], str]:
    bindings = load_run_repos(db, run_id)
    if not bindings:
        raise ManifestRefused("not_multi_repo")
    stored = {r[0] for r in db.execute("SELECT DISTINCT bindings_sha256 FROM run_repo_bindings WHERE run_id=?",
                                       (run_id,))}
    digest = bindings_digest(bindings.values())
    if stored != {digest}:
        raise ManifestRefused("candidate_invalid", "bindings_mismatch")
    return bindings, digest


# ---------------------------------------------------------------- D2
def check_baselines(db: sqlite3.Connection, manager: WorktreeManager, run_id: str, *, actor: str = PRINCIPAL) -> None:
    """Re-resolve every pinned baseline ref. Moved (or no longer resolvable): ``baseline_moved`` is recorded for every
    manifest of the run in its own transaction and ``ManifestRefused('baseline_moved')`` is raised."""
    bindings, _ = _bindings(db, run_id)
    moved = False
    for b in bindings.values():
        try:
            moved = moved or manager.check_base(b.path, b.baseline_ref) != b.baseline_sha
        except ValueError:
            moved = True  # a ref that no longer resolves (or a repository no longer allowed) is not the baseline
        except (WorktreeError, OSError):
            raise ManifestRefused("candidate_unverifiable") from None  # git could not answer: refuse, record nothing
    if not moved:
        if store.baseline_moved(db, run_id):
            raise ManifestRefused("baseline_moved")
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        store.invalidate_run(db, run_id, "baseline_moved", actor)
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    raise ManifestRefused("baseline_moved")


# ---------------------------------------------------------------- sources (DB facts only)
def _attempt_of(db: sqlite3.Connection, task_id: str, attempt_no: int | None) -> _Source:
    if attempt_no is None:
        row = db.execute("SELECT id,attempt_no FROM task_attempts WHERE task_id=? AND state=? "
                         "ORDER BY attempt_no DESC LIMIT 1", (task_id, ATTEMPT_DONE)).fetchone()
    else:
        row = db.execute("SELECT id,attempt_no FROM task_attempts WHERE task_id=? AND attempt_no=? AND state=?",
                         (task_id, attempt_no, ATTEMPT_DONE)).fetchone()
    if row is None:
        raise ManifestRefused("no_source_attempt")
    return _Source(row[0], row[1])


def _committed(db: sqlite3.Connection, attempt_id: str, binding: RepoBinding) -> str:
    """The candidate SHA the attempt's commit phase recorded for this write repository (DB facts, no git)."""
    tree = load_attempt_worktrees(db, attempt_id).get(binding.repo_id)
    if tree is None or tree.access != "write" or tree.baseline_sha != binding.baseline_sha:
        raise ManifestRefused("candidate_invalid", "worktree_mismatch")
    rows = db.execute("SELECT state,head_before,candidate_sha FROM attempt_repo_commits WHERE attempt_id=? AND "
                      "repo_id=? AND state<>'intent' ORDER BY seq", (attempt_id, binding.repo_id)).fetchall()
    if len(rows) != 1 or rows[0][0] not in ("done", "unchanged"):
        raise ManifestRefused("candidate_invalid", "commit_incomplete")
    state, head_before, candidate = rows[0]
    if head_before != binding.baseline_sha or candidate is None:
        raise ManifestRefused("candidate_invalid", "commit_incomplete")
    if state == "unchanged" and candidate != binding.baseline_sha:
        raise ManifestRefused("candidate_invalid", "commit_incomplete")
    return candidate


def _task_run(db: sqlite3.Connection, task_id: str) -> str | None:
    row = db.execute("SELECT run_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    return None if row is None else row[0]


# ---------------------------------------------------------------- git facts
def _git_facts(manager: WorktreeManager, b: RepoBinding, candidate: str, max_diff_bytes: int) -> tuple[bytes, int]:
    """(diff bytes, changed paths) for baseline..candidate after the full commit rule. Raises ManifestRefused."""
    try:
        if candidate == b.baseline_sha:
            return b"", 0
        if not manager.commit_is_ancestor(b.path, b.baseline_sha, candidate):
            raise ManifestRefused("candidate_invalid", "not_descendant")
        changed = manager.check_commit_range(b.path, b.baseline_sha, candidate, b.protected_paths)
        diff, truncated = manager.commit_range_diff(b.path, b.baseline_sha, candidate, max_bytes=max_diff_bytes)
    except ManifestRefused:
        raise
    except CommitRefused as refused:
        raise ManifestRefused("candidate_invalid", refused.reason) from None
    except (WorktreeError, OSError, ValueError):
        raise ManifestRefused("candidate_unverifiable") from None
    if truncated:
        raise ManifestRefused("candidate_invalid", "diff_too_large")
    if changed == 0 or not diff:
        raise ManifestRefused("candidate_invalid", "empty_change")
    return diff, changed


def _entry(b: RepoBinding, candidate: str, source: _Source, diff: bytes, changed: int) -> RepoEntry:
    assert b.writer_task_id is not None
    return RepoEntry(b.repo_id, "write", b.baseline_ref, b.baseline_sha, candidate_sha=candidate,
                     source=SourceAttempt(b.writer_task_id, source.attempt_no),
                     diff_sha256=hashlib.sha256(diff).hexdigest() if diff else EMPTY_DIFF_SHA256,
                     diff_bytes=len(diff), changed_paths=changed,
                     validations=passed_validations(b.repo_id, b.baseline_sha, candidate, changed))


def collect_manifest(db: sqlite3.Connection, manager: WorktreeManager, run_id: str, *,
                     sources: Mapping[str, int] | None = None,
                     max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES) -> tuple[ManifestV2, dict[str, bytes]]:
    """Build (do not store) the run's v2 manifest. ``sources`` optionally names ``{writer_task_id: attempt_no}``;
    by default each writer task's latest completed attempt. Returns the manifest and the diff bytes per changed
    repository. Raises ``ManifestRefused``."""
    bindings, digest = _bindings(db, run_id)
    writers = {b.writer_task_id for b in bindings.values() if b.writer_task_id is not None}
    if not writers:
        raise ManifestRefused("no_changes")
    if sources is not None and not set(sources) <= writers:
        raise ManifestRefused("not_a_writer")  # a source must be the writer task of a bound repository
    chosen: dict[str, _Source] = {}
    for task_id in sorted(writers):
        if _task_run(db, task_id) != run_id:
            raise ManifestRefused("candidate_invalid", "writer_mismatch")
        chosen[task_id] = _attempt_of(db, task_id, None if sources is None else sources.get(task_id))
    entries, diffs = [], {}
    for b in bindings.values():
        if b.writer_task_id is None:
            entries.append(RepoEntry(b.repo_id, "read", b.baseline_ref, b.baseline_sha))
            continue
        source = chosen[b.writer_task_id]
        candidate = _committed(db, source.attempt_id, b)
        diff, changed = _git_facts(manager, b, candidate, max_diff_bytes)
        entries.append(_entry(b, candidate, source, diff, changed))
        if diff:
            diffs[b.repo_id] = diff
    try:
        manifest = build_manifest_v2(run_id, digest, entries)
    except ManifestError as exc:
        raise ManifestRefused("no_changes" if str(exc).startswith("no_changes") else "manifest_invalid") from None
    return manifest, diffs


def verify_manifest_candidates(db: sqlite3.Connection, manager: WorktreeManager, manifest: ManifestV2, *,
                               max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES) -> None:
    """D3 enforcement point 2 plus the D5 no-widening rule, on a manifest built any way at all. Re-derives every
    fact from the immutable rows and git; raises ``ManifestRefused('candidate_invalid', ...)`` (or
    ``candidate_unverifiable`` when git cannot answer)."""
    if not isinstance(manifest, ManifestV2):
        raise ManifestRefused("manifest_invalid")
    bindings, digest = _bindings(db, manifest.run_id)
    if manifest.bindings_sha256 != digest:
        raise ManifestRefused("candidate_invalid", "bindings_mismatch")
    if [r.repo_id for r in manifest.repos] != sorted(bindings):
        raise ManifestRefused("candidate_invalid", "repo_set_mismatch")
    for entry in manifest.repos:
        b = bindings[entry.repo_id]
        if (entry.baseline_ref, entry.baseline_sha) != (b.baseline_ref, b.baseline_sha):
            raise ManifestRefused("candidate_invalid", "baseline_mismatch")
        if entry.access != ("read" if b.writer_task_id is None else "write"):
            raise ManifestRefused("candidate_invalid", "access_mismatch")
        if entry.access == "read":
            continue
        assert entry.source is not None and entry.candidate_sha is not None
        if entry.source.task_id != b.writer_task_id:
            raise ManifestRefused("candidate_invalid", "not_the_writer")
        try:
            source = _attempt_of(db, entry.source.task_id, entry.source.attempt_no)
        except ManifestRefused:
            raise ManifestRefused("candidate_invalid", "no_source_attempt") from None
        if _committed(db, source.attempt_id, b) != entry.candidate_sha:
            raise ManifestRefused("candidate_invalid", "candidate_mismatch")
        diff, changed = _git_facts(manager, b, entry.candidate_sha, max_diff_bytes)
        if _entry(b, entry.candidate_sha, source, diff, changed) != entry:
            raise ManifestRefused("candidate_invalid", "diff_mismatch")


# ---------------------------------------------------------------- service
class ManifestBuilder:
    def __init__(self, db: sqlite3.Connection, blobs: ArtifactBlobStore, manager: WorktreeManager, *,
                 principal: str = PRINCIPAL) -> None:
        self.db, self.blobs, self.manager, self.principal = db, blobs, manager, principal

    def build(self, run_id: str, *, sources: Mapping[str, int] | None = None) -> dict:
        """D2 check, collect, verify, publish, index. Idempotent: the same candidates give the same hash, artifact
        and row (``replayed``). Returns ids and hashes only."""
        check_baselines(self.db, self.manager, run_id, actor=self.principal)
        manifest, diffs = collect_manifest(self.db, self.manager, run_id, sources=sources)
        verify_manifest_candidates(self.db, self.manager, manifest)
        digest = manifest.manifest_sha256
        for entry in manifest.repos:
            if entry.repo_id in diffs:
                self._publish_diff(manifest, entry, diffs[entry.repo_id])
        published = publish_manifest_v2(self.db, self.blobs, manifest, self.principal, "manifest-v2:" + digest)
        # Close the build race: a baseline that moved while the candidates were verified and published must not
        # yield a fresh manifest row (the 0019 trigger only stops it once an earlier manifest recorded the move).
        check_baselines(self.db, self.manager, run_id, actor=self.principal)
        existing = store.load_manifest(self.db, digest)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            record = store.record_manifest(self.db, manifest_sha256=digest, run_id=run_id,
                                           artifact_id=published.response["artifact_id"],
                                           bindings_sha256=manifest.bindings_sha256)
            self.db.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            self.db.execute("ROLLBACK")
            if "baseline_moved" in str(exc):
                raise ManifestRefused("baseline_moved") from None
            raise
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return {"run_id": run_id, "manifest_sha256": digest, "artifact_id": record.artifact_id, "seq": record.seq,
                "invalidated": store.invalidations(self.db, digest), "replayed": existing is not None}

    def _publish_diff(self, manifest: ManifestV2, entry: RepoEntry, diff: bytes) -> None:
        # Scoped per run and repository: the same diff bytes in another run or repository is a different artifact
        # (its provenance differs), never an idempotency conflict.
        key = "diff-v2:%s:%s:%s" % (manifest.run_id, entry.repo_id, entry.diff_sha256)
        try:
            result = publish_artifact(
                self.db, self.blobs, chunks=[diff],
                artifact_id=_artifact_id(DIFF_PROVENANCE_KIND, manifest.run_id, key),
                media_type="text/x-diff",
                provenance={"kind": DIFF_PROVENANCE_KIND, "run_id": manifest.run_id, "repo_id": entry.repo_id,
                            "baseline_sha": entry.baseline_sha, "candidate_sha": entry.candidate_sha},
                principal=self.principal, idempotency_key=key)
        except Exception:
            raise ManifestRefused("artifact_publish_failed", "diff") from None  # fixed code, no store detail
        if result.response["sha256"] != entry.diff_sha256:
            raise ManifestRefused("candidate_invalid", "diff_mismatch")

    def load(self, manifest_sha256: str, *, run_id: str | None = None) -> ManifestV2:
        record = store.load_manifest(self.db, manifest_sha256) if isinstance(manifest_sha256, str) else None
        if record is None or (run_id is not None and record.run_id != run_id):
            raise ManifestRefused("manifest_not_found")
        try:
            return read_manifest_v2_artifact(self.db, self.blobs, record.artifact_id,
                                             expected_manifest_sha256=manifest_sha256, run_id=record.run_id)
        except Exception:
            raise ManifestRefused("candidate_invalid", "artifact_corrupt") from None

    def recheck(self, manifest_sha256: str, *, run_id: str | None = None) -> ManifestV2:
        """Before a deploy approval is requested, and (mandatory) in the deploy pre-check, D9 step 2: refuse an
        invalidated manifest, re-run D2 (records ``baseline_moved``) and ``verify_manifest_candidates`` (a refusal
        records ``candidate_invalid``; an unverifiable candidate is refused without a record). The approval decision
        does not call this; it refuses only on invalidations already recorded."""
        manifest = self.load(manifest_sha256, run_id=run_id)
        if store.invalidations(self.db, manifest_sha256):
            raise ManifestRefused("manifest_invalidated")
        check_baselines(self.db, self.manager, manifest.run_id, actor=self.principal)
        try:
            verify_manifest_candidates(self.db, self.manager, manifest)
        except ManifestRefused as refused:
            if refused.code == "candidate_invalid":
                self.invalidate(manifest_sha256, "candidate_invalid")
            raise
        return manifest

    def invalidate(self, manifest_sha256: str, reason: str, *, actor: str | None = None) -> bool:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            added = store.invalidate(self.db, manifest_sha256, reason, actor or self.principal)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return added


def request_manifest_approval(builder: ManifestBuilder, requester, *, manifest_sha256: str, operation: str,
                              target: str, policy_version: str, idempotency_key: str, correlation_id: str,
                              ttl: timedelta = timedelta(hours=1)):
    """Request (as the daemon requester) an approval bound to a live, re-verified manifest. The database trigger of
    migration 0019 refuses the insert anyway if the manifest is not a live manifest of the run."""
    manifest = builder.recheck(manifest_sha256)
    expires = (datetime.now(timezone.utc) + ttl).isoformat()
    return requester.request_approval(run_id=manifest.run_id, operation=operation, target=target,
                                      candidate_sha256=manifest_sha256, policy_version=policy_version,
                                      expires_at=expires, idempotency_key=idempotency_key,
                                      correlation_id=correlation_id)


__all__ = ["ManifestBuilder", "ManifestRefused", "check_baselines", "collect_manifest", "request_manifest_approval",
           "verify_manifest_candidates"]
