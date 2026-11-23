"""Multi-repository candidate manifest (ARCHITECTURE.md section 10; invariants 5 and 9).

A manifest names, for every repository a run changed: the repository id, the baseline commit, the
candidate commit, the SHA-256 of the diff between them and the validation results. Its own SHA-256
(``manifest_sha256``) is the single value that approvals, reviews and deployments bind to: change any
field and the hash changes, so an approval for one candidate can never be replayed for another.

This module is pure apart from two clearly separated helpers: ``collect_repo_candidate`` (read-only
git through ``WorktreeManager``) and ``publish_manifest`` / ``verify_manifest_artifact`` (the existing
artifact store). It performs no push, merge, deployment or integration check.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from synapse.domain.contracts import validate_id
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore, ArtifactIntegrityError
from synapse.infrastructure.worktrees import WorktreeError, WorktreeManager
from synapse.persistence.artifact_store import publish_artifact
from synapse.persistence.idempotency import CommandResult

MANIFEST_VERSION = 1
MEDIA_TYPE = "application/json"
PROVENANCE_KIND = "candidate_manifest"
DIFF_PROVENANCE_KIND = "candidate_diff"
MAX_REPOS = 16
MAX_VALIDATIONS_PER_REPO = 32
MAX_MANIFEST_BYTES = 48 * 1024  # leaves headroom under the 64 KiB blob store the agent runtime uses
DEFAULT_MAX_DIFF_BYTES = 60_000  # ditto: a diff that must be stored as an artifact stays under 64 KiB
OUTCOMES = frozenset({"passed", "failed", "skipped"})
_SHA1_OR_256 = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_MAX_DIFF_INT = 2**40


class ManifestError(ValueError):
    """The manifest or the candidate it describes is invalid, unsafe or does not verify."""


def _name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ManifestError(f"{what} must be 1-64 characters of [A-Za-z0-9._-] starting alphanumeric")
    return value


def _sha(value: Any, what: str, pattern: re.Pattern[str] = _SHA1_OR_256) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ManifestError(f"{what} must be a full lowercase hex digest")
    return value


@dataclass(frozen=True, slots=True)
class ValidationResult:
    name: str
    outcome: str  # passed | failed | skipped
    evidence_sha256: str | None = None  # SHA-256 of an evidence artifact, when there is one

    def __post_init__(self) -> None:
        _name(self.name, "validation name")
        if self.outcome not in OUTCOMES:
            raise ManifestError("validation outcome must be passed, failed or skipped")
        if self.evidence_sha256 is not None:
            _sha(self.evidence_sha256, "evidence_sha256", _SHA256)

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_sha256": self.evidence_sha256, "name": self.name, "outcome": self.outcome}


@dataclass(frozen=True, slots=True)
class RepoCandidate:
    repo_id: str
    baseline_sha: str
    candidate_sha: str
    diff_sha256: str  # SHA-256 of the UTF-8 text of `git diff baseline candidate` (see docs for the decoding rule)
    diff_bytes: int
    validations: tuple[ValidationResult, ...] = ()

    def __post_init__(self) -> None:
        _name(self.repo_id, "repo_id")
        _sha(self.baseline_sha, "baseline_sha")
        _sha(self.candidate_sha, "candidate_sha")
        _sha(self.diff_sha256, "diff_sha256", _SHA256)
        if type(self.diff_bytes) is not int or not 0 <= self.diff_bytes <= _MAX_DIFF_INT:
            raise ManifestError("diff_bytes must be a non-negative integer")
        if not isinstance(self.validations, tuple) or len(self.validations) > MAX_VALIDATIONS_PER_REPO:
            raise ManifestError(f"validations must be a tuple of at most {MAX_VALIDATIONS_PER_REPO} results")
        if any(not isinstance(v, ValidationResult) for v in self.validations):
            raise ManifestError("validations must be ValidationResult values")
        names = [v.name for v in self.validations]
        if names != sorted(names) or len(set(names)) != len(names):
            raise ManifestError("validations must be sorted by name with unique names")

    def to_dict(self) -> dict[str, Any]:
        return {"baseline_sha": self.baseline_sha, "candidate_sha": self.candidate_sha, "diff_bytes": self.diff_bytes,
                "diff_sha256": self.diff_sha256, "repo_id": self.repo_id,
                "validations": [v.to_dict() for v in self.validations]}


@dataclass(frozen=True, slots=True)
class CandidateManifest:
    run_id: str
    repos: tuple[RepoCandidate, ...]
    version: int = MANIFEST_VERSION

    def __post_init__(self) -> None:
        validate_id(self.run_id)
        if type(self.version) is not int or self.version != MANIFEST_VERSION:
            raise ManifestError("unsupported manifest version")
        if not isinstance(self.repos, tuple) or not 1 <= len(self.repos) <= MAX_REPOS:
            raise ManifestError(f"a manifest holds 1 to {MAX_REPOS} repositories")
        if any(not isinstance(r, RepoCandidate) for r in self.repos):
            raise ManifestError("repos must be RepoCandidate values")
        ids = [r.repo_id for r in self.repos]
        if ids != sorted(ids) or len(set(ids)) != len(ids):
            raise ManifestError("repositories must be sorted by repo_id with unique ids")
        if len(self.canonical_bytes()) > MAX_MANIFEST_BYTES:
            raise ManifestError("manifest is too large")

    def to_dict(self) -> dict[str, Any]:
        return {"repos": [r.to_dict() for r in self.repos], "run_id": self.run_id, "v": self.version}

    def canonical_bytes(self) -> bytes:
        """Sorted keys, no whitespace, ASCII-only escapes: byte-for-byte reproducible."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("utf-8")

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


