"""Environment binding for approvals (ADR-022). No deployment code lives here.

An approval may be bound to one target environment (an allowlisted opaque identifier such as ``STAGING``). The binding
lives in ``approval_environment_binding`` (inline schema, like the side-effect ledger; no migration), is immutable,
and carries ``bound_sha256 = sha256(domain || candidate_sha256 || environment)``. ``verify_approval_for_environment``
runs the normal execution-time recheck and then requires the stored environment, and the recomputed bound hash, to match
the environment about to be acted on. Missing, unknown or different environment fails closed. The existing
``candidate_manifest_sha256`` and every legacy approval hash are untouched.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
from datetime import datetime, timezone

from synapse.persistence.approval_binding import (
    ApprovalGrant, DenialReason, _deny, verify_approval_for_execution)

ALLOWED_ENVIRONMENTS = frozenset({"STAGING"})
_DOMAIN = b"synapse.approval-environment.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_environment_binding (
  approval_id TEXT PRIMARY KEY REFERENCES approval_requests(id),
  environment TEXT NOT NULL, bound_sha256 TEXT NOT NULL CHECK (length(bound_sha256) = 64), created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS approval_environment_binding_only_undecided BEFORE INSERT ON approval_environment_binding
WHEN NOT EXISTS (SELECT 1 FROM approval_requests WHERE id = NEW.approval_id AND state = 'requested' AND state_version = 0)
BEGIN SELECT RAISE(ABORT, 'environment can only be bound to an undecided request'); END;
CREATE TRIGGER IF NOT EXISTS approval_environment_binding_no_update BEFORE UPDATE ON approval_environment_binding
BEGIN SELECT RAISE(ABORT, 'approval environment binding is immutable'); END;
CREATE TRIGGER IF NOT EXISTS approval_environment_binding_no_delete BEFORE DELETE ON approval_environment_binding
BEGIN SELECT RAISE(ABORT, 'approval environment binding is immutable'); END;
"""


def validate_environment(environment: object) -> str:
    """Return the environment if it is allowlisted; otherwise raise ValueError (fail closed)."""
    if not isinstance(environment, str) or environment not in ALLOWED_ENVIRONMENTS:
        raise ValueError("unknown or missing environment")
    return environment


def environment_bound_hash(candidate_sha256: str, environment: str) -> str:
    """Hash binding a candidate digest to an environment. Length-prefixed so fields cannot be shifted."""
    validate_environment(environment)
    if not isinstance(candidate_sha256, str) or not _SHA256.fullmatch(candidate_sha256):
        raise ValueError("candidate_sha256 must be 64 lowercase hex characters")
    h = hashlib.sha256(_DOMAIN)
    for part in (candidate_sha256.encode(), environment.encode()):
        h.update(len(part).to_bytes(4, "big") + part)
    return h.hexdigest()


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the binding table if absent (idempotent); needs an autocommit connection and approval_requests."""
    if connection.in_transaction:
        raise RuntimeError("ensure_schema requires an autocommit connection")
    connection.execute("BEGIN IMMEDIATE")
    try:
        buf = ""
        for line in SCHEMA.splitlines():
            buf += line + "\n"
            if sqlite3.complete_statement(buf):
                connection.execute(buf)
                buf = ""
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def bind_approval_environment(connection: sqlite3.Connection, *, approval_id: str, environment: str) -> str:
    """Bind a still-undecided request to an environment, in the caller's command transaction. Returns the bound hash.

    Allowed only while the request is ``requested`` at version 0, so a decided approval can never be re-targeted.
    """
    if not connection.in_transaction:
        raise RuntimeError("Environment binding requires a command transaction")
    validate_environment(environment)
    row = connection.execute("SELECT state,state_version,candidate_manifest_sha256 FROM approval_requests WHERE id=?",
                             (approval_id,)).fetchone()
    if row is None:
        raise ValueError("approval not found")
    if row[0] != "requested" or row[1] != 0:
        raise ValueError("environment can only be bound to an undecided request")
    bound = environment_bound_hash(row[2], environment)
    connection.execute("INSERT INTO approval_environment_binding(approval_id,environment,bound_sha256,created_at) "
                       "VALUES(?,?,?,?)", (approval_id, environment, bound, datetime.now(timezone.utc).isoformat()))
    return bound


def verify_approval_for_environment(connection: sqlite3.Connection, *, environment: str, **kwargs) -> ApprovalGrant:
    """Execution-time recheck including the environment about to be acted on.

    ``kwargs`` are those of ``verify_approval_for_execution``. Order: the normal recheck first, then environment.
    Unknown/missing requested environment is MALFORMED; an approval without a binding, with another environment, or
    with a hash that no longer recomputes is denied (BINDING_MISMATCH / MALFORMED). Nothing is written.
    """
    try:
        validate_environment(environment)
    except ValueError as error:
        raise _deny(DenialReason.MALFORMED, "Unknown or missing environment") from error
    grant = verify_approval_for_execution(connection, **kwargs)
    approval_id, candidate = kwargs["approval_id"], kwargs["candidate_sha256"]
    connection.execute("BEGIN")
    try:
        row = connection.execute("SELECT environment,bound_sha256 FROM approval_environment_binding "
                                 "WHERE approval_id=?", (approval_id,)).fetchone()
    except sqlite3.OperationalError as error:  # table absent: nothing was ever bound
        raise _deny(DenialReason.BINDING_MISMATCH, "Approval has no environment binding") from error
    finally:
        if connection.in_transaction:
            connection.execute("COMMIT")
    if row is None:
        raise _deny(DenialReason.BINDING_MISMATCH, "Approval has no environment binding")
    stored_env, stored_hash = row
    try:
        validate_environment(stored_env)
        expected = environment_bound_hash(candidate, stored_env)
    except ValueError as error:
        raise _deny(DenialReason.MALFORMED, "Malformed environment binding") from error
    if not (isinstance(stored_hash, str) and hmac.compare_digest(stored_hash.encode(), expected.encode())):
        raise _deny(DenialReason.MALFORMED, "Environment bound hash does not match")
    if not hmac.compare_digest(stored_env.encode(), environment.encode()):
        raise _deny(DenialReason.BINDING_MISMATCH, "Approval is bound to a different environment")
    return grant
