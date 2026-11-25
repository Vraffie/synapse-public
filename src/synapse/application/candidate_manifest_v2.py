"""Candidate manifest v2 (ADR 026 D8): one object binding every repository of a multi-repository run.

``{"v": 2, "run_id", "bindings_sha256", "repos": [...]}``. Every repository of the run set appears exactly once,
sorted by ``repo_id``. A read repository carries only its pinned baseline; a write repository also carries its
candidate commit, the attempt that produced it (``source``), the SHA-256 and size of the diff baseline..candidate,
the number of changed paths and exactly the two v1 validations (``paths_editable``, ``xml_well_formed``), both
``passed``. No timestamps, paths or prompts. ``manifest_sha256`` is the SHA-256 of the canonical bytes and is the
single value deploy approvals bind to.

This module is pure: it defines the data, its canonical form and strict parsing, plus the artifact publish and the
stored-bytes verifier (the existing artifact store). Reading git and the database is the daemon's
``manifest_builder``. v1 (``candidate_manifest``) is unchanged and stays verifiable; v1 and v2 never parse as each
other.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from synapse.application.candidate_manifest import (
    MAX_MANIFEST_BYTES, MAX_REPOS, MAX_VALIDATIONS_PER_REPO, MEDIA_TYPE, PROVENANCE_KIND, ManifestError,
    ValidationResult, _artifact_id, _constant, _keys, _object)
from synapse.domain.contracts import validate_id
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.infrastructure.worktrees import check_registry_id
from synapse.persistence.artifact_store import publish_artifact
from synapse.persistence.idempotency import CommandResult

MANIFEST_VERSION = 2
REQUIRED_VALIDATIONS = ("paths_editable", "xml_well_formed")  # v1 of the rule set: exactly these, both passed
EMPTY_DIFF_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_ATTEMPT_NO = 10_000
MAX_CHANGED_PATHS = 1000  # the commit rule's cap (worktrees.MAX_COMMIT_PATHS)
_MAX_DIFF_INT = 2**40
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REF = re.compile(r"refs/heads/[A-Za-z0-9._/-]{1,240}")

_READ_KEYS = {"access", "baseline_ref", "baseline_sha", "repo_id"}
_WRITE_KEYS = _READ_KEYS | {"candidate_sha", "changed_paths", "diff_bytes", "diff_sha256", "source", "validations"}


def _commit(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise ManifestError(f"{what} must be a full lowercase hex commit id")
    return value


def _count(value: Any, what: str, limit: int) -> int:
    if type(value) is not int or not 0 <= value <= limit:
        raise ManifestError(f"{what} must be an integer from 0 to {limit}")
    return value


def evidence_sha256(name: str, repo_id: str, baseline_sha: str, candidate_sha: str, changed_paths: int) -> str:
    """The evidence a v2 validation result binds: what was checked, on which commits, over how many paths. Any
    verifier recomputes it from the manifest alone; there is no separate evidence artifact in this slice."""
    body = {"baseline_sha": baseline_sha, "candidate_sha": candidate_sha, "changed_paths": changed_paths,
            "check": name, "outcome": "passed", "repo_id": repo_id, "rules": "adr-026-d3-v1"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def passed_validations(repo_id: str, baseline_sha: str, candidate_sha: str,
                       changed_paths: int) -> tuple[ValidationResult, ...]:
    return tuple(ValidationResult(n, "passed", evidence_sha256(n, repo_id, baseline_sha, candidate_sha, changed_paths))
                 for n in REQUIRED_VALIDATIONS)


@dataclass(frozen=True, slots=True)
class SourceAttempt:
    task_id: str
    attempt_no: int

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str):
            raise ManifestError("source task_id must be a string")
        try:
            validate_id(self.task_id)
        except ValueError:
            raise ManifestError("source task_id is not a valid id") from None
        if type(self.attempt_no) is not int or not 1 <= self.attempt_no <= MAX_ATTEMPT_NO:
            raise ManifestError("source attempt_no is out of range")

    def to_dict(self) -> dict[str, Any]:
        return {"attempt_no": self.attempt_no, "task_id": self.task_id}


@dataclass(frozen=True, slots=True)
class RepoEntry:
    repo_id: str
    access: str  # "write" | "read"
    baseline_ref: str
    baseline_sha: str
    candidate_sha: str | None = None
    source: SourceAttempt | None = None
    diff_sha256: str | None = None
    diff_bytes: int | None = None
    changed_paths: int | None = None
    validations: tuple[ValidationResult, ...] = ()

    def __post_init__(self) -> None:
        try:
            check_registry_id(self.repo_id)
        except ValueError:
            raise ManifestError("repo_id is not a valid registry id") from None
        if not isinstance(self.baseline_ref, str) or not _REF.fullmatch(self.baseline_ref) or ".." in self.baseline_ref:
            raise ManifestError("baseline_ref must be a branch ref under refs/heads/")
        _commit(self.baseline_sha, "baseline_sha")
        if self.access == "read":
            if (self.candidate_sha, self.source, self.diff_sha256, self.diff_bytes, self.changed_paths) != (None,) * 5 \
                    or self.validations != ():
                raise ManifestError("a read repository carries only its baseline")
            return
        if self.access != "write":
            raise ManifestError("access must be write or read")
        _commit(self.candidate_sha, "candidate_sha")
        if not isinstance(self.source, SourceAttempt):
            raise ManifestError("a write repository names its source attempt")
        if not isinstance(self.diff_sha256, str) or not _SHA256.fullmatch(self.diff_sha256):
            raise ManifestError("diff_sha256 must be a full lowercase SHA-256")
        _count(self.diff_bytes, "diff_bytes", _MAX_DIFF_INT)
        _count(self.changed_paths, "changed_paths", MAX_CHANGED_PATHS)
        if self.candidate_sha == self.baseline_sha:
            if self.diff_sha256 != EMPTY_DIFF_SHA256 or self.diff_bytes != 0 or self.changed_paths != 0:
                raise ManifestError("an unchanged repository has an empty diff and no changed paths")
        elif self.changed_paths == 0 or self.diff_bytes == 0:
            raise ManifestError("a changed repository has a non-empty diff")
        if not isinstance(self.validations, tuple) or len(self.validations) > MAX_VALIDATIONS_PER_REPO \
                or any(not isinstance(v, ValidationResult) for v in self.validations):
            raise ManifestError("validations must be a tuple of ValidationResult values")
        expected = passed_validations(self.repo_id, self.baseline_sha, self.candidate_sha, self.changed_paths)
        if self.validations != expected:
            raise ManifestError("v2 validations are exactly paths_editable and xml_well_formed, passed, "
                                "with their evidence digests")

    @property
    def changed(self) -> bool:
        return self.access == "write" and self.candidate_sha != self.baseline_sha

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"access": self.access, "baseline_ref": self.baseline_ref,
                               "baseline_sha": self.baseline_sha, "repo_id": self.repo_id}
        if self.access == "write":
            assert self.source is not None
            out.update({"candidate_sha": self.candidate_sha, "changed_paths": self.changed_paths,
                        "diff_bytes": self.diff_bytes, "diff_sha256": self.diff_sha256,
                        "source": self.source.to_dict(), "validations": [v.to_dict() for v in self.validations]})
        return out


@dataclass(frozen=True, slots=True)
class ManifestV2:
    run_id: str
    bindings_sha256: str
    repos: tuple[RepoEntry, ...]
    version: int = MANIFEST_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise ManifestError("run_id must be a string")
        try:
            validate_id(self.run_id)
        except ValueError:
            raise ManifestError("run_id is not a valid id") from None
        if type(self.version) is not int or self.version != MANIFEST_VERSION:
            raise ManifestError("unsupported manifest version")
        if not isinstance(self.bindings_sha256, str) or not _SHA256.fullmatch(self.bindings_sha256):
            raise ManifestError("bindings_sha256 must be a full lowercase SHA-256")
        if not isinstance(self.repos, tuple) or not 1 <= len(self.repos) <= MAX_REPOS \
                or any(not isinstance(r, RepoEntry) for r in self.repos):
            raise ManifestError(f"a manifest holds 1 to {MAX_REPOS} repository entries")
        ids = [r.repo_id for r in self.repos]
        if ids != sorted(ids) or len(set(ids)) != len(ids):
            raise ManifestError("repositories must be sorted by repo_id with unique ids")
        writes = [r for r in self.repos if r.access == "write"]
        if not any(r.changed for r in writes):
            raise ManifestError("no_changes: at least one write repository must differ from its baseline")
        # One attempt per writer task: the repositories one task writes all come from the same attempt.
        per_task: dict[str, int] = {}
        for r in writes:
            assert r.source is not None
            if per_task.setdefault(r.source.task_id, r.source.attempt_no) != r.source.attempt_no:
                raise ManifestError("the repositories of one writer task must come from one attempt")
        if len(self.canonical_bytes()) > MAX_MANIFEST_BYTES:
            raise ManifestError("manifest is too large")

    def to_dict(self) -> dict[str, Any]:
        return {"bindings_sha256": self.bindings_sha256, "repos": [r.to_dict() for r in self.repos],
                "run_id": self.run_id, "v": self.version}

    def canonical_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("utf-8")

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def build_manifest_v2(run_id: str, bindings_sha256: str, repos: Iterable[RepoEntry]) -> ManifestV2:
    """Pure: sort by repo_id and validate."""
    ordered = tuple(sorted(repos, key=lambda r: r.repo_id if isinstance(r, RepoEntry) else ""))
    return ManifestV2(run_id=run_id, bindings_sha256=bindings_sha256, repos=ordered)


# ---------------------------------------------------------------- strict parsing
def manifest_v2_from_dict(data: Any) -> ManifestV2:
    """Strict: exact keys per access mode, exact types, no unknown or missing fields."""
    top = _keys(data, {"bindings_sha256", "repos", "run_id", "v"}, "manifest")
    if type(top["v"]) is not int or top["v"] != MANIFEST_VERSION:
        raise ManifestError("unsupported manifest version")
    if not isinstance(top["repos"], list) or len(top["repos"]) > MAX_REPOS:
        raise ManifestError("repos must be a list of at most %d entries" % MAX_REPOS)
    repos = []
    for raw in top["repos"]:
        access = raw.get("access") if isinstance(raw, dict) else None
        if access == "read":
            r = _keys(raw, _READ_KEYS, "read repo entry")
            repos.append(RepoEntry(r["repo_id"], "read", r["baseline_ref"], r["baseline_sha"]))
            continue
        r = _keys(raw, _WRITE_KEYS, "write repo entry")
        src = _keys(r["source"], {"attempt_no", "task_id"}, "source")
        if not isinstance(r["validations"], list) or len(r["validations"]) > MAX_VALIDATIONS_PER_REPO:
            raise ManifestError("validations must be a list")
        validations = tuple(ValidationResult(**_keys(v, {"evidence_sha256", "name", "outcome"}, "validation"))
                            for v in r["validations"])
        repos.append(RepoEntry(r["repo_id"], r["access"], r["baseline_ref"], r["baseline_sha"],
                               candidate_sha=r["candidate_sha"], source=SourceAttempt(src["task_id"], src["attempt_no"]),
                               diff_sha256=r["diff_sha256"], diff_bytes=r["diff_bytes"],
                               changed_paths=r["changed_paths"], validations=validations))
    return ManifestV2(run_id=top["run_id"], bindings_sha256=top["bindings_sha256"], repos=tuple(repos))


def manifest_v2_from_bytes(data: bytes) -> ManifestV2:
    """Parse and require the bytes to be exactly the canonical serialization."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest bytes are missing or too large")
    try:
        parsed = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ManifestError):
            raise
        raise ManifestError("manifest is not valid UTF-8 JSON") from None
    manifest = manifest_v2_from_dict(parsed)
    if manifest.canonical_bytes() != bytes(data):
        raise ManifestError("manifest is not in canonical form")
    return manifest


