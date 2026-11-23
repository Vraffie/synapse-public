"""Exact result cache: lookup/record logic against a storage port (M5.4 cache 2/3, ADR 017 D6).

Pure: no SQLite, no file or network I/O, no clock, no daemon. Storage is a ``CacheStore`` port; the
``InMemoryCacheStore`` here exists for tests. The SQL ``result_cache`` table (ADR 017 D3) needs a
migration that does not exist yet, so nothing in Synapse calls this module today.

Rules:

- The key and every cacheability rule come from ``synapse.application.cache_key`` and nothing else.
  Anything it bypasses (edits, reviews, verifications, approval-gated or write-capable work, dirty or
  unknown heads, unknown tool versions, malformed input) is a ``bypass`` here with its reason.
- A hit is a *reference* to a recorded result artifact (SHA-256 and size) plus the originating
  attempt. It is never a verification, approval, review or mandatory check (invariants 5, 9, 10):
  ``CacheDecision`` carries those as class constants that are always ``False``, and the caller must
  still run every mandatory check on the reused output.
- Fail to a miss: a store that raises, returns garbage, or returns an entry that is malformed,
  oversized, for another key, invalidated, or whose material does not hash to the key, is a ``miss``
  with a typed reason. ``lookup`` and ``record`` never raise into the caller (except
  ``BaseException`` such as ``KeyboardInterrupt``).
- When a verifier callback is given, it returns the current SHA-256 of the referenced artifact (or
  ``None`` if missing); anything other than the recorded hash is a miss.
- ``put`` is first-writer-wins: an existing entry is never overwritten. The only change an entry may
  ever get is the one-way invalidation marker (ADR 017 D3), which a lookup treats as a miss.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
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
MAX_ENTRY_BYTES = 32 * 1024  # whole entry document, as the future entry_json column holds it
MAX_TEXT = 256

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
    INVALIDATED = "invalidated"
    ARTIFACT_MISSING = "artifact_missing"
    CORRUPT_ENTRY = "corrupt_entry"  # the artifact's current hash is not the recorded one
    VERIFIER_ERROR = "verifier_error"


class RecordStatus(StrEnum):
    STORED = "stored"
    ALREADY_PRESENT = "already_present"  # first writer won; nothing was overwritten
    REFUSED = "refused"  # not cacheable, not storable, or an invalid result
    STORE_ERROR = "store_error"


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One stored result reference. ``key_material_json`` is the canonical JSON of the key's material."""

    key_sha256: str
    result_sha256: str
    result_size: int
    origin_attempt_id: str
    created_marker: str  # opaque, caller-supplied (an event sequence or timestamp); this module has no clock
    key_material_json: str
    entry_schema_version: int = ENTRY_SCHEMA_VERSION
    invalidated_marker: str | None = None  # one-way: once set it is never cleared

    def document(self) -> dict[str, Any]:
        """The ``entry_json`` a SQL store would persist (see docs/operations/exact-cache-logic.md)."""
        return {
            "entry_schema_version": self.entry_schema_version, "key_sha256": self.key_sha256,
            "result_sha256": self.result_sha256, "result_size": self.result_size,
            "origin_attempt_id": self.origin_attempt_id, "created_marker": self.created_marker,
            "key_material": json.loads(self.key_material_json),
        }


@runtime_checkable
class CacheStore(Protocol):
    """Storage port. ``put`` returns True if it stored the entry, False if the key already existed."""

    def get(self, key_sha256: str) -> CacheEntry | None: ...

    def put(self, key_sha256: str, entry: CacheEntry) -> bool: ...


class InMemoryCacheStore:
    """Deterministic, thread-safe, first-writer-wins store for tests. Never overwrites an entry."""

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, key_sha256: str) -> CacheEntry | None:
        with self._lock:
            return self._entries.get(key_sha256)

    def put(self, key_sha256: str, entry: CacheEntry) -> bool:
        with self._lock:
            if key_sha256 in self._entries:
                return False
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

    counts_as_verification: ClassVar[bool] = False
    counts_as_review: ClassVar[bool] = False
    satisfies_approval: ClassVar[bool] = False
    counts_as_mandatory_check: ClassVar[bool] = False
    requires_verification: ClassVar[bool] = True

    @property
    def is_hit(self) -> bool:
        return self.outcome is CacheOutcome.HIT


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


Verifier = Callable[[str], "str | None"]


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_TEXT and not _CONTROL.search(value)


