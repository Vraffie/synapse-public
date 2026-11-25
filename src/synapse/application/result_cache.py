"""Exact result cache: lookup/record logic against a storage port (M5.4 cache 2/3, ADR 017 D6).

Pure: no SQLite, no file or network I/O, no clock, no daemon. Storage is a ``CacheStore`` port; the
``InMemoryCacheStore`` here exists for tests. The SQL store over the ``result_cache`` table (ADR 017
D3, migration 0020) is ``synapse.persistence.result_cache_store``. Nothing in Synapse calls this
module at runtime today.

Rules:

- The key and every cacheability rule come from ``synapse.application.cache_key`` and nothing else.
  Anything it bypasses (edits, reviews, verifications, validations, approval-gated or write-capable
  work, dirty or unknown heads, unknown tool versions, malformed input) is a ``bypass`` here.
- A hit is a *reference* to a recorded result artifact (SHA-256 and size) plus the originating
  attempt. It is never a verification, approval, review or mandatory check (invariants 5, 9, 10):
  ``CacheDecision`` carries those as class constants that are always ``False``, and the caller must
  still run every mandatory check on the reused output.
- Fail to a miss: a store that raises or returns garbage, or an entry that is not exactly a
  ``CacheEntry``, is malformed, oversized, for another key, invalidated, whose material does not hash
  to the key, or whose entry digest does not match its fields, is a ``miss`` with a typed reason.
  ``lookup`` and ``record`` never raise into the caller (``BaseException`` such as
  ``KeyboardInterrupt`` still propagates).
- Each entry field is read exactly once; only those values reach the verifier and the decision.
- When a verifier callback is given, it reports the referenced artifact's current SHA-256 (and size,
  if known); any disagreement with the entry is a miss.
- ``put`` is first-writer-wins: an existing entry is never overwritten. The only change an entry may
  ever get is the one-way invalidation marker (ADR 017 D3), which a lookup treats as a miss.
- ``record`` should be given the key the attempt was looked up under (``expected_key_sha256``); if
  the request now derives a different key, nothing is stored.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
import json
import re
import threading
from typing import Any, ClassVar, Protocol, runtime_checkable

from synapse.application.cache_key import (
    CacheBypass, CacheHit, CacheKey, CacheKeyInputs, derive_cache_key, may_serve, store_decision,
)
from synapse.persistence.idempotency import canonical_request_hash

ENTRY_SCHEMA_VERSION = 1
MAX_RESULT_BYTES = 64 * 1024 * 1024  # largest result artifact an entry may reference
MAX_MATERIAL_BYTES = 16 * 1024  # key material JSON (hashes, ids, versions only)
MAX_TEXT = 256
# Bound on entry_json(): material (16 KiB) + two text fields of 256 chars at <= 12 bytes each when
# ASCII-escaped + three hashes + size + field names stays below this. It is the SQL CHECK value.
MAX_ENTRY_BYTES = 24 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MATERIAL_FIELDS = frozenset({
    "cache_schema_version", "policy_version", "approval_policy_sha256", "provider", "profile_id",
    "cli_version", "model", "allowed_tools", "permission_mode", "settings_sha256", "spec_sha256",
    "manifest_sha256", "repo_head", "worktree_clean", "task_class", "validator_sha256s",
})


class CacheOutcome(StrEnum):
    HIT = "hit"
    MISS = "miss"
    BYPASS = "bypass"  # not cacheable: the cache_key reason is passed through


class MissReason(StrEnum):
    NOT_FOUND = "not_found"
    STORE_ERROR = "store_error"
    MALFORMED_ENTRY = "malformed_entry"
    ENTRY_TOO_LARGE = "entry_too_large"
    KEY_MISMATCH = "key_mismatch"
    MATERIAL_MISMATCH = "material_mismatch"
    ENTRY_DIGEST_MISMATCH = "entry_digest_mismatch"
    INVALIDATED = "invalidated"
    ARTIFACT_MISSING = "artifact_missing"
    CORRUPT_ENTRY = "corrupt_entry"  # the artifact's current hash is not the recorded one
    SIZE_MISMATCH = "size_mismatch"  # the artifact's current size is not the recorded one
    VERIFIER_ERROR = "verifier_error"


class RecordStatus(StrEnum):
    STORED = "stored"
    ALREADY_PRESENT = "already_present"  # first writer won; nothing was overwritten
    REFUSED = "refused"  # not cacheable, not storable, key changed, or an invalid result
    STORE_ERROR = "store_error"


def entry_digest(*, key_sha256: str, result_sha256: str, result_size: int, origin_attempt_id: str,
                 created_marker: str, entry_schema_version: int = ENTRY_SCHEMA_VERSION) -> str:
    """SHA-256 over the canonical JSON of the fields that make up the result reference.

    It detects an accidental or partial rewrite of those fields. It does not stop anyone who can
    write the store and recompute it; the append-only storage triggers are the real protection.
    """
    return canonical_request_hash({
        "entry_schema_version": entry_schema_version, "key_sha256": key_sha256,
        "result_sha256": result_sha256, "result_size": result_size,
        "origin_attempt_id": origin_attempt_id, "created_marker": created_marker,
    })


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One stored result reference. ``key_material_json`` is the canonical JSON of the key's material."""

    key_sha256: str
    result_sha256: str
    result_size: int
    origin_attempt_id: str
    created_marker: str  # opaque, caller-supplied (an event sequence or timestamp); this module has no clock
    key_material_json: str
    entry_digest: str
    entry_schema_version: int = ENTRY_SCHEMA_VERSION
    invalidated_marker: str | None = None  # one-way: once set it is never cleared; not part of entry_json

    def entry_json(self) -> str:
        """The ``entry_json`` a SQL store persists (see docs/operations/exact-cache-logic.md)."""
        return json.dumps({
            "entry_schema_version": self.entry_schema_version, "key_sha256": self.key_sha256,
            "result_sha256": self.result_sha256, "result_size": self.result_size,
            "origin_attempt_id": self.origin_attempt_id, "created_marker": self.created_marker,
            "entry_digest": self.entry_digest, "key_material": json.loads(self.key_material_json),
        }, sort_keys=True, separators=(",", ":"), allow_nan=False)


