"""Operator-facing candidate manifest service (ADR 026 D8 to D10): build, list, show, recheck, and request a deploy
approval bound to a live manifest.

A thin layer over ``ManifestBuilder`` (which owns the git and verification work) and ``candidate_manifests`` (the
index). It maps the builder's fixed refusal codes to HTTP statuses, never echoes a path or git output, audits what
it did, and keeps the order ADR D9 requires: a manifest is rechecked before an approval is requested.

Threading: the builder is blocking (git). Build and recheck therefore run in a worker thread
(``asyncio.to_thread``), on a private SQLite connection that the thread opens (after the same private-file check
the daemon runs at startup) and closes itself: the daemon's own connection is single-threaded and is only ever used
on the event loop (reads, audit events, idempotency receipts, the approval request). ``_lock`` serialises the
builder work across requests and is held across the awaits; an approval's replay lookup, recheck and request all run
under it. When a request's overall deadline (``deadline_seconds``, which also covers waiting for the lock) expires,
the request answers 503 ``candidate_unverifiable:deadline`` (audited with code ``deadline``) while the thread keeps
the lock until git returns (each git call carries its own timeout), so a late builder never overlaps the next one.
What the late builder then did (a manifest indexed, ``baseline_moved`` or ``candidate_invalid`` recorded) is
audited on the loop with ``"late": true``; a late recheck never requests an approval.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone

from synapse.api.agent_commands import AgentRunError
from synapse.daemon.manifest_builder import ManifestBuilder, ManifestRefused
from synapse.domain.contracts import EventEnvelope, encode_payload
from synapse.infrastructure.daemon_database import check_private_database
from synapse.persistence import candidate_manifests as store
from synapse.application.approval_requests import approval_id_for
from synapse.persistence.approval_environment import validate_environment
from synapse.persistence.idempotency import CommandResult, IdempotencyConflict, canonical_request_hash, execute_idempotent
from synapse.persistence.migrations import open_database
from synapse.persistence.run_snapshot import RunNotFound

_LOG = logging.getLogger(__name__)
PRINCIPAL = "local-operator"
MAX_DIFF_BYTES_SHOWN = 64 * 1024
DEFAULT_DEADLINE_SECONDS = 120.0
# Manifest approvals live in their own idempotency namespace: an operator key can never equal (and so reuse the
# approval id of) the spawn gate's ``gate:<task_id>`` keys or any other requester's keys.
APPROVAL_KEY_PREFIX = "manifest:"
MAX_APPROVAL_KEY = 256 - len(APPROVAL_KEY_PREFIX)  # the requester refuses keys over 256 characters
RECEIPT_OPERATION = "manifest.approval"  # idempotency_keys receipt of the whole request, ttl_seconds included
DEADLINE = "candidate_unverifiable:deadline"
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
    def __init__(self, db: sqlite3.Connection, builder: ManifestBuilder | None, requester=None, *,
                 deadline_seconds: float = DEFAULT_DEADLINE_SECONDS) -> None:
        self.db, self.builder, self.requester = db, builder, requester
        self.deadline_seconds = deadline_seconds
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

    def _database_path(self) -> str:
        for _seq, name, path in self.db.execute("PRAGMA database_list"):
            if name == "main" and path:
                return path
        raise AgentRunError(503, "Candidate manifests need a file database")

    # ------------------------------------------------------- off-loop builder work
    @staticmethod
    def _in_thread(path: str, template: ManifestBuilder, work: Callable[[ManifestBuilder], object]):
        """Worker thread: the builder gets its own connection, opened and closed in this thread."""
        try:
            check_private_database(path)  # the startup check again: the file must still be the daemon's own
        except OSError:
            raise AgentRunError(503, "Database file is not private") from None
        with closing(open_database(path)) as db:
            return work(ManifestBuilder(db, template.blobs, template.manager, principal=template.principal))

    def _finish_late(self, job: asyncio.Future, on_late) -> None:
        """On the loop, for a builder thread whose request already answered: audit what it did, then unlock."""
        try:
            if on_late is not None and not job.cancelled():
                exc = job.exception()
                on_late(None if exc is not None else job.result(), exc)
        except Exception:
            _LOG.error("Could not audit a manifest builder call that finished after its deadline")
        finally:
            self._lock.release()

    async def _exclusive(self, work: Callable[[ManifestBuilder], object], *, before=None, then=None, on_late=None):
        """Under ``_lock`` and the request deadline: ``before()`` on the loop (a non-None result is returned at once),
        then ``work(builder)`` in a thread, then ``then(result)`` on the loop. Raises ``ManifestRefused`` from the
        builder, or 503 ``DEADLINE``; a thread that outlives the deadline reports to ``on_late(result, exc)``."""
        template = self._require_builder()
        path = self._database_path()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.deadline_seconds
        try:
            async with asyncio.timeout_at(deadline):
                await self._lock.acquire()
        except TimeoutError:
            raise AgentRunError(503, DEADLINE) from None
        try:
            early = None if before is None else before()
        except BaseException:
            self._lock.release()
            raise
        if early is not None:
            self._lock.release()
            return early
        job = asyncio.ensure_future(asyncio.to_thread(self._in_thread, path, template, work))
        try:
            async with asyncio.timeout_at(deadline):
                out = await asyncio.shield(job)
        except BaseException as exc:
            if job.done() and not job.cancelled() and job.exception() is exc:  # the builder's own refusal
                self._lock.release()
                raise
            if job.done():  # finished just as the deadline (or a cancellation) hit: its result is still late
                self._finish_late(job, on_late)
            else:  # the thread keeps the lock until it returns
                job.add_done_callback(lambda j: self._finish_late(j, on_late))
            if isinstance(exc, TimeoutError):
                raise AgentRunError(503, DEADLINE) from None
            raise
        try:
            return out if then is None else then(out)
        finally:
            self._lock.release()

    # --------------------------------------------------------------------- build
    async def build(self, run_id: str, sources: dict | None) -> dict:
        self._require_builder()
        self._require_run(run_id)

        def late(out, exc):
            if isinstance(exc, ManifestRefused):
                self._audit(run_id, "agent.manifest_refused", {"action": "build", "code": exc.code, "late": True})
            elif exc is None and not out["replayed"]:
                self._audit(run_id, "agent.manifest_built", {"manifest_sha256": out["manifest_sha256"],
                                                             "seq": out["seq"], "late": True})
            elif exc is not None:
                _LOG.error("A manifest build of run %s failed after its deadline (%s)", run_id, type(exc).__name__)
        try:
            out = await self._exclusive(lambda builder: builder.build(run_id, sources=sources), on_late=late)
        except ManifestRefused as exc:
            self._audit(run_id, "agent.manifest_refused", {"action": "build", "code": exc.code})
            raise refusal(exc) from None
        except AgentRunError as exc:
            if exc.message == DEADLINE:
                self._audit(run_id, "agent.manifest_refused", {"action": "build", "code": "deadline"})
            raise
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
    async def _recheck(self, run_id: str, sha: str, action: str, *, before=None, then=None):
        def late(_out, exc):  # only reports what the late recheck did: never requests an approval
            if isinstance(exc, ManifestRefused):
                self._audit(run_id, "agent.manifest_refused", {"action": action, "manifest_sha256": sha,
                                                               "code": exc.code, "late": True})
            elif exc is None:
                self._audit(run_id, "agent.manifest_rechecked", {"manifest_sha256": sha, "late": True})
            else:
                _LOG.error("A manifest recheck of run %s failed after its deadline (%s)", run_id, type(exc).__name__)
        try:
            return await self._exclusive(lambda builder: builder.recheck(sha, run_id=run_id), before=before,
                                         then=then, on_late=late)
        except ManifestRefused as exc:
            self._audit(run_id, "agent.manifest_refused", {"action": action, "manifest_sha256": sha, "code": exc.code})
            raise refusal(exc) from None
        except AgentRunError as exc:
            if exc.message == DEADLINE:
                self._audit(run_id, "agent.manifest_refused", {"action": action, "manifest_sha256": sha,
                                                               "code": "deadline"})
            raise

    async def recheck(self, run_id: str, sha: str) -> dict:
        self._require_builder()
        self._require_run(run_id)
        self._record(run_id, sha)
        await self._recheck(run_id, sha, "recheck")
        self._audit(run_id, "agent.manifest_rechecked", {"manifest_sha256": sha})
        return {"run_id": run_id, "manifest_sha256": sha, "valid": True, "invalidations": []}

    # ------------------------------------------------------------------ approval
    async def request_approval(self, run_id: str, sha: str, environment: str, policy_version: str, ttl_seconds: int,
                               key: str, correlation_id: str) -> CommandResult:
        """Recheck (ADR D9 step 2: records baseline_moved / candidate_invalid and refuses), then request a
        ``deploy`` approval for ``environment`` as the daemon requester. The operator token alone can never decide it.

        Idempotency: a receipt of the whole request (ttl_seconds included) is stored in ``idempotency_keys`` right
        before the approval is requested. Under the lock, a repeat of the same key and request answers from the
        stored approval (no recheck, but 409 ``manifest_invalidated`` once the manifest has an invalidation); the
        same key with another request is 409."""
        self._require_builder()
        if self.requester is None:
            raise AgentRunError(503, "Approvals unavailable")
        try:
            validate_environment(environment)  # ADR 022 allowlist; the route refuses the same values first
        except ValueError:
            raise AgentRunError(422, "Invalid approval request") from None
        if not isinstance(key, str) or not key.strip() or len(key) > MAX_APPROVAL_KEY:
            raise AgentRunError(422, "Invalid approval request")  # before any git: the requester would refuse it
        self._require_run(run_id)
        self._record(run_id, sha)
        approval_key = APPROVAL_KEY_PREFIX + key
        approval_id = approval_id_for(run_id, approval_key)
        receipt_key = json.dumps([run_id, key], separators=(",", ":"))
        request = {"run_id": run_id, "manifest_sha256": sha, "operation": "deploy", "environment": environment,
                   "policy_version": policy_version, "ttl_seconds": ttl_seconds}

        def answer(replayed: bool) -> CommandResult:
            row = self.db.execute("SELECT state,state_version,expires_at FROM approval_requests WHERE id=?",
                                  (approval_id,)).fetchone()
            return CommandResult({"approval_id": approval_id, "run_id": run_id, "manifest_sha256": sha,
                                  "operation": "deploy", "target": environment, "state": row[0],
                                  "state_version": row[1], "expires_at": row[2]}, replayed)

        def replay():  # under the lock, before any git
            stored = self.db.execute(
                "SELECT request_sha256 FROM idempotency_keys WHERE principal=? AND operation=? AND key=?",
                (PRINCIPAL, RECEIPT_OPERATION, receipt_key)).fetchone()
            exists = self.db.execute("SELECT 1 FROM approval_requests WHERE id=? AND run_id=?",
                                     (approval_id, run_id)).fetchone() is not None
            if stored is None:
                if exists:  # an approval under this key without a receipt: never guess that it matches
                    raise IdempotencyConflict("Idempotency key conflicts with prior request")
                return None
            if stored[0] != canonical_request_hash(request):
                raise IdempotencyConflict("Idempotency key conflicts with prior request")
            if not exists:
                return None  # the earlier attempt was refused after its receipt: recheck and try again
            if self._invalidations(sha):  # no recheck on replay, but never hand back an approval of a dead manifest
                raise AgentRunError(409, "manifest_invalidated")
            return answer(True)

        def ask(manifest) -> CommandResult:  # on the loop, still under the lock, right after a successful recheck
            execute_idempotent(self.db, principal=PRINCIPAL, operation=RECEIPT_OPERATION, key=receipt_key,
                               request=request, execute=lambda _db: {"approval_id": approval_id})
            expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
            result = self.requester.request_approval(
                run_id=manifest.run_id, operation="deploy", target=environment, candidate_sha256=sha,
                policy_version=policy_version, expires_at=expires, idempotency_key=approval_key,
                correlation_id=correlation_id)
            if result.response["approval_id"] != approval_id:
                raise RuntimeError("approval id mismatch")
            return answer(result.replayed)
        try:
            return await self._recheck(run_id, sha, "approval", before=replay, then=ask)
        except sqlite3.DatabaseError as exc:
            if "live candidate manifest" in str(exc) or "manifest_invalidated" in str(exc):
                raise AgentRunError(409, "manifest_invalidated") from None
            raise
        except ValueError as exc:
            if isinstance(exc, IdempotencyConflict):
                raise
            raise AgentRunError(422, "Invalid approval request") from None