def _sha(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _size(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_RESULT_BYTES


def _material_json(material: Mapping[str, Any]) -> str:
    return json.dumps(material, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _entry_problem(entry: object, key: CacheKey) -> MissReason | None:
    """Why ``entry`` must not be served for ``key``; None when it is internally consistent."""
    if not isinstance(entry, CacheEntry):
        return MissReason.MALFORMED_ENTRY
    if type(entry.entry_schema_version) is not int or entry.entry_schema_version != ENTRY_SCHEMA_VERSION:
        return MissReason.MALFORMED_ENTRY
    if not isinstance(entry.key_sha256, str) or entry.key_sha256 != key.sha256:
        return MissReason.KEY_MISMATCH
    if not (_sha(entry.result_sha256) and _text(entry.origin_attempt_id) and _text(entry.created_marker)):
        return MissReason.MALFORMED_ENTRY
    if type(entry.result_size) is not int or entry.result_size < 0:
        return MissReason.MALFORMED_ENTRY
    if entry.result_size > MAX_RESULT_BYTES:
        return MissReason.ENTRY_TOO_LARGE
    if not isinstance(entry.key_material_json, str):
        return MissReason.MALFORMED_ENTRY
    if len(entry.key_material_json.encode("utf-8")) > MAX_MATERIAL_BYTES:
        return MissReason.ENTRY_TOO_LARGE
    if entry.invalidated_marker is not None:
        return MissReason.INVALIDATED
    material = json.loads(entry.key_material_json)
    if not isinstance(material, dict) or set(material) != _MATERIAL_FIELDS:
        return MissReason.MALFORMED_ENTRY
    # the stored components must hash to the key and equal the current request's components exactly
    if canonical_request_hash(material) != key.sha256 or _material_json(material) != _material_json(key.material):
        return MissReason.MATERIAL_MISMATCH
    if len(json.dumps(entry.document(), sort_keys=True, separators=(",", ":")).encode("utf-8")) > MAX_ENTRY_BYTES:
        return MissReason.ENTRY_TOO_LARGE
    return None


def _miss(reason: str, key: CacheKey | None = None) -> CacheDecision:
    return CacheDecision(CacheOutcome.MISS, reason, key.sha256 if key is not None else None)


def lookup(store: CacheStore, request: CacheKeyInputs, *, verifier: Verifier | None = None) -> CacheDecision:
    """Decide hit, miss or bypass for ``request``. Never raises; any doubt is a miss or a bypass."""
    try:
        decision = derive_cache_key(request)
    except Exception:  # derive_cache_key never raises; belt and braces
        return CacheDecision(CacheOutcome.BYPASS, "invalid_input")
    if isinstance(decision, CacheBypass):
        return CacheDecision(CacheOutcome.BYPASS, decision.reason if isinstance(decision.reason, str) else "invalid_input")
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
        problem = _entry_problem(entry, key)
    except Exception:  # undecodable JSON, cyclic or non-serialisable values, recursion: never served
        problem = MissReason.MALFORMED_ENTRY
    if problem is not None:
        return _miss(problem, key)

    checked = False
    if verifier is not None:
        try:
            current = verifier(entry.result_sha256)
            if current is None:
                return _miss(MissReason.ARTIFACT_MISSING, key)
            if type(current) is not str or current != entry.result_sha256:
                return _miss(MissReason.CORRUPT_ENTRY, key)
        except Exception:
            return _miss(MissReason.VERIFIER_ERROR, key)
        checked = True

    hit = CacheHit(key.sha256, entry.origin_attempt_id)
    if not may_serve(key, hit):
        return _miss(MissReason.MALFORMED_ENTRY, key)
    return CacheDecision(CacheOutcome.HIT, None, key.sha256, hit, entry.result_sha256, entry.result_size, checked)


def _result_problem(result: object) -> str | None:
    if not isinstance(result, CacheResult):
        return "invalid_result"
    if not _text(result.origin_attempt_id):
        return "invalid_result:origin_attempt_id"
    if not isinstance(result.outcome, str):
        return "invalid_result:outcome"
    if not isinstance(result.budget_blocked, bool):
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


def record(store: CacheStore, request: CacheKeyInputs, result: CacheResult) -> RecordOutcome:
    """Store ``result`` under ``request``'s key if allowed; first writer wins. Never raises."""
    try:
        decision = derive_cache_key(request)
        if isinstance(decision, CacheBypass):
            return RecordOutcome(RecordStatus.REFUSED, decision.reason)
        if not isinstance(decision, CacheKey):
            return RecordOutcome(RecordStatus.REFUSED, "invalid_input")
        problem = _result_problem(result)
        if problem is not None:
            return RecordOutcome(RecordStatus.REFUSED, problem, decision.sha256)
        refused = store_decision(decision, outcome=result.outcome, budget_blocked=result.budget_blocked)
        if refused is not None:
            return RecordOutcome(RecordStatus.REFUSED, refused.reason, decision.sha256)
        material = _material_json(decision.material)
        if len(material.encode("utf-8")) > MAX_MATERIAL_BYTES:
            return RecordOutcome(RecordStatus.REFUSED, "entry_too_large", decision.sha256)
        entry = CacheEntry(decision.sha256, result.result_sha256, result.result_size, result.origin_attempt_id,
                           result.created_marker, material)
        entry_problem = _entry_problem(entry, decision)
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