@runtime_checkable
class CacheStore(Protocol):
    """Storage port. ``put`` returns True if it stored the entry, False if the key already existed."""

    def get(self, key_sha256: str) -> CacheEntry | None: ...

    def put(self, key_sha256: str, entry: CacheEntry) -> bool: ...


class InMemoryCacheStore:
    """Deterministic, thread-safe, first-writer-wins store for tests. Never overwrites an entry.

    ``race_hook`` (tests only) runs between the existence check and the insert, to widen the window a
    lockless store would lose.
    """

    def __init__(self, *, race_hook: Callable[[], None] | None = None) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self._race_hook = race_hook

    def get(self, key_sha256: str) -> CacheEntry | None:
        with self._lock:
            return self._entries.get(key_sha256)

    def put(self, key_sha256: str, entry: CacheEntry) -> bool:
        with self._lock:
            if key_sha256 in self._entries:
                return False
            if self._race_hook is not None:
                self._race_hook()
            self._entries[key_sha256] = entry
            return True

    def invalidate(self, key_sha256: str, marker: str) -> bool:
        """The one permitted change: set the invalidation marker once. False if absent or already set."""
        with self._lock:
            entry = self._entries.get(key_sha256)
            if entry is None or entry.invalidated_marker is not None:
                return False
            self._entries[key_sha256] = replace(entry, invalidated_marker=marker)
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


@dataclass(frozen=True, slots=True)
class CacheDecision:
    """Result of ``lookup``. A hit is a reused artifact reference, never evidence and never authority."""

    outcome: CacheOutcome
    reason: str | None = None
    key_sha256: str | None = None
    hit: CacheHit | None = None
    result_sha256: str | None = None
    result_size: int | None = None
    artifact_hash_checked: bool = False  # True only when a verifier confirmed the recorded hash

    # Class attributes, not fields: they cannot be passed to the constructor or set on an instance.
    counts_as_verification: ClassVar[bool] = False
    counts_as_review: ClassVar[bool] = False
    satisfies_approval: ClassVar[bool] = False
    counts_as_mandatory_check: ClassVar[bool] = False
    requires_verification: ClassVar[bool] = True

    @property
    def is_hit(self) -> bool:
        return self.outcome is CacheOutcome.HIT


@dataclass(frozen=True, slots=True)
class ArtifactFacts:
    """What a verifier observed about the referenced artifact now. ``size=None`` means not checked."""

    sha256: str
    size: int | None = None


Verifier = Callable[[str, int], "ArtifactFacts | None"]


@dataclass(frozen=True, slots=True)
class CacheResult:
    """What a finished attempt produced, as the daemon recorded it."""

    origin_attempt_id: str
    outcome: str
    budget_blocked: bool
    result_sha256: str
    result_size: int
    created_marker: str


