"""Context manifests: what the daemon assembled for one attempt, as an immutable hashed record.

ADR 017 D5 (record only). A manifest is built by the daemon from sources it owns: the exact
repository head, the task spec hash, requirement references, file ranges with content hashes,
artifacts and validation results. The agent never declares its own manifest, and nothing here
changes what an agent is given; it only records it, so cache keys (M5.4 second half) and
analytics can bind to an exact input set.

Encoding is canonical: entries are sorted, identical duplicates collapse, conflicting ones are
refused, and the hash is the SHA-256 of sorted-key compact JSON. ``repo_head=None`` or
``worktree_clean=None`` mean *unknown* and are recorded as such, never as a clean tree; a
consumer that needs certainty (the cache) must treat unknown as "do not reuse".
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3
from typing import Any

from synapse.domain.contracts import EventEnvelope, encode_payload, new_id
from synapse.persistence.idempotency import canonical_request_hash

SCHEMA_VERSION = 1
KINDS = frozenset({"spec", "requirement", "file_range", "artifact", "validation"})
MAX_ENTRIES = 1000
MAX_REF = 512
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class ManifestError(ValueError):
    """The manifest input is malformed or inconsistent."""


class ManifestConflict(RuntimeError):
    """A different manifest was already recorded for this attempt."""


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    kind: str
    ref: str
    sha256: str
    byte_size: int | None = None
    start: int | None = None  # 1-based inclusive line range for file_range entries
    end: int | None = None

    def as_json(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"kind": self.kind, "ref": self.ref, "sha256": self.sha256, "byte_size": self.byte_size}
        if self.kind == "file_range":
            entry["range"] = [self.start, self.end]
        return entry

    def _identity(self) -> tuple:
        return (self.kind, self.ref, self.start or 0, self.end or 0)


def _validate(entry: ManifestEntry) -> None:
    if entry.kind not in KINDS:
        raise ManifestError(f"unknown entry kind: {entry.kind!r}")
    if not isinstance(entry.ref, str) or not entry.ref.strip() or len(entry.ref) > MAX_REF or "\x00" in entry.ref:
        raise ManifestError("entry ref must be a nonempty string up to 512 characters")
    if not isinstance(entry.sha256, str) or not _SHA256.fullmatch(entry.sha256):
        raise ManifestError("entry sha256 must be 64 lowercase hex characters")
    if entry.byte_size is not None and (type(entry.byte_size) is not int or entry.byte_size < 0):
        raise ManifestError("entry byte_size must be a nonnegative integer or None")
    if entry.kind == "file_range":
        if (type(entry.start) is not int or type(entry.end) is not int or entry.start < 1 or entry.end < entry.start):
            raise ManifestError("file_range needs integer 1 <= start <= end")
    elif entry.start is not None or entry.end is not None:
        raise ManifestError("only file_range entries carry a range")


@dataclass(frozen=True, slots=True)
class ContextManifest:
    task_id: str
    repo_head: str | None
    worktree_clean: bool | None
    entries: tuple[ManifestEntry, ...]

    def document(self) -> dict[str, Any]:
        return {"v": SCHEMA_VERSION, "task_id": self.task_id, "repo_head": self.repo_head,
                "worktree_clean": self.worktree_clean, "entries": [e.as_json() for e in self.entries]}

    @property
    def sha256(self) -> str:
        return canonical_request_hash(self.document())


def build_manifest(task_id: str, *, repo_head: str | None, worktree_clean: bool | None,
                   entries: list[ManifestEntry] | tuple[ManifestEntry, ...]) -> ContextManifest:
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 256:
        raise ManifestError("task_id must be a nonempty string up to 256 characters")
    if repo_head is not None and not (isinstance(repo_head, str) and _GIT_SHA.fullmatch(repo_head)):
        raise ManifestError("repo_head must be a 40-character lowercase hex git SHA or None (unknown)")
    if worktree_clean is not None and type(worktree_clean) is not bool:
        raise ManifestError("worktree_clean must be a bool or None (unknown)")
    if len(entries) > MAX_ENTRIES:
        raise ManifestError(f"at most {MAX_ENTRIES} entries")
    unique: dict[tuple, ManifestEntry] = {}
    for entry in entries:
        if not isinstance(entry, ManifestEntry):
            raise ManifestError("entries must be ManifestEntry values")
        _validate(entry)
        seen = unique.setdefault(entry._identity(), entry)
        if seen != entry:
            raise ManifestError(f"conflicting entries for {entry.kind} {entry.ref!r}")
    ordered = tuple(unique[key] for key in sorted(unique))
    return ContextManifest(task_id, repo_head, worktree_clean, ordered)


def record_manifest(db: sqlite3.Connection, *, run_id: str, attempt_id: str, manifest: ContextManifest) -> bool:
    """Append the manifest and its event atomically; return False when the same manifest exists.

    Must run inside the caller's transaction. A different manifest for the same attempt raises
    ``ManifestConflict`` (manifests are immutable).
    """
    if not db.in_transaction:
        raise RuntimeError("record_manifest must run inside a transaction")
    digest = manifest.sha256
    existing = db.execute("SELECT manifest_sha256 FROM context_manifests WHERE task_attempt_id=?", (attempt_id,)).fetchone()
    if existing is not None:
        if existing[0] != digest:
            raise ManifestConflict("a different manifest is already recorded for this attempt")
        return False
    event = EventEnvelope.create(aggregate_type="task_attempt", aggregate_id=attempt_id,
        event_type="context.manifest_recorded",
        payload={"task_id": manifest.task_id, "manifest_sha256": digest, "entry_count": len(manifest.entries)})
    db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
               (event.id, event.aggregate_type, attempt_id, run_id, event.event_type, event.schema_version,
                encode_payload(event.payload), event.occurred_at))
    db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
    db.execute("INSERT INTO context_manifests(id,task_attempt_id,manifest_json,manifest_sha256,created_at) VALUES(?,?,?,?,?)",
               (new_id(), attempt_id, json.dumps(manifest.document(), sort_keys=True, separators=(",", ":"), allow_nan=False),
                digest, event.occurred_at))
    return True