# ---------------------------------------------------------------- strict parsing
def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError("duplicate key in manifest")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise ManifestError("non-finite number in manifest")


def _keys(obj: Any, expected: set[str], what: str) -> dict[str, Any]:
    if not isinstance(obj, dict) or set(obj) != expected:
        raise ManifestError(f"{what} must be an object with exactly the keys {sorted(expected)}")
    return obj


def manifest_from_dict(data: Any) -> CandidateManifest:
    """Strict: exact keys, exact types, no unknown or missing fields."""
    top = _keys(data, {"repos", "run_id", "v"}, "manifest")
    if not isinstance(top["repos"], list):
        raise ManifestError("repos must be a list")
    repos = []
    for raw in top["repos"]:
        r = _keys(raw, {"baseline_sha", "candidate_sha", "diff_bytes", "diff_sha256", "repo_id", "validations"}, "repo entry")
        if not isinstance(r["validations"], list):
            raise ManifestError("validations must be a list")
        validations = tuple(
            ValidationResult(**_keys(v, {"evidence_sha256", "name", "outcome"}, "validation")) for v in r["validations"])
        repos.append(RepoCandidate(repo_id=r["repo_id"], baseline_sha=r["baseline_sha"], candidate_sha=r["candidate_sha"],
                                   diff_sha256=r["diff_sha256"], diff_bytes=r["diff_bytes"], validations=validations))
    if type(top["v"]) is not int:
        raise ManifestError("v must be an integer")
    if not isinstance(top["run_id"], str):
        raise ManifestError("run_id must be a string")
    return CandidateManifest(run_id=top["run_id"], repos=tuple(repos), version=top["v"])