@dataclass(frozen=True, slots=True)
class RecordOutcome:
    status: RecordStatus
    reason: str | None = None
    key_sha256: str | None = None


def _text(value: object) -> bool:
    return type(value) is str and bool(value.strip()) and len(value) <= MAX_TEXT and not _CONTROL.search(value)


def _sha(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _material_json(material: Any) -> str:
    return json.dumps(material, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _miss(reason: str, key: CacheKey | None = None) -> CacheDecision:
    return CacheDecision(CacheOutcome.MISS, reason, key.sha256 if key is not None else None)


def _check_entry(entry: object, key: CacheKey) -> tuple[MissReason | None, tuple[str, int, str]]:
    """Validate ``entry`` for ``key``, reading each field once. Returns (problem, (sha, size, attempt))."""
    none: tuple[str, int, str] = ("", 0, "")
    if type(entry) is not CacheEntry:  # exact type: a subclass could override fields with properties
        return MissReason.MALFORMED_ENTRY, none
    version = entry.entry_schema_version
    key_sha = entry.key_sha256
    result_sha = entry.result_sha256
    size = entry.result_size
    attempt = entry.origin_attempt_id
    marker = entry.created_marker
    material_json = entry.key_material_json
    digest = entry.entry_digest
    invalidated = entry.invalidated_marker

    if type(version) is not int or version != ENTRY_SCHEMA_VERSION:
        return MissReason.MALFORMED_ENTRY, none
    if type(key_sha) is not str or key_sha != key.sha256:
        return MissReason.KEY_MISMATCH, none
    if not (_sha(result_sha) and _text(attempt) and _text(marker) and _sha(digest)):
        return MissReason.MALFORMED_ENTRY, none
    if type(size) is not int or size < 0:
        return MissReason.MALFORMED_ENTRY, none
    if size > MAX_RESULT_BYTES:
        return MissReason.ENTRY_TOO_LARGE, none
    if type(material_json) is not str:
        return MissReason.MALFORMED_ENTRY, none
    if len(material_json.encode("utf-8")) > MAX_MATERIAL_BYTES:
        return MissReason.ENTRY_TOO_LARGE, none
    if invalidated is not None:
        return MissReason.INVALIDATED, none
    material = json.loads(material_json)
    if type(material) is not dict or set(material) != _MATERIAL_FIELDS:
        return MissReason.MALFORMED_ENTRY, none
    # the stored components must hash to the key and equal the current request's components exactly
    if canonical_request_hash(material) != key.sha256 or _material_json(material) != _material_json(key.material):
        return MissReason.MATERIAL_MISMATCH, none
    expected = entry_digest(key_sha256=key_sha, result_sha256=result_sha, result_size=size,
                            origin_attempt_id=attempt, created_marker=marker, entry_schema_version=version)
    if digest != expected:
        return MissReason.ENTRY_DIGEST_MISMATCH, none
    return None, (result_sha, size, attempt)


def lookup(store: CacheStore, request: CacheKeyInputs, *, verifier: Verifier | None = None) -> CacheDecision:
    """Decide hit, miss or bypass for ``request``. Never raises; any doubt is a miss or a bypass."""
    try:
        decision = derive_cache_key(request)
    except Exception:  # derive_cache_key never raises; belt and braces
        return CacheDecision(CacheOutcome.BYPASS, "invalid_input")
    if isinstance(decision, CacheBypass):
        return CacheDecision(CacheOutcome.BYPASS, decision.reason if type(decision.reason) is str else "invalid_input")
    if not isinstance(decision, CacheKey):
        return CacheDecision(CacheOutcome.BYPASS, "invalid_input")
    key = decision
    try:
        entry = store.get(key.sha256)
    except Exception:
        return _miss(MissReason.STORE_ERROR, key)
    if entry is None:
        return _miss(MissReason.NOT_FOUND, key)
    try:
        problem, (result_sha, size, attempt) = _check_entry(entry, key)
    except Exception:  # undecodable JSON, deep nesting, non-serialisable values: never served
        problem, (result_sha, size, attempt) = MissReason.MALFORMED_ENTRY, ("", 0, "")
    if problem is not None:
        return _miss(problem, key)

    checked = False
    if verifier is not None:
        try:
            facts = verifier(result_sha, size)
            if facts is None:
                return _miss(MissReason.ARTIFACT_MISSING, key)
            if type(facts) is not ArtifactFacts:
                return _miss(MissReason.VERIFIER_ERROR, key)
            seen_sha, seen_size = facts.sha256, facts.size
            if type(seen_sha) is not str or seen_sha != result_sha:
                return _miss(MissReason.CORRUPT_ENTRY, key)
            if seen_size is not None and (type(seen_size) is not int or seen_size != size):
                return _miss(MissReason.SIZE_MISMATCH, key)
        except Exception:
            return _miss(MissReason.VERIFIER_ERROR, key)
        checked = True

    hit = CacheHit(key.sha256, attempt)
    if not may_serve(key, hit):
        return _miss(MissReason.MALFORMED_ENTRY, key)
    return CacheDecision(CacheOutcome.HIT, None, key.sha256, hit, result_sha, size, checked)


def _result_problem(result: object) -> str | None:
    if type(result) is not CacheResult:
        return "invalid_result"
    if not _text(result.origin_attempt_id):
        return "invalid_result:origin_attempt_id"
    if type(result.outcome) is not str:
        return "invalid_result:outcome"
    if type(result.budget_blocked) is not bool:
        return "invalid_result:budget_blocked"
    if not _sha(result.result_sha256):
        return "invalid_result:result_sha256"
    if type(result.result_size) is not int or result.result_size < 0:
        return "invalid_result:result_size"
    if result.result_size > MAX_RESULT_BYTES:
        return "result_too_large"
    if not _text(result.created_marker):
        return "invalid_result:created_marker"
    return None


def record(store: CacheStore, request: CacheKeyInputs, result: CacheResult, *,
           expected_key_sha256: str) -> RecordOutcome:
    """Store ``result`` under ``request``'s key if allowed; first writer wins. Never raises.

    ``expected_key_sha256`` is the key from the ``lookup`` that preceded this attempt. The daemon
    must pass it (it is a required keyword; omitting it is a TypeError, never a silent skip): if the request now derives a different key (the head, spec, policy ... moved
    while the attempt ran), the result is refused with ``key_changed`` instead of being filed under
    inputs it was not produced from.
    """
    try:
        decision = derive_cache_key(request)
        if isinstance(decision, CacheBypass):
            return RecordOutcome(RecordStatus.REFUSED, decision.reason)
        if not isinstance(decision, CacheKey):
            return RecordOutcome(RecordStatus.REFUSED, "invalid_input")
        if not _sha(expected_key_sha256):
            return RecordOutcome(RecordStatus.REFUSED, "invalid_input:expected_key_sha256", decision.sha256)
        if expected_key_sha256 != decision.sha256:
            return RecordOutcome(RecordStatus.REFUSED, "key_changed", decision.sha256)
        problem = _result_problem(result)
        if problem is not None:
            return RecordOutcome(RecordStatus.REFUSED, problem, decision.sha256)
        refused = store_decision(decision, outcome=result.outcome, budget_blocked=result.budget_blocked)
        if refused is not None:
            return RecordOutcome(RecordStatus.REFUSED, refused.reason, decision.sha256)
        material = _material_json(decision.material)
        if len(material.encode("utf-8")) > MAX_MATERIAL_BYTES:
            return RecordOutcome(RecordStatus.REFUSED, "entry_too_large", decision.sha256)
        digest = entry_digest(key_sha256=decision.sha256, result_sha256=result.result_sha256,
                              result_size=result.result_size, origin_attempt_id=result.origin_attempt_id,
                              created_marker=result.created_marker)
        entry = CacheEntry(decision.sha256, result.result_sha256, result.result_size, result.origin_attempt_id,
                           result.created_marker, material, digest)
        if len(entry.entry_json().encode("utf-8")) > MAX_ENTRY_BYTES:  # what the SQL CHECK would refuse
            return RecordOutcome(RecordStatus.REFUSED, "entry_too_large", decision.sha256)
        entry_problem, _ = _check_entry(entry, decision)
        if entry_problem is not None:
            return RecordOutcome(RecordStatus.REFUSED, f"invalid_entry:{entry_problem}", decision.sha256)
    except Exception:
        return RecordOutcome(RecordStatus.REFUSED, "invalid_input")
    try:
        stored = store.put(decision.sha256, entry)
    except Exception:
        return RecordOutcome(RecordStatus.STORE_ERROR, MissReason.STORE_ERROR, decision.sha256)
    if stored is True:
        return RecordOutcome(RecordStatus.STORED, None, decision.sha256)
    if stored is False:
        return RecordOutcome(RecordStatus.ALREADY_PRESENT, "first_writer_wins", decision.sha256)
    return RecordOutcome(RecordStatus.STORE_ERROR, MissReason.STORE_ERROR, decision.sha256)