# ---------------------------------------------------------------- publishing and stored-bytes verification
def publish_manifest_v2(db: sqlite3.Connection, blobs: ArtifactBlobStore, manifest: ManifestV2, principal: str,
                        idempotency_key: str) -> CommandResult:
    """Publish the canonical bytes as an immutable artifact (provenance ``v: 2``). Idempotent per key."""
    if not isinstance(manifest, ManifestV2):
        raise ManifestError("not a v2 manifest")
    digest = manifest.manifest_sha256
    result = publish_artifact(
        db, blobs, chunks=[manifest.canonical_bytes()],
        artifact_id=_artifact_id(PROVENANCE_KIND, manifest.run_id, idempotency_key), media_type=MEDIA_TYPE,
        provenance={"kind": PROVENANCE_KIND, "run_id": manifest.run_id, "manifest_sha256": digest,
                    "v": MANIFEST_VERSION},
        principal=principal, idempotency_key=idempotency_key)
    if result.response.get("sha256") != digest:
        raise ManifestError("the stored manifest does not match its digest")
    return CommandResult({**result.response, "manifest_sha256": digest}, result.replayed)


def read_manifest_v2_artifact(db: sqlite3.Connection, blobs: ArtifactBlobStore, artifact_id: str, *,
                              expected_manifest_sha256: str | None = None, run_id: str | None = None) -> ManifestV2:
    """Re-read stored bytes and prove they are the v2 manifest that was bound to: artifact row and provenance say a
    v2 candidate manifest; the blob matches its content address; the bytes parse strictly and are canonical; the
    hash equals the provenance hash, the content address and, when given, ``expected_manifest_sha256`` and
    ``run_id``. Says nothing about git or the database; ``manifest_builder.verify_manifest_candidates`` does."""
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
        raise ManifestError("artifact is not a v2 candidate manifest")
    manifest = manifest_v2_from_bytes(blobs.read(sha256))
    digest = manifest.manifest_sha256
    if provenance.get("manifest_sha256") != digest or digest != sha256:
        raise ManifestError("manifest hash does not match the artifact record")
    if provenance.get("run_id") != manifest.run_id or (run_id is not None and manifest.run_id != run_id):
        raise ManifestError("manifest belongs to a different run")
    if expected_manifest_sha256 is not None and expected_manifest_sha256 != digest:
        raise ManifestError("manifest hash differs from the hash that was bound to")
    return manifest


__all__ = [
    "EMPTY_DIFF_SHA256", "MANIFEST_VERSION", "REQUIRED_VALIDATIONS", "ManifestError", "ManifestV2", "RepoEntry",
    "SourceAttempt", "build_manifest_v2", "evidence_sha256", "manifest_v2_from_bytes", "manifest_v2_from_dict",
    "passed_validations", "publish_manifest_v2", "read_manifest_v2_artifact",
]