def manifest_from_bytes(data: bytes) -> CandidateManifest:
    """Parse and require the bytes to be exactly the canonical serialization (no alternative encodings)."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest bytes are missing or too large")
    try:
        parsed = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ManifestError):
            raise
        raise ManifestError("manifest is not valid UTF-8 JSON") from None
    manifest = manifest_from_dict(parsed)
    if manifest.canonical_bytes() != bytes(data):
        raise ManifestError("manifest is not in canonical form")
    return manifest


def build_manifest(run_id: str, repos: Iterable[RepoCandidate]) -> CandidateManifest:
    """Pure: sort the repositories by id and validate. Facts are injected, nothing is read or run."""
    ordered = tuple(sorted(repos, key=lambda r: r.repo_id if isinstance(r, RepoCandidate) else ""))
    return CandidateManifest(run_id=run_id, repos=ordered)


# ---------------------------------------------------------------- collecting from a worktree
@dataclass(frozen=True, slots=True)
class CollectedCandidate:
    candidate: RepoCandidate
    diff: bytes  # exactly the bytes whose SHA-256 is candidate.diff_sha256 (publish with publish_diff)


def collect_repo_candidate(worktree_manager: WorktreeManager, run_id: str, task_id: str, repo: str, baseline_ref: str,
                           *, repo_id: str | None = None, validations: Iterable[ValidationResult] = (),
                           max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES) -> CollectedCandidate:
    """Read-only facts from a task's worktree.

    Refuses: a repository that is not the worktree's owner, a dirty tree (including untracked files), a
    candidate that does not descend from the baseline, a diff over ``max_diff_bytes`` (a truncated diff
    could not be bound to), and a tree that moved while it was being read. The candidate is the
    worktree's committed HEAD; uncommitted work is never part of a manifest.
    """
    if type(max_diff_bytes) is not int or not 1 <= max_diff_bytes <= 16 * 1024 * 1024:
        raise ManifestError("max_diff_bytes is out of range")
    rid = _name(repo_id if repo_id is not None else repo.rstrip("/").rsplit("/", 1)[-1], "repo_id")
    try:
        baseline_sha = worktree_manager.check_base(repo, baseline_ref)
        if worktree_manager.worktree_repo(run_id, task_id) != repo:
            raise ManifestError("the task's worktree does not belong to this repository")
        before = worktree_manager.status(run_id, task_id)
        if not before.clean:
            raise ManifestError("the worktree has uncommitted or untracked changes; commit or discard them first")
        candidate_sha = _sha(before.head, "candidate sha")
        if not worktree_manager.descends_from(run_id, task_id, baseline_sha):
            raise ManifestError("the candidate does not descend from the baseline")
        diff = worktree_manager.diff_since(run_id, task_id, baseline_sha, max_bytes=max_diff_bytes)
        if diff.truncated:
            raise ManifestError("the diff exceeds the size limit and cannot be bound to")
        after = worktree_manager.status(run_id, task_id)
    except (WorktreeError, ValueError) as exc:
        if isinstance(exc, ManifestError):
            raise
        raise ManifestError("the candidate could not be read from the worktree") from None
    if not after.clean or after.head != candidate_sha:
        raise ManifestError("the worktree changed while the candidate was being read")
    data = diff.text.encode("utf-8")
    validation_tuple = tuple(sorted(validations, key=lambda v: v.name if isinstance(v, ValidationResult) else ""))
    candidate = RepoCandidate(repo_id=rid, baseline_sha=baseline_sha, candidate_sha=candidate_sha,
                              diff_sha256=hashlib.sha256(data).hexdigest(), diff_bytes=len(data),
                              validations=validation_tuple)
    return CollectedCandidate(candidate, data)


# ---------------------------------------------------------------- publishing and verification
def _artifact_id(kind: str, run_id: str, key: str) -> str:
    # Deterministic per (kind, run, key): a retry with the same idempotency key replays instead of conflicting.
    return kind + "-" + hashlib.sha256(f"{kind}\x00{run_id}\x00{key}".encode()).hexdigest()[:40]


def publish_manifest(db: sqlite3.Connection, blobs: ArtifactBlobStore, run_id: str, manifest: CandidateManifest,
                     principal: str, idempotency_key: str) -> CommandResult:
    """Publish the canonical manifest bytes as an immutable artifact. Idempotent per ``idempotency_key``."""
    validate_id(run_id)
    if not isinstance(manifest, CandidateManifest) or manifest.run_id != run_id:
        raise ManifestError("the manifest belongs to a different run")
    digest = manifest.manifest_sha256
    result = publish_artifact(
        db, blobs, chunks=[manifest.canonical_bytes()], artifact_id=_artifact_id(PROVENANCE_KIND, run_id, idempotency_key),
        media_type=MEDIA_TYPE,
        provenance={"kind": PROVENANCE_KIND, "run_id": run_id, "manifest_sha256": digest, "v": manifest.version},
        principal=principal, idempotency_key=idempotency_key)
    return CommandResult({**result.response, "manifest_sha256": digest}, result.replayed)


def publish_diff(db: sqlite3.Connection, blobs: ArtifactBlobStore, run_id: str, collected: CollectedCandidate,
                 principal: str, idempotency_key: str) -> CommandResult:
    """Publish a collected diff; the blob's content address equals ``candidate.diff_sha256``."""
    validate_id(run_id)
    if hashlib.sha256(collected.diff).hexdigest() != collected.candidate.diff_sha256 \
            or len(collected.diff) != collected.candidate.diff_bytes:
        raise ManifestError("the diff does not match the candidate that describes it")
    result = publish_artifact(
        db, blobs, chunks=[collected.diff],
        artifact_id=_artifact_id(DIFF_PROVENANCE_KIND, run_id, idempotency_key), media_type="text/x-diff",
        provenance={"kind": DIFF_PROVENANCE_KIND, "run_id": run_id, "repo_id": collected.candidate.repo_id,
                    "baseline_sha": collected.candidate.baseline_sha, "candidate_sha": collected.candidate.candidate_sha},
        principal=principal, idempotency_key=idempotency_key)
    if result.response["sha256"] != collected.candidate.diff_sha256:
        raise ManifestError("the stored diff does not match its recorded digest")
    return result


