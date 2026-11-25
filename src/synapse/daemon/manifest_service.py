"""Operator-facing candidate manifest service (ADR 026 D8 to D10): build, list, show, recheck, and request a deploy
approval bound to a live manifest.

A thin layer over ``ManifestBuilder`` (which owns the git and verification work) and ``candidate_manifests`` (the
index). It maps the builder's fixed refusal codes to HTTP statuses, never echoes a path or git output, audits what
it did, and keeps the order ADR D9 requires: a manifest is rechecked before an approval is requested.

The builder is blocking (git) and shares the daemon's single-thread database connection, so calls run on the
daemon's event loop one at a time (``_lock``); each git call carries its own timeout.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import timedelta

from synapse.api.agent_commands import AgentRunError
from synapse.daemon.manifest_builder import ManifestBuilder, ManifestRefused, request_manifest_approval
from synapse.domain.contracts import EventEnvelope, encode_payload
from synapse.persistence import candidate_manifests as store
from synapse.application.approval_requests import approval_id_for
from synapse.persistence.idempotency import CommandResult, IdempotencyConflict
from synapse.persistence.run_snapshot import RunNotFound

_LOG = logging.getLogger(__name__)
PRINCIPAL = "local-operator"
MAX_DIFF_BYTES_SHOWN = 64 * 1024
_CODE = re.compile(r"[a-z0-9_]{1,48}(:[a-z0-9_]{1,48})?\Z")

# ManifestRefused.code -> HTTP status. Everything else is a 409 (the manifest or run is not in a usable state).
_STATUS = {
    "manifest_not_found": 404,
    "not_multi_repo": 422, "no_changes": 422, "not_a_writer": 422, "no_source_attempt": 422, "manifest_invalid": 422,
    "baseline_moved": 409, "manifest_invalidated": 409, "candidate_invalid": 409,
    "candidate_unverifiable": 503, "artifact_publish_failed": 503,
}


def refusal(exc: ManifestRefused) -> AgentRunError:
    """The refusal code (plus its fixed sub-code) is the whole message: safe to show and to match on."""
    text = str(exc)
    return AgentRunError(_STATUS.get(exc.code, 409), text if _CODE.fullmatch(text) else exc.code)


class ManifestService:
    def __init__(self, db: sqlite3.Connection, builder: ManifestBuilder | None, requester=None) -> None:
        self.db, self.builder, self.requester = db, builder, requester
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ helpers
    def _require_run(self, run_id: str) -> None:
        if self.db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            raise RunNotFound("Run not found")

    def _require_builder(self) -> ManifestBuilder:
        if self.builder is None:
            raise AgentRunError(503, "Multi-repository runs are not configured (repos and worktree_root)")
        return self.builder

    def _invalidations(self, sha: str) -> list[dict]:
        return [{"reason": r[0], "actor": r[1], "at": r[2]} for r in self.db.execute(
            "SELECT reason,actor,at FROM candidate_manifest_invalidations WHERE manifest_sha256=? ORDER BY seq", (sha,))]

    def _record(self, run_id: str, sha: str) -> store.ManifestRecord:
        record = store.load_manifest(self.db, sha)
        if record is None or record.run_id != run_id:
            raise AgentRunError(404, "manifest_not_found")
        return record

    def _audit(self, run_id: str, event_type: str, extra: dict) -> None:
        try:
            event = EventEnvelope.create(aggregate_type="run", aggregate_id=run_id, event_type=event_type,
                                         payload={"run_id": run_id, "principal": PRINCIPAL, **extra})
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
                                "VALUES(?,?,?,?,?,?,?,?)",
                                (event.id, event.aggregate_type, run_id, run_id, event.event_type, event.schema_version,
                                 encode_payload(event.payload), event.occurred_at))
                self.db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
        except Exception:
            _LOG.error("Manifest event %s of run %s happened but the audit event could not be written", event_type, run_id)

    # --------------------------------------------------------------------- build
    async def build(self, run_id: str, sources: dict | None) -> dict:
        builder = self._require_builder()
        self._require_run(run_id)
        async with self._lock:
            try:
                out = builder.build(run_id, sources=sources)
            except ManifestRefused as exc:
                self._audit(run_id, "agent.manifest_refused", {"action": "build", "code": exc.code})
                raise refusal(exc) from None
            if not out["replayed"]:
                self._audit(run_id, "agent.manifest_built", {"manifest_sha256": out["manifest_sha256"], "seq": out["seq"]})
            return out

    # ---------------------------------------------------------------------- read
    def list(self, run_id: str) -> dict:
        self._require_run(run_id)
        rows = []
        for r in store.run_manifests(self.db, run_id):
            inv = self._invalidations(r.manifest_sha256)
            rows.append({"manifest_sha256": r.manifest_sha256, "version": r.version, "seq": r.seq,
                         "artifact_id": r.artifact_id, "bindings_sha256": r.bindings_sha256, "created_at": r.created_at,
                         "valid": not inv, "invalidations": inv})
        return {"run_id": run_id, "manifests": rows}

    def show(self, run_id: str, sha: str) -> dict:
        """Stored view only: no git is run, so ``valid`` means no recorded invalidation (use recheck to re-verify)."""
        builder = self._require_builder()
        self._require_run(run_id)
        record = self._record(run_id, sha)
        try:
            manifest = builder.load(sha, run_id=run_id)
        except ManifestRefused as exc:
            raise refusal(exc) from None
        diffs = []
        for entry in manifest.repos:
            if entry.diff_sha256 is None or not entry.diff_bytes:
                continue
            try:
                raw = builder.blobs.read(entry.diff_sha256)
            except Exception:
                raise AgentRunError(409, "candidate_invalid:artifact_corrupt") from None
            diffs.append({"repo_id": entry.repo_id, "diff": raw[:MAX_DIFF_BYTES_SHOWN].decode("utf-8", "replace"),
                          "truncated": len(raw) > MAX_DIFF_BYTES_SHOWN})
        inv = self._invalidations(sha)
        return {"run_id": run_id, "manifest_sha256": sha, "version": record.version, "seq": record.seq,
                "created_at": record.created_at, "valid": not inv, "manifest": json.loads(manifest.canonical_bytes()),
                "invalidations": inv, "diffs": diffs}

    # ------------------------------------------------------------------- recheck
    async def recheck(self, run_id: str, sha: str) -> dict:
        builder = self._require_builder()
        self._require_run(run_id)
        self._record(run_id, sha)
        async with self._lock:
            try:
                builder.recheck(sha, run_id=run_id)
            except ManifestRefused as exc:
                self._audit(run_id, "agent.manifest_refused", {"action": "recheck", "manifest_sha256": sha,
                                                               "code": exc.code})
                raise refusal(exc) from None
        self._audit(run_id, "agent.manifest_rechecked", {"manifest_sha256": sha})
        return {"run_id": run_id, "manifest_sha256": sha, "valid": True, "invalidations": []}

    # ------------------------------------------------------------------ approval
    async def request_approval(self, run_id: str, sha: str, environment: str, policy_version: str, ttl_seconds: int,
                               key: str, correlation_id: str) -> CommandResult:
        """Recheck (ADR D9 step 2: records baseline_moved / candidate_invalid and refuses), then request a
        ``deploy`` approval for ``environment`` as the daemon requester. The operator token alone can never decide it."""
        builder = self._require_builder()
        if self.requester is None:
            raise AgentRunError(503, "Approvals unavailable")
        self._require_run(run_id)
        self._record(run_id, sha)
        earlier = self.db.execute(
            "SELECT id,operation,target,candidate_manifest_sha256,policy_version,state,state_version,expires_at "
            "FROM approval_requests WHERE id=? AND run_id=?", (approval_id_for(run_id, key), run_id)).fetchone()
        if earlier is not None:  # the same Idempotency-Key: an exact repeat answers from the record, else it conflicts
            if tuple(earlier[1:5]) != ("deploy", environment, sha, policy_version):
                raise IdempotencyConflict("Idempotency key conflicts with prior request")
            return CommandResult({"approval_id": earlier[0], "run_id": run_id, "manifest_sha256": sha,
                                  "operation": "deploy", "target": environment, "state": earlier[5],
                                  "state_version": earlier[6], "expires_at": earlier[7]}, True)
        async with self._lock:
            try:
                result = request_manifest_approval(
                    builder, self.requester, manifest_sha256=sha, operation="deploy", target=environment,
                    policy_version=policy_version, idempotency_key=key, correlation_id=correlation_id,
                    ttl=timedelta(seconds=ttl_seconds))
            except ManifestRefused as exc:
                self._audit(run_id, "agent.manifest_refused", {"action": "approval", "manifest_sha256": sha,
                                                               "code": exc.code})
                raise refusal(exc) from None
            except sqlite3.DatabaseError as exc:
                if "live candidate manifest" in str(exc) or "manifest_invalidated" in str(exc):
                    raise AgentRunError(409, "manifest_invalidated") from None
                raise
            except ValueError:
                raise AgentRunError(422, "Invalid approval request") from None
        row = self.db.execute("SELECT state,state_version,expires_at FROM approval_requests WHERE id=?",
                              (result.response["approval_id"],)).fetchone()
        body = {"approval_id": result.response["approval_id"], "run_id": run_id, "manifest_sha256": sha,
                "operation": "deploy", "target": environment, "state": row[0], "state_version": row[1],
                "expires_at": row[2]}
        return CommandResult(body, result.replayed)
