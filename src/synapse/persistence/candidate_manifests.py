"""Candidate manifest index and invalidations (ADR 026 D2/D8/D9, migration 0019).

* ``candidate_manifests``: one insert-only row per v2 manifest of a multi-repository run, pointing at the immutable
  artifact that holds its canonical bytes. Triggers refuse a row whose artifact is not a v2 candidate manifest of
  the same run with the manifest hash as content address, a run without bindings or with other bindings, a seq out
  of order, and any new manifest once one of the run's manifests carries a ``baseline_moved`` invalidation.
* ``candidate_manifest_invalidations``: insert-only; a manifest with any row here can never again be approved (a
  trigger refuses the approval transition) or bound by a new approval request.

Every writer runs inside the caller's transaction. Rows carry ids, hashes and fixed reason codes only.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from synapse.domain.contracts import validate_id

INVALIDATION_REASONS = ("baseline_moved", "candidate_invalid", "operator")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ManifestRecord:
    manifest_sha256: str
    run_id: str
    artifact_id: str
    bindings_sha256: str
    version: int
    seq: int
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _need_txn(db: sqlite3.Connection, what: str) -> None:
    if not db.in_transaction:
        raise RuntimeError("%s requires a transaction" % what)


def _sha256(value: object, what: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("%s must be a full lowercase SHA-256" % what)
    return value


def tables_present(db: sqlite3.Connection) -> bool:
    """False only for a database older than migration 0019 (which can hold no manifest)."""
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidate_manifests'").fetchone() \
        is not None


def record_manifest(db: sqlite3.Connection, *, manifest_sha256: str, run_id: str, artifact_id: str,
                    bindings_sha256: str, version: int = 2) -> ManifestRecord:
    """Index a published v2 manifest (caller's transaction). Replays an identical existing row."""
    _need_txn(db, "Recording a candidate manifest")
    validate_id(run_id)
    validate_id(artifact_id)
    _sha256(manifest_sha256, "manifest_sha256")
    _sha256(bindings_sha256, "bindings_sha256")
    existing = load_manifest(db, manifest_sha256)
    if existing is not None:
        if (existing.run_id, existing.artifact_id, existing.bindings_sha256, existing.version) != \
                (run_id, artifact_id, bindings_sha256, version):
            raise ValueError("a different manifest record already uses this hash")
        return existing
    seq = 1 + db.execute("SELECT COALESCE(max(seq),0) FROM candidate_manifests WHERE run_id=?", (run_id,)).fetchone()[0]
    now = _now()
    db.execute("INSERT INTO candidate_manifests(manifest_sha256,run_id,artifact_id,bindings_sha256,version,seq,"
               "created_at) VALUES(?,?,?,?,?,?,?)",
               (manifest_sha256, run_id, artifact_id, bindings_sha256, version, seq, now))
    return ManifestRecord(manifest_sha256, run_id, artifact_id, bindings_sha256, version, seq, now)


def load_manifest(db: sqlite3.Connection, manifest_sha256: str) -> ManifestRecord | None:
    row = db.execute("SELECT manifest_sha256,run_id,artifact_id,bindings_sha256,version,seq,created_at "
                     "FROM candidate_manifests WHERE manifest_sha256=?", (manifest_sha256,)).fetchone()
    return None if row is None else ManifestRecord(*row)


def run_manifests(db: sqlite3.Connection, run_id: str) -> list[ManifestRecord]:
    rows = db.execute("SELECT manifest_sha256,run_id,artifact_id,bindings_sha256,version,seq,created_at "
                      "FROM candidate_manifests WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
    return [ManifestRecord(*r) for r in rows]


def invalidations(db: sqlite3.Connection, manifest_sha256: str) -> list[str]:
    """The invalidation reasons recorded for a manifest, oldest first (empty: live)."""
    return [r[0] for r in db.execute("SELECT reason FROM candidate_manifest_invalidations WHERE manifest_sha256=? "
                                     "ORDER BY seq", (manifest_sha256,))]


def is_invalidated(db: sqlite3.Connection, manifest_sha256: str) -> bool:
    """True if ``manifest_sha256`` has any invalidation row, whatever run asks (matched by manifest hash only, so an
    approval row naming another run cannot hide an invalidation). No such row, or a pre-0019 database: False."""
    if not tables_present(db):
        return False
    return db.execute("SELECT 1 FROM candidate_manifest_invalidations WHERE manifest_sha256=? LIMIT 1",
                      (manifest_sha256,)).fetchone() is not None


def baseline_moved(db: sqlite3.Connection, run_id: str) -> bool:
    return db.execute("SELECT 1 FROM candidate_manifest_invalidations i JOIN candidate_manifests m "
                      "ON m.manifest_sha256=i.manifest_sha256 WHERE m.run_id=? AND i.reason='baseline_moved' LIMIT 1",
                      (run_id,)).fetchone() is not None


def invalidate(db: sqlite3.Connection, manifest_sha256: str, reason: str, actor: str) -> bool:
    """Record an invalidation (caller's transaction). Idempotent per (manifest, reason); True if a row was added."""
    _need_txn(db, "Invalidating a candidate manifest")
    if reason not in INVALIDATION_REASONS:
        raise ValueError("invalid invalidation reason")
    validate_id(actor)
    _sha256(manifest_sha256, "manifest_sha256")
    if db.execute("SELECT 1 FROM candidate_manifest_invalidations WHERE manifest_sha256=? AND reason=?",
                  (manifest_sha256, reason)).fetchone():
        return False
    db.execute("INSERT INTO candidate_manifest_invalidations(manifest_sha256,reason,actor,at) VALUES(?,?,?,?)",
               (manifest_sha256, reason, actor, _now()))
    return True


def invalidate_run(db: sqlite3.Connection, run_id: str, reason: str, actor: str) -> int:
    """Invalidate every manifest of the run (D2: a moved baseline). Returns the number of rows added."""
    return sum(invalidate(db, m.manifest_sha256, reason, actor) for m in run_manifests(db, run_id))


__all__ = ["INVALIDATION_REASONS", "ManifestRecord", "baseline_moved", "invalidate", "invalidate_run",
           "invalidations", "is_invalidated", "load_manifest", "record_manifest", "run_manifests", "tables_present"]
