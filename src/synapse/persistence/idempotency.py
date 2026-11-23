"""Atomic idempotency-key reservation and deterministic response replay.

Only the daemon-owned SQLite writer may call this module. A callback performs
all state changes inside the same transaction as the stored response.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class IdempotencyConflict(ValueError):
    """An existing key was reused for a different request."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    response: Any
    replayed: bool


def _validate_request_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("request object keys must be strings")
            _validate_request_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_request_keys(item)


def canonical_request_hash(request: Any) -> str:
    """SHA-256 of deterministic UTF-8 JSON, independent of object key order."""
    _validate_request_keys(request)
    serialized = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def execute_idempotent(
    connection: sqlite3.Connection,
    *,
    principal: str,
    operation: str,
    key: str,
    request: Any,
    execute: Callable[[sqlite3.Connection], Any],
) -> CommandResult:
    """Run a database command exactly once for a scoped key and request.

    Commands must use the supplied connection and MUST NOT perform external
    side effects. Retrying a key replays its persisted JSON response. All
    changes, including response persistence, commit or roll back together.
    """
    if not all(isinstance(value, str) and value.strip() for value in (principal, operation, key)):
        raise ValueError("principal, operation and key must be nonempty strings")
    if connection.in_transaction:
        raise RuntimeError("Nested transactions are unsupported")
    request_hash = canonical_request_hash(request)
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys "
            "WHERE principal=? AND operation=? AND key=?",
            (principal, operation, key),
        ).fetchone()
        if existing is not None:
            if existing[0] != request_hash:
                raise IdempotencyConflict("Idempotency key reused for a different request")
            result = CommandResult(json.loads(existing[1]), replayed=True)
        else:
            response = execute(connection)
            response_json = json.dumps(response, sort_keys=True, separators=(",", ":"), allow_nan=False)
            connection.execute(
                "INSERT INTO idempotency_keys "
                "(principal,operation,key,request_sha256,response_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (principal, operation, key, request_hash, response_json,
                 datetime.now(timezone.utc).isoformat()),
            )
            result = CommandResult(json.loads(response_json), replayed=False)
        connection.execute("COMMIT")
        return result
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
