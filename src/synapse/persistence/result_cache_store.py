"""SQLite ``CacheStore`` for the exact result cache (M5.4 cache 3/3, ADR 017 D3/D6, migration 0018).

Implements the ``synapse.application.result_cache.CacheStore`` port over the ``result_cache`` table.
The daemon owns the connection; nothing else may write this table. Not wired into the engine yet.

- ``put`` is a plain ``INSERT``. The table's ``BEFORE INSERT`` trigger skips a second row for a key
  (``RAISE(IGNORE)``), so ``put`` returns ``False`` (``already_present``) and never overwrites.
- ``get`` reads ``entry_json`` and ``invalidated_at`` only. The copied columns are never trusted.
  ``entry_json`` is parsed strictly: duplicate object keys, ``NaN``/``Infinity`` and any field set
  other than the exact one ``CacheEntry.entry_json()`` writes raise ``CorruptCacheEntry``, which
  ``lookup`` turns into a ``store_error`` miss. Field values are not judged here: ``lookup`` checks
  key, shapes, sizes, material and digest, and a hit is never a verification (invariant 10).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from synapse.application.result_cache import MAX_ENTRY_BYTES, CacheEntry

_ENTRY_FIELDS = frozenset({
    "entry_schema_version", "key_sha256", "result_sha256", "result_size", "origin_attempt_id",
    "created_marker", "entry_digest", "key_material",
})


class CorruptCacheEntry(ValueError):
    """A stored row could not be decoded into a ``CacheEntry``."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CorruptCacheEntry(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _no_constant(name: str) -> Any:
    raise CorruptCacheEntry(f"non-standard JSON constant: {name}")


def _decode(entry_json: object, invalidated_at: object) -> CacheEntry:
    if type(entry_json) is not str:
        raise CorruptCacheEntry("entry_json is not text")
    if len(entry_json.encode("utf-8")) > MAX_ENTRY_BYTES:
        raise CorruptCacheEntry("entry_json too large")
    if invalidated_at is not None and type(invalidated_at) is not str:
        raise CorruptCacheEntry("invalidated_at is not text")
    try:
        data = json.loads(entry_json, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except CorruptCacheEntry:
        raise
    except ValueError as exc:
        raise CorruptCacheEntry("entry_json is not JSON") from exc
    if type(data) is not dict or set(data) != _ENTRY_FIELDS:
        raise CorruptCacheEntry("entry_json has the wrong fields")
    material_json = json.dumps(data["key_material"], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return CacheEntry(
        key_sha256=data["key_sha256"], result_sha256=data["result_sha256"],
        result_size=data["result_size"], origin_attempt_id=data["origin_attempt_id"],
        created_marker=data["created_marker"], key_material_json=material_json,
        entry_digest=data["entry_digest"], entry_schema_version=data["entry_schema_version"],
        invalidated_marker=invalidated_at,
    )


class SqliteCacheStore:
    """First-writer-wins ``CacheStore`` on a daemon-owned connection (``open_database`` defaults).

    If the connection is already inside a transaction, ``put`` joins it; otherwise it runs its own
    ``BEGIN IMMEDIATE`` ... ``COMMIT`` so concurrent writers on other connections serialize.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def get(self, key_sha256: str) -> CacheEntry | None:
        row = self._db.execute(
            "SELECT entry_json, invalidated_at FROM result_cache WHERE key_sha256 = ?", (key_sha256,)
        ).fetchone()
        if row is None:
            return None
        return _decode(row[0], row[1])

    def put(self, key_sha256: str, entry: CacheEntry) -> bool:
        if type(entry) is not CacheEntry:
            raise TypeError("entry must be a CacheEntry")
        if entry.key_sha256 != key_sha256:
            raise ValueError("entry is for another key")
        if entry.invalidated_marker is not None:
            raise ValueError("a new entry cannot be stored already invalidated")
        params = (key_sha256, entry.entry_json(), entry.result_sha256, entry.origin_attempt_id,
                  entry.created_marker)
        own = not self._db.in_transaction
        if own:
            self._db.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._db.execute(
                "INSERT INTO result_cache(key_sha256, entry_json, result_artifact_id, origin_attempt_id,"
                " created_at) VALUES (?, ?, ?, ?, ?)", params)
            stored = cursor.rowcount == 1
            if own:
                self._db.execute("COMMIT")
        except BaseException:
            if own and self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return stored

    def invalidate(self, key_sha256: str, marker: str) -> bool:
        """Set the one-way invalidation marker. False if the key is absent or already invalidated.

        Not part of the port and not an audited operator action yet; tests and a future daemon
        command use it. The table trigger refuses any other change.
        """
        own = not self._db.in_transaction
        if own:
            self._db.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._db.execute(
                "UPDATE result_cache SET invalidated_at = ? WHERE key_sha256 = ? AND invalidated_at IS NULL",
                (marker, key_sha256))
            changed = cursor.rowcount == 1
            if own:
                self._db.execute("COMMIT")
        except BaseException:
            if own and self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return changed
