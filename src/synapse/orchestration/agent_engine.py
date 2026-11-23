"""Real-provider task execution through the mock engine's claim/attempt/gate machinery.

``AgentWorkflowEngine`` reuses ``MockWorkflowEngine`` scheduling, attempts,
approval gates and run completion and replaces only the provider step. Policy
differences, all deliberately conservative:

* a failed, timed-out or verifier-rejected attempt is never retried (a retry
  would spend quota and may modify the worktree again);
* an ``unknown`` provider outcome, or any error after the process may have
  started, records the attempt as ``unknown`` and blocks the task for an
  operator; nothing re-spawns it;
* the full provider text output is stored as a raw artifact (its SHA-256 is
  recorded in the summary), and the summary carries the deterministic reduction
  of it (every unique failure kept, see ``application.output_reducer``) plus
  tool names only, stored as a second artifact; usage is recorded as an event.
  Reduction affects only what is forwarded; the outcome and the verifier never
  depend on it, so mandatory checks cannot be reduced away;
* with an operator budget policy bound to the run, a budget breach before
  scheduling fails the task with the distinct reason ``budget_exceeded`` and is
  never retried; unknown usage or an unreadable budget blocks it instead
  (``orchestration.budget_enforcement``, docs/operations/budget-enforcement.md).
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
import hashlib
import json
import logging
import sqlite3
from typing import Any

from synapse.application.context_manifest import ManifestEntry, build_manifest, record_manifest
from synapse.domain.contracts import EventEnvelope, encode_payload, new_id
from synapse.domain.runs import RunChange, RunState
from synapse.application.output_reducer import reduce_output
from synapse.application.routing_dry_run import RoutingDryRunConfig, record_routing, routing_record
from synapse.application.routing_policy import RoutingInput
from synapse.domain.workflow import State
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.orchestration.mock_engine import MockWorkflowEngine, _RunInterrupted
from synapse.orchestration.budget_enforcement import agent_budget_check, bound_policy_identity, write_budget_refusal
from synapse.persistence.agent_tasks import spec_hash
from synapse.persistence.artifact_store import publish_artifact
from synapse.persistence.mock_execution_store import record_verification, set_attempt
from synapse.persistence.run_store import write_run_transition
from synapse.persistence.state_store import StateConflict
from synapse.providers.claude_cli import ClaudeCliProfile, ClaudeCliProvider
from synapse.providers.contracts import ExecutionRequest, ProviderAdapter, ProviderEvent, UsageObservation

_LOG = logging.getLogger(__name__)
MAX_SUMMARY_TEXT_BYTES = 16 * 1024
# reduce_output keeps every failure line even past its budget (up to 1 MiB); the summary blob holds 64 KiB, so the
# forwarded text is clipped hard. The full text is always in the raw artifact.
MAX_FORWARDED_TEXT_BYTES = 32 * 1024
# The serialised summary (json.dumps escapes non-ASCII: up to 3x) must fit the 64 KiB blob bound with headroom.
MAX_SUMMARY_JSON_BYTES = 48 * 1024
_CLIP_MARKER = "\n[clipped: output continues in the raw artifact]\n"
MAX_TOOL_NAMES = 100
_PROVIDER_OUTPUT_CAP = 4 * 1024 * 1024
_LOG_LOCK_WAIT_MS = 100  # audit-only writes (manifest, routing dry run) wait this long for the write lock
RAW_OUTPUT_KIND = "agent_raw_output"
_ATTEMPT_STATE = {"succeeded": "completed", "failed": "failed", "timed_out": "failed",
                  "cancelled": "cancelled", "unknown": "unknown"}
VERIFIER_ID = "builtin.agent.process-outcome-verifier.v1"


async def process_outcome_verifier(task_id: str, candidate: bytes) -> bool:
    """Approve only a well-formed summary whose recorded provider outcome is ``succeeded``.

    This checks the engine-observed process result, NOT the quality of the
    agent's work. Operators needing more inject a stronger verifier.
    """
    try:
        summary = json.loads(candidate)
    except ValueError:
        return False
    return (isinstance(summary, dict) and summary.get("v") == 1 and summary.get("task_id") == task_id
            and summary.get("outcome") == "succeeded")


class AgentProviderRouter:
    """ProviderAdapter that picks a ``ClaudeCliProvider`` per task spec.

    Tool allowlist and permission mode are profile-level in the adapter, so one
    provider is built (and cached) for each distinct narrowed combination via
    ``dataclasses.replace`` on the operator profile, which re-runs its validation.
    """

    def __init__(self, profile: ClaudeCliProfile, specs: Mapping[str, Mapping[str, Any]],
                 factory: Callable[[ClaudeCliProfile], ProviderAdapter] = ClaudeCliProvider,
                 profile_resolver: Callable[[str], ClaudeCliProfile] | None = None):
        # profile_resolver: registry profile id -> the ClaudeCliProfile to spawn with (never invents one).
        self._profile, self._specs, self._factory = profile, specs, factory
        self._resolver = profile_resolver
        self._providers: dict[tuple, ProviderAdapter] = {}
        self._owner: dict[str, ProviderAdapter] = {}
        self.handles: dict[str, str] = {}
        self.start_failed: set[str] = set()

    async def probe(self, profile_id):
        return await next(iter(self._providers.values())).probe(profile_id)

    async def capabilities(self, profile_id):
        return await next(iter(self._providers.values())).capabilities(profile_id)

    async def start(self, request: ExecutionRequest) -> str:
        spec = self._specs[request.task_id]
        named = spec.get("profile_id")
        key = (named, tuple(spec["allowed_tools"]), spec["permission_mode"])
        try:
            provider = self._providers.get(key)
            if provider is None:
                base = self._profile
                if named is not None:
                    if self._resolver is None:
                        raise ValueError("no resolver for a named profile")  # fail closed: never the default
                    base = self._resolver(named)
                provider = self._providers[key] = self._factory(
                    replace(base, allowed_tools=key[1], permission_mode=key[2]))
            handle = await provider.start(request)
        except BaseException:
            self.start_failed.add(request.task_id)  # provider.start never spawns: known no process
            raise
        self._owner[handle] = provider
        self.handles[request.task_id] = handle
        return handle

    def stream(self, handle):
        return self._owner[handle].stream(handle)

    async def cancel(self, handle):
        await self._owner[handle].cancel(handle)

    async def usage(self, handle) -> UsageObservation | None:
        return await self._owner[handle].usage(handle)


def _serialize_summary(summary: dict) -> bytes:
    return json.dumps(summary, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fit_summary(summary: dict) -> dict:
    """Shrink ``output_text`` until the SERIALISED summary fits; measured on the JSON, not on the text."""
    if len(_serialize_summary(summary)) <= MAX_SUMMARY_JSON_BYTES:
        return summary
    fitted = dict(summary, clipped=True, output_truncated=True)
    text = fitted.get("output_text")
    text = text if isinstance(text, str) else ""
    keep = len(text)
    while keep > 0:
        fitted["output_text"] = text[:keep].rstrip() + _CLIP_MARKER
        size = len(_serialize_summary(fitted))
        if size <= MAX_SUMMARY_JSON_BYTES:
            return fitted
        # shrink in proportion to the overshoot (escaping makes bytes-per-char uneven), always making progress
        keep = min(keep - 1, int(keep * MAX_SUMMARY_JSON_BYTES / size * 0.95))
    fitted["output_text"] = _CLIP_MARKER
    return fitted


def _manifest_head(head: object) -> str | None:
    """A 40-hex worktree head, lowercased; anything else is recorded as unknown (None) so the manifest is kept."""
    if isinstance(head, str) and len(head) == 40:
        lowered = head.lower()
        if all(c in "0123456789abcdef" for c in lowered):
            return lowered
    return None


def _clip_text(text: str) -> tuple[str, bool]:
    """Hard cap on forwarded text, cut on a UTF-8 boundary, with a marker. Returns (text, clipped)."""
    data = text.encode("utf-8")
    if len(data) <= MAX_FORWARDED_TEXT_BYTES:
        return text, False
    room = MAX_FORWARDED_TEXT_BYTES - len(_CLIP_MARKER.encode("utf-8"))
    return data[:room].decode("utf-8", "ignore") + _CLIP_MARKER, True


class AgentWorkflowEngine(MockWorkflowEngine):
    """Run tasks with a real provider; see module docstring for the policy."""

    _provider_type = AgentProviderRouter
    _profile_provider = "claude"
    _profile_limits_check = None  # set per instance; None keeps the pre-limits behaviour
    _principal = "agent-engine"
    # Unbudgeted unless the run has a bound policy (ADR 020); fails closed if the budget cannot be read. A breach
    # (budget_exceeded) fails the task; usage_unknown / budget_check_failed block it. See budget_enforcement.
    _budget_check = staticmethod(agent_budget_check)
    _budget_block_writer = staticmethod(write_budget_refusal)

    def __init__(self, connection: sqlite3.Connection, router: AgentProviderRouter,
                 blobs: ArtifactBlobStore, specs: Mapping[str, Mapping[str, Any]],
                 prompts: Callable[[str], bytes], *,
                 verify: Callable[[str, bytes], Awaitable[bool]] = process_outcome_verifier,
                 verifier_id: str = VERIFIER_ID, max_concurrency: int = 2, approval_requester=None,
                 worktree_check: Callable[[str, str, str], tuple[str, bool]] | None = None,
                 profile_check: Callable[[str], str | None] | None = None,
                 profile_limits_check: Callable[[Mapping[str, Any]], str | None] | None = None,
                 raw_blobs: ArtifactBlobStore | None = None,
                 routing_dry_run: RoutingDryRunConfig | None = RoutingDryRunConfig()):
        # max_attempts=2: a gated task spends one attempt on the approval wait.
        super().__init__(connection, router, blobs, verify, verifier_id=verifier_id,
                         max_concurrency=max_concurrency, max_attempts=2, approval_requester=approval_requester)
        self.router, self.specs, self._prompts = router, specs, prompts
        self._worktree_check = worktree_check
        self._profile_check = profile_check  # profile_id -> refusal reason or None; only asked for named profiles
        # spec -> refusal reason or None: the recorded tools, mode and timeout must still fit the named profile
        self._profile_limits_check = profile_limits_check
        # Raw provider output may exceed the summary blob bound; same directory, larger limit.
        self._routing_dry_run = routing_dry_run  # None disables the log; it never changes what runs
        self.raw_blobs = raw_blobs if raw_blobs is not None else blobs.with_max_bytes(_PROVIDER_OUTPUT_CAP)

    def _profile_for(self, run_id: str, task_id: str, default_profile_id: str) -> str:
        """A task that named a registry profile claims capacity under it (its own cap, ADR 016).

        If that profile is unknown or disabled the task claims under the run's profile instead, so it is not
        stuck unclaimable; ``_execute`` then refuses it before any spawn via ``_profile_problem``.
        """
        spec = self.specs.get(task_id)
        named = None if spec is None else spec.get("profile_id")
        if named is None or self._profile_problem(spec) is not None:
            return default_profile_id
        return named

    def _record_profile_selected(self, run_id: str, task_id: str, attempt_id: str, profile_id: str,
                                 requested: str | None) -> None:
        """Audit which profile an attempt runs under; recorded before the spawn, fail closed if it cannot be."""
        row = self.db.execute("SELECT provider,config_ref FROM profiles WHERE id=?", (profile_id,)).fetchone()
        ref = row[1] if row is not None and isinstance(row[1], str) else ""
        event = EventEnvelope.create(
            aggregate_type="task_attempt", aggregate_id=attempt_id, event_type="agent.profile_selected",
            payload={"task_id": task_id, "profile_id": profile_id, "provider": row[0] if row else None,
                     "model": None,  # the adapter does not report a model; never claim one
                     "profile_sha256": ref[len("registry:"):] if ref.startswith("registry:") else None,
                     "requested": requested, "fell_back_from": None,
                     "reason": "requested" if requested else "default"})
        with self._transaction():
            self.db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                            (event.id, event.aggregate_type, attempt_id, run_id, event.event_type, event.schema_version,
                             encode_payload(event.payload), event.occurred_at))
            self.db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))

    def _profile_problem(self, spec: Mapping[str, Any]) -> str | None:
        """Fail-closed recheck just before spawn of the profile a task was queued under (absent: default)."""
        profile_id = spec.get("profile_id")
        if profile_id is None:
            return None
        if self._profile_check is None:
            return "profile_check_unavailable"
        try:
            result = self._profile_check(profile_id)
        except Exception:
            return "profile_check_failed"
        if result is not None and not (isinstance(result, str) and result):
            return "profile_check_invalid"  # any other value (False, '', ...) still refuses
        if result is not None:
            return result
        if self._profile_limits_check is None:
            return None  # legacy callers without a limits check keep the old behaviour
        try:
            limits = self._profile_limits_check(spec)
        except Exception:
            return "profile_limits_check_failed"
        if limits is None or (isinstance(limits, str) and limits):
            return limits
        return "profile_limits_check_invalid"

    async def _worktree_problem(self, run_id: str, task_id: str, path: str, expected_head: str) -> str | None:
        """Fail-closed recheck just before spawn: HEAD must be the recorded one and the tree clean."""
        if self._worktree_check is None:
            return "worktree_check_unavailable"
        try:
            head, clean = await asyncio.to_thread(self._worktree_check, run_id, task_id, path)
        except Exception:
            return "worktree_check_failed"
        if head != expected_head:
            return "worktree_head_changed_since_approval"
        if not clean:
            return "worktree_dirty_since_approval"
        return None

    def _retryable(self, attempt_count: int) -> bool:
        return False

    def _block_for_budget(self, run_id: str, task_id: str, version: int, profile_id: str, denied) -> None:
        """A budget refusal at claim or park: a breach fails the READY task, other reasons block it; nothing is
        retried (``budget_enforcement.write_budget_refusal``). A lost race leaves it READY for the next pass."""
        try:
            with self._transaction():
                self._check_running(run_id)
                self._budget_block_writer(self.db, principal=self._principal, run_id=run_id, task_id=task_id,
                                          version=version, before=State.READY, profile_id=profile_id, denied=denied,
                                          max_running=self.max_concurrency)
        except StateConflict:
            pass

    async def run(self, run_id: str, profile_id: str, scenarios, approval_steps=None) -> str:
        outcome = await super().run(run_id, profile_id, scenarios, approval_steps)
        if outcome == "blocked" and not self._halt and self._finish_stranded(run_id):
            return RunState.FAILED.value
        return outcome

    def _finish_stranded(self, run_id: str) -> bool:
        """End a run whose remaining tasks can never run because a prerequisite failed or was cancelled.

        A FAILED or CANCELLED prerequisite is unambiguous (its dependents need every prerequisite
        COMPLETED), so those dependents are cancelled and the run fails instead of going to
        ``recovering``. Anything ambiguous keeps the old behaviour: a task that is blocked, awaiting
        approval or ready, or ANY attempt that is not finished (``unknown`` included), returns False
        and the caller routes the run to recovery for an operator. Nothing is ever re-spawned here.
        """
        db = self.db
        if db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone() != ("running",):
            return False
        if db.execute("SELECT 1 FROM task_attempts a JOIN tasks t ON t.id=a.task_id WHERE t.run_id=? "
                      "AND a.state NOT IN ('completed','failed','cancelled') LIMIT 1", (run_id,)).fetchone():
            return False
        tasks = {t: (st, ver) for t, st, ver in db.execute("SELECT id,state,state_version FROM tasks WHERE run_id=?", (run_id,))}
        deps: dict[str, list[str]] = {}
        for task_id, dep in db.execute("SELECT d.task_id,d.depends_on_id FROM task_dependencies d JOIN tasks t ON t.id=d.task_id WHERE t.run_id=?", (run_id,)):
            deps.setdefault(task_id, []).append(dep)
        dead = {t for t, (st, _) in tasks.items() if st in (State.FAILED, State.CANCELLED)}
        doomed: set[str] = set()
        changed = True
        while changed:  # a waiting task is doomed once any prerequisite is dead or doomed
            changed = False
            for task_id, (st, _) in tasks.items():
                if st == State.WAITING_DEPENDENCIES and task_id not in doomed and any(d in dead or d in doomed for d in deps.get(task_id, ())):
                    doomed.add(task_id)
                    changed = True
        unfinished = {t for t, (st, _) in tasks.items() if st not in (State.COMPLETED, State.FAILED, State.CANCELLED)}
        if not doomed or unfinished != doomed:
            return False
        with self._transaction():
            self._check_running(run_id)
            for task_id in sorted(doomed):
                self._change(task_id, State.WAITING_DEPENDENCIES, State.CANCELLED, tasks[task_id][1])
            run = db.execute("SELECT state,state_version FROM runs WHERE id=?", (run_id,)).fetchone()
            write_run_transition(db, RunChange(run_id, RunState(run[0]), RunState.FAILED, run[1]), principal=self._principal)
        return True

    def _usage_event(self, run_id: str, task_id: str, attempt_id: str, usage: UsageObservation, profile_id: str) -> None:
        """Record provider-reported usage inside the caller's transaction, the one that makes the attempt terminal.

        The event and the append-only ledger row commit together with the terminal attempt state, so a budget
        check never sees a finished attempt whose usage is still on its way (it would read as unknown). The write
        runs under a savepoint: if it fails it is logged and rolled back alone, never the attempt's outcome.
        Nothing is recorded when the provider reported nothing usable (unknown is never stored as 0).
        ``confidence='reported'``: provider-reported, not independently measured.
        """
        if not self.db.in_transaction:
            raise RuntimeError("usage is recorded in the attempt's terminal transaction")
        known = (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens, usage.cost_usd)
        if all(v is None for v in known):
            return
        self.db.execute("SAVEPOINT usage_record")
        try:
            event = EventEnvelope.create(aggregate_type="task_attempt", aggregate_id=attempt_id,
                event_type="agent.usage_observed",
                payload={"task_id": task_id, "input_tokens": usage.input_tokens,
                         "output_tokens": usage.output_tokens, "cache_read_tokens": usage.cache_read_tokens,
                         "cache_write_tokens": usage.cache_write_tokens, "cost_usd": usage.cost_usd,
                         "source": usage.source})
            self.db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                            (event.id, event.aggregate_type, attempt_id, run_id, event.event_type, event.schema_version,
                             encode_payload(event.payload), event.occurred_at))
            self.db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
            self.db.execute("INSERT INTO usage_observations(id,attempt_id,profile_id,source,input_tokens,output_tokens,"
                            "cache_read_tokens,cache_write_tokens,cost_usd,confidence,observed_at,source_event_id) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (new_id(), attempt_id, profile_id, usage.source, usage.input_tokens, usage.output_tokens,
                             usage.cache_read_tokens, usage.cache_write_tokens, usage.cost_usd, "reported",
                             event.occurred_at, event.id))
        except Exception:
            self.db.execute("ROLLBACK TO SAVEPOINT usage_record")
            self.db.execute("RELEASE SAVEPOINT usage_record")
            _LOG.exception("Could not record usage for task %s", task_id)
            return
        self.db.execute("RELEASE SAVEPOINT usage_record")

    def _publish_summary(self, run_id: str, task_id: str, attempt_id: str, summary: dict) -> tuple[str, dict]:
        artifact_id = new_id()
        data = _serialize_summary(_fit_summary(summary))
        ref = publish_artifact(self.db, self.blobs, chunks=[data], artifact_id=artifact_id,
            media_type="application/json",
            provenance={"run_id": run_id, "task_id": task_id, "attempt_id": attempt_id, "kind": "agent_summary"},
            principal=self._principal, idempotency_key=attempt_id).response
        return artifact_id, ref

    async def _store_raw_and_reduce(self, run_id: str, task_id: str, attempt_id: str, raw: bytes,
                                    raw_capped: bool) -> dict[str, Any]:
        """Store ``raw`` as an artifact and return the reduction fields for the summary.

        The raw artifact is published before the summary so the summary can cite it.
        """
        # The reducer treats a NUL byte as binary and would replace the whole text, hiding failure lines. Reduce a
        # copy with NUL replaced by U+FFFD; the stored artifact and the digest are always of the real bytes.
        nul_replaced = b"\x00" in raw
        scrubbed = raw.replace(b"\x00", "\ufffd".encode()) if nul_replaced else raw
        reduced = await asyncio.to_thread(reduce_output, scrubbed, max_bytes=MAX_SUMMARY_TEXT_BYTES)
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        text, clipped = _clip_text(reduced.reduced)
        artifact_id = None
        if raw:
            artifact_id = new_id()
            ref = publish_artifact(self.db, self.raw_blobs, chunks=[raw], artifact_id=artifact_id,
                media_type="text/plain; charset=utf-8",
                provenance={"run_id": run_id, "task_id": task_id, "attempt_id": attempt_id, "kind": RAW_OUTPUT_KIND},
                principal=self._principal, idempotency_key=attempt_id + ":raw").response
            if ref["sha256"] != raw_sha256:
                raise RuntimeError("Stored raw output does not match its reduction hash")
        return {"output_text": text, "output_truncated": bool(clipped or reduced.truncated or reduced.dropped_lines or reduced.rules_applied or raw_capped),
                "clipped": clipped, "nul_replaced": nul_replaced,
                "raw_sha256": raw_sha256, "raw_artifact_id": artifact_id,
                "raw_complete": not raw_capped,
                "reduction": {"rules": list(reduced.rules_applied), "dropped_lines": reduced.dropped_lines,
                              "kept_failures": len(reduced.kept_failures), "over_budget": reduced.over_budget}}

    @contextlib.contextmanager
    def _best_effort_transaction(self):
        """A transaction for audit-only writes: waits at most ``_LOG_LOCK_WAIT_MS`` for the write lock,
        so a busy database costs a lost log row, not seconds of a blocked event loop."""
        previous = self.db.execute("PRAGMA busy_timeout").fetchone()[0]
        self.db.execute(f"PRAGMA busy_timeout={_LOG_LOCK_WAIT_MS}")
        try:
            with self._transaction():
                yield
        finally:
            self.db.execute(f"PRAGMA busy_timeout={int(previous)}")

    def _record_manifest(self, run_id: str, task_id: str, attempt_id: str, spec: Mapping[str, Any]) -> None:
        """Record (never act on) the daemon-owned context for this attempt (ADR 017 D5, record only).

        A failure is logged and does not change the run: the manifest is an audit and cache-key input,
        not a gate. Unknown facts are recorded as unknown: the tree cleanliness is not checked here.
        """
        try:
            head = spec.get("worktree_head")
            entries = [ManifestEntry("spec", task_id, spec_hash(dict(spec))),
                       ManifestEntry("artifact", str(spec["prompt_artifact_id"]), spec["prompt_sha256"], spec.get("prompt_bytes"))]
            manifest = build_manifest(task_id, repo_head=_manifest_head(head),
                                      worktree_clean=None, entries=entries)
            with self._best_effort_transaction():
                record_manifest(self.db, run_id=run_id, attempt_id=attempt_id, manifest=manifest)
        except Exception:
            _LOG.exception("Could not record the context manifest for task %s", task_id)

    def _record_routing(self, run_id: str, task_id: str, attempt_id: str, spec: Mapping[str, Any], attempt_no: int) -> None:
        """Log what the routing rules would pick for this attempt (M5.5, dry run; nothing is applied).

        Unknown signals stay unknown: the spec carries no role, task kind or file count, so
        ``is_review`` is unknown and the attempt counts as protected (it can never be downgraded).
        A gated task spends its first attempt on the approval wait, so that attempt is not a retry.
        A failure is logged and never affects the run.
        """
        if self._routing_dry_run is None:
            return
        try:
            gated = spec.get("approval_step") is not None
            record = routing_record(self._routing_dry_run, RoutingInput(
                prompt_bytes=spec.get("prompt_bytes"), has_approval_gate=gated,
                retry_no=max(0, attempt_no - 1 - (1 if gated else 0))))
            with self._best_effort_transaction():
                record_routing(self.db, run_id=run_id, attempt_id=attempt_id, record=record)
        except Exception:
            _LOG.exception("Could not record the routing dry run for task %s", task_id)

    def _record_budget(self, run_id: str, task_id: str, attempt_id: str) -> None:
        """Record which bound budget policy version this spawned attempt ran under (M5.2, record only).

        Nothing is written for an unbudgeted run. The decision itself was taken (fail closed) at claim time;
        a failure here is logged and never affects the run.
        """
        try:
            identity = bound_policy_identity(self.db, run_id)
            if identity is None:
                return
            event = EventEnvelope.create(
                aggregate_type="task_attempt", aggregate_id=attempt_id, event_type="budget.policy_applied",
                payload={"task_id": task_id, "policy_version": identity[0], "policy_sha256": identity[1]})
            with self._best_effort_transaction():
                self.db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                                (event.id, event.aggregate_type, attempt_id, run_id, event.event_type, event.schema_version,
                                 encode_payload(event.payload), event.occurred_at))
                self.db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
        except Exception:
            _LOG.exception("Could not record the budget policy version for task %s", task_id)

    async def _execute(self, run_id: str, task_id: str, attempt_id: str, profile_id: str, scenario: str,
                       version: int, attempt_no: int) -> None:
        spec = self.specs[task_id]
        raw, texts_total, raw_capped = bytearray(), 0, False
        tools: list[str] = []
        tool_count = 0
        terminal: ProviderEvent | None = None

        async def emit(event: ProviderEvent) -> None:
            nonlocal terminal, texts_total, raw_capped, tool_count
            if terminal is not None:
                raise RuntimeError("Provider emitted after terminal outcome")
            if event.kind == "output" and isinstance(event.text, str):
                data = event.text.encode("utf-8")
                if not data.endswith(b"\n"):
                    data += b"\n"  # raw text is the output events, one per line
                texts_total += len(data)
                room = _PROVIDER_OUTPUT_CAP - len(raw)
                if room < len(data):
                    raw_capped = True  # the stored raw is a prefix; raw_complete=false records that
                raw.extend(data[:max(room, 0)])
            elif event.kind == "tool_use" and isinstance(event.text, str):
                tool_count += 1
                if len(tools) < MAX_TOOL_NAMES:
                    tools.append(event.text[:128])
            elif event.kind in ("succeeded", "failed", "timed_out", "cancelled", "unknown"):
                terminal = event
            elif event.kind != "started":
                raise ValueError("Unexpected provider event")

        try:
            self._check_running(run_id)
            problem = self._profile_problem(spec)
            if problem is None and (spec.get("approval_step") is not None or spec.get("worktree_head") is not None):
                # A gated task must carry a recorded HEAD; a missing one fails closed (never skips the recheck).
                problem = (await self._worktree_problem(run_id, task_id, spec["worktree"], spec["worktree_head"])
                           if isinstance(spec.get("worktree_head"), str) else "worktree_head_missing")
            named = spec.get("profile_id")
            if problem is None and named is not None and profile_id != named:
                # Capacity, attempt row and budget were charged to another row than the one we would spawn
                # under (e.g. the profile was disabled when the task was claimed, enabled again since).
                problem = "profile_assignment_mismatch"
            if problem is None:
                try:
                    self._record_profile_selected(run_id, task_id, attempt_id, profile_id, spec.get("profile_id"))
                except Exception:
                    _LOG.exception("Could not record the profile selection for task %s", task_id)
                    problem = "profile_selection_not_recorded"
            if problem is not None:
                terminal = ProviderEvent("failed", problem)  # refused before any spawn
            else:
                request = ExecutionRequest(profile_id, task_id, {
                    "prompt": self._prompts(task_id).decode("utf-8"), "worktree": spec["worktree"],
                    "timeout_seconds": spec["timeout_seconds"], "max_output_bytes": _PROVIDER_OUTPUT_CAP})
                try:
                    await self.provider.execute(request, emit)
                except BaseException:
                    if task_id in self.router.start_failed and terminal is None:
                        terminal = ProviderEvent("failed", "start_rejected")  # no process was ever spawned
                    else:
                        raise
                finally:
                    # Audit logs never delay a spawn: written after the provider returns, from spec facts
                    # fixed before the run, under a short lock wait (see _best_effort_transaction).
                    self._record_manifest(run_id, task_id, attempt_id, spec)
                    self._record_routing(run_id, task_id, attempt_id, spec, attempt_no)
                    self._record_budget(run_id, task_id, attempt_id)
            if terminal is None:
                terminal = ProviderEvent("unknown", "no_terminal_event")
            outcome, detail = terminal.kind, terminal.text
            usage = None
            handle = self.router.handles.get(task_id)
            if handle is not None:
                try:
                    usage = await self.router.usage(handle)
                except Exception:
                    _LOG.warning("Usage observation unavailable for task %s", task_id)
            if outcome == "unknown":
                with self._transaction():
                    self._check_running(run_id)
                    set_attempt(self.db, attempt_id=attempt_id, before="running", after="unknown", run_id=run_id, task_id=task_id)
                    if usage is not None:
                        self._usage_event(run_id, task_id, attempt_id, usage, profile_id)
                    self._change(task_id, State.RUNNING, State.BLOCKED, version)
            else:
                target = {"succeeded": State.VERIFYING, "cancelled": State.CANCELLED}.get(outcome, State.FAILED)
                with self._transaction():
                    self._check_running(run_id)
                    set_attempt(self.db, attempt_id=attempt_id, before="running", after=_ATTEMPT_STATE[outcome], run_id=run_id, task_id=task_id)
                    if usage is not None:
                        self._usage_event(run_id, task_id, attempt_id, usage, profile_id)
                    self._change(task_id, State.RUNNING, target, version)
            try:
                reduction = await self._store_raw_and_reduce(run_id, task_id, attempt_id, bytes(raw), raw_capped)
            except Exception:
                if outcome == "succeeded":
                    raise  # fail closed: a success whose raw output cannot be kept is blocked for an operator
                _LOG.exception("Could not store raw output for task %s", task_id)
                cut = bytes(raw[:MAX_SUMMARY_TEXT_BYTES]).decode("utf-8", "ignore").replace("\x00", "\ufffd")
                reduction = {"output_text": cut, "output_truncated": True, "clipped": False, "nul_replaced": b"\x00" in raw,
                             "raw_sha256": None,
                             "raw_artifact_id": None, "raw_complete": False, "reduction": None}
            summary = {"v": 1, "run_id": run_id, "task_id": task_id, "attempt_id": attempt_id, "outcome": outcome,
                       "detail": detail if isinstance(detail, str) else None,
                       "output_bytes": texts_total, **reduction,
                       "tool_names": tools, "tool_use_count": tool_count,
                       "usage": None if usage is None else {"input_tokens": usage.input_tokens,
                                                            "output_tokens": usage.output_tokens, "source": usage.source}}
            if outcome != "succeeded":
                try:
                    self._publish_summary(run_id, task_id, attempt_id, summary)
                except Exception:
                    _LOG.exception("Could not store summary for task %s", task_id)
                return
            artifact_id, ref = self._publish_summary(run_id, task_id, attempt_id, summary)
            candidate = self.blobs.read(ref["sha256"])
            approved = await self.verify(task_id, candidate)
            if type(approved) is not bool:
                raise ValueError("Verifier must return bool")
            with self._transaction():
                self._check_running(run_id)
                record_verification(self.db, run_id=run_id, task_id=task_id, task_version=version + 1,
                    candidate_sha256=ref["sha256"], artifact_id=artifact_id, verifier_id=self.verifier_id, approved=approved)
        except BaseException as exc:
            state = self.db.execute("SELECT state,state_version FROM tasks WHERE id=?", (task_id,)).fetchone()
            with self._transaction():
                if state[0] == State.RUNNING:
                    set_attempt(self.db, attempt_id=attempt_id, before="running", after="unknown", run_id=run_id, task_id=task_id)
                    self._change(task_id, State.RUNNING, State.BLOCKED, state[1])
                elif state[0] == State.VERIFYING:
                    self._change(task_id, State.VERIFYING, State.BLOCKED, state[1])
            if not isinstance(exc, _RunInterrupted):
                raise