def verify_manifest_artifact(db: sqlite3.Connection, blobs: ArtifactBlobStore, artifact_id: str, *,
                             expected_manifest_sha256: str | None = None, run_id: str | None = None,
                             verify_diffs: bool = False) -> CandidateManifest:
    """Re-read the stored bytes and prove they are the manifest that was bound to.

    Checks, in order: the artifact row and its provenance say candidate_manifest; the blob matches its
    content address (the store verifies this on read); the bytes parse strictly and are canonical; the
    recomputed hash equals the provenance hash and, when given, ``expected_manifest_sha256`` (what an
    approval recorded) and ``run_id``. With ``verify_diffs`` every repository's diff blob must also exist
    and match its recorded SHA-256 and size. Raises ``ManifestError`` or ``ArtifactIntegrityError``.
    """
    validate_id(artifact_id)
    row = db.execute("SELECT sha256,media_type,provenance_json FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    if row is None:
        raise ManifestError("manifest artifact does not exist")
    sha256, media_type, provenance_json = row
    try:
        provenance = json.loads(provenance_json)
    except ValueError:
        raise ManifestError("manifest artifact provenance is unreadable") from None
    if (media_type != MEDIA_TYPE or not isinstance(provenance, dict) or provenance.get("kind") != PROVENANCE_KIND
            or provenance.get("v") != MANIFEST_VERSION):
        raise ManifestError("artifact is not a candidate manifest")
    data = blobs.read(sha256)  # raises ArtifactIntegrityError on any mismatch with the content address
    manifest = manifest_from_bytes(data)
    digest = manifest.manifest_sha256
    if provenance.get("manifest_sha256") != digest or digest != sha256:
        raise ManifestError("manifest hash does not match the artifact record")
    if provenance.get("run_id") != manifest.run_id or (run_id is not None and manifest.run_id != run_id):
        raise ManifestError("manifest belongs to a different run")
    if expected_manifest_sha256 is not None and expected_manifest_sha256 != digest:
        raise ManifestError("manifest hash differs from the hash that was bound to")
    if verify_diffs:
        for repo in manifest.repos:
            try:
                diff = blobs.read(repo.diff_sha256)
            except (OSError, ArtifactIntegrityError):
                raise ManifestError(f"diff artifact for {repo.repo_id} is missing or corrupt") from None
            if len(diff) != repo.diff_bytes:
                raise ManifestError(f"diff artifact for {repo.repo_id} has the wrong size")
    return manifest


__all__ = [
    "ArtifactIntegrityError", "CandidateManifest", "CollectedCandidate", "ManifestError", "RepoCandidate",
    "ValidationResult", "build_manifest", "collect_repo_candidate", "manifest_from_bytes", "manifest_from_dict",
    "publish_diff", "publish_manifest", "verify_manifest_artifact",
]
_ = math  # (math kept for explicit non-finite handling in future numeric fields)
