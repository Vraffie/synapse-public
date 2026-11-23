"""Daemon-owned execution of tasks with a REAL provider (Claude Code CLI).

Runtime-level only: no HTTP or CLI surface calls this yet. It is a sibling of
``MockRuntime`` and shares its machinery (claim, attempts, approval gates,
idempotent start, durable cancellation intent, conservative restart recovery),
but never re-spawns a process on its own: a task whose provider outcome is
unknown stays blocked for an operator. See docs/operations/agent-runtime.md.

Run exclusion piggybacks on ``mock_run_config``: an agent run owns a row there
(with the built-in agent profile), so neither runtime can start while the other
has a running run. ``mock_scenarios.mock_kind`` returns None for such a run, so
every mock-only guard (mock cancel/resume/recovery) refuses to touch it.
"""
from __future__ import annotations

import asyncio
import functools
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
import sqlite3
from collections.abc import Awaitable, Callable

from synapse.daemon.agent_config import ApprovalPolicy
from synapse.daemon.agent_profiles import REGISTRY_REF_PREFIX, mode_within
from synapse.daemon.profile_registry import Cooldowns
from synapse.application.approval_requests import DaemonApprovalRequester
from synapse.application.budget_policy import BudgetPolicy
from synapse.domain.contracts import EventEnvelope, RunId, encode_payload, new_id, validate_id
from synapse.domain.runs import RunChange, RunState
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.infrastructure.spawn_journal import FenceReport, SpawnJournal
from synapse.orchestration.agent_engine import (
    AgentProviderRouter, AgentWorkflowEngine, process_outcome_verifier,
)
from synapse.orchestration.approval_gates import ApprovalStepSpec
from synapse.persistence import agent_tasks as at
from synapse.persistence import mock_scenarios as ms
from synapse.persistence.agent_abandon import abandon_agent_run
from synapse.persistence.agent_cancellation import (
    cancellation_requested, finalize_agent_cancellation, request_agent_cancellation,
)
from synapse.persistence.budget_binding import bind_run
from synapse.persistence.idempotency import execute_idempotent
from synapse.persistence.mock_recovery import recover_mock_run
from synapse.persistence.run_store import write_run_transition
from synapse.persistence.state_store import StateConflict
from synapse.providers.claude_cli import ClaudeCliProfile, ClaudeCliProvider

_LOG = logging.getLogger(__name__)


class AgentDisabled(RuntimeError):
    """No enabled operator Claude CLI profile: real agents are refused."""


class AgentRuntime:
    def __init__(self, connection: sqlite3.Connection, blob_directory, *, profile: ClaudeCliProfile | None = None,
                 verify: Callable[[str, bytes], Awaitable[bool]] = process_outcome_verifier,
                 max_concurrency: int = 2, provider_factory=ClaudeCliProvider,
                 approval_policy: ApprovalPolicy | None = None,
                 worktree_check: Callable[[str, str, str], tuple[str, bool]] | None = None,
                 budget_policy: BudgetPolicy | None = None, registry=None,
                 spawn_journal: SpawnJournal | None = None):
        if profile is not None and not isinstance(profile, ClaudeCliProfile):
            raise TypeError("profile must be a ClaudeCliProfile")
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 6:
            raise ValueError("max_concurrency must be between 1 and 6")
        self.db = connection
        self.profile, self._verify, self._max_concurrency = profile, verify, max_concurrency
        self._journal = spawn_journal
        self.fence_report: FenceReport | None = None  # result of the startup fence, None until it ran
        # With a journal every real spawn is recorded durably so a restart can prove or enforce that none survives.
        self._factory = provider_factory if spawn_journal is None else functools.partial(provider_factory, spawn_journal=spawn_journal)
        self.registry = registry  # ProfileRegistry or None: named profiles resolve here and nowhere else
        self.cooldowns = Cooldowns()  # operator-set, in memory: a restart forgets them (ADR 016 decision 6)
        self.approval_policy = approval_policy or ApprovalPolicy()
        self.budget_policy = budget_policy  # bound as an immutable copy to each run at start (ADR 020 D2)
        # (run_id, task_id) -> (HEAD sha, tree is clean); blocking, called in a worker thread.
        self._worktree_check = worktree_check
        self.blobs = ArtifactBlobStore(blob_directory, max_bytes=65536)
        self._active: asyncio.Task | None = None
        self._active_run: str | None = None
        self._cancelling: asyncio.Task | None = None
        self._engine: AgentWorkflowEngine | None = None
        self._cancel_requested: str | None = None
        self._pending_redrive: str | None = None
        self._park_poll = 2.0
        self._cancel_timeout = 20.0

    # --- start -------------------------------------------------------------
    def _busy(self) -> bool:
        return ((self._active is not None and not self._active.done())
                or (self._cancelling is not None and not self._cancelling.done()))

    def start(self, run_id: str, key: str, task_specs: dict):
        """Validate, store immutably, then run the queued run's tasks with the real provider.

        ``task_specs`` is ``{node: {prompt, worktree, allowed_tools, permission_mode,
        timeout_seconds[, approval_step: {operation, target, policy_version[, ttl_seconds]}]}}``
        and must name exactly the run's nodes. Raises ``AgentDisabled`` without an
        enabled operator profile and ``AgentSpecError`` (a ValueError) for any
        invalid spec, in both cases before any state change. Replays are receipts.
        """
        validate_id(run_id)
        profile = self.profile
        if profile is None or not profile.enabled:
            raise AgentDisabled("Real agent execution requires an enabled operator Claude CLI profile")
        parsed = at.parse_task_specs(task_specs, operator_tools=profile.allowed_tools,
                                     max_timeout=profile.max_timeout_seconds, state_dir=profile.state_dir)
        for spec in parsed.specs.values():
            if spec["approval_step"] is None and self.approval_policy.required_for(spec["permission_mode"], spec["allowed_tools"]):
                raise at.AgentSpecError("operator policy requires an approval_step for this task")
        for data in parsed.prompts.values():
            self.blobs.put([data])  # content-addressed and private; unreferenced until the txn commits
        artifact_ids = {node: new_id() for node in parsed.specs}

        def write(db):
            if self._busy():
                raise StateConflict("Another agent run is active")
            row = db.execute("SELECT state,state_version FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row[0] != "queued":
                raise StateConflict("Run must be queued")
            if db.execute("SELECT 1 FROM mock_run_config c JOIN runs r ON r.id=c.run_id WHERE r.state='running' LIMIT 1").fetchone():
                raise StateConflict("Another run is active")
            at.check_nodes_for_run(db, run_id, parsed.specs)
            existing = db.execute("SELECT provider,enabled,max_concurrency FROM profiles WHERE id=?", (at.AGENT_PROFILE,)).fetchone()
            if existing is None:
                db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,enabled,max_concurrency,created_at) VALUES(?,'claude','Built-in Claude CLI agent',?,1,?,?)",
                           (at.AGENT_PROFILE, "builtin:" + at.AGENT_KIND, self._max_concurrency, datetime.now(timezone.utc).isoformat()))
            elif existing[:2] != ("claude", 1) or existing[2] < 1:
                raise StateConflict("Built-in agent profile is unavailable")
            db.execute("INSERT INTO mock_run_config(run_id,profile_id,fixture) VALUES(?,?,?)", (run_id, at.AGENT_PROFILE, ms.FIXED_KIND))
            tasks = {node: {**spec, "prompt_artifact_id": artifact_ids[node]} for node, spec in parsed.specs.items()}
            at.record_task_specs(db, run_id, tasks, parsed.prompts, artifact_ids, datetime.now(timezone.utc).isoformat())
            if self.budget_policy is not None:
                bind_run(db, run_id, self.budget_policy)
            write_run_transition(db, RunChange(RunId(run_id), RunState.QUEUED, RunState.RUNNING, row[1]), principal="local-operator")
            return {"run_id": run_id, "state": "running", "kind": at.AGENT_KIND}
        result = execute_idempotent(self.db, principal="local-operator", operation="agent.start", key=key,
                                    request={"run_id": run_id, "kind": at.AGENT_KIND, "tasks": parsed.specs}, execute=write)
        if not result.replayed:
            self._launch(run_id)
        return result

    def _launch(self, run_id: str) -> None:
        self._cancel_requested = None
        self._active_run = run_id
        self._active = asyncio.create_task(self._run(run_id))

    # --- abandon -----------------------------------------------------------
    def abandon(self, run_id: str, key: str, expected_run_version: int, assertion: str):
        """Operator ends a ``recovering`` run (failed; tasks cancelled). Never re-spawns anything."""
        if self._active is not None and not self._active.done() and self._active_run == run_id:
            raise StateConflict("The run is still active")
        return abandon_agent_run(self.db, run_id=run_id, expected_run_version=expected_run_version,
                                 assertion=assertion, key=key)

    # --- cancel ------------------------------------------------------------
    def cancel(self, run_id: str, key: str):
        """Persist intent, then kill active agent process groups through the provider.

        The run only becomes ``cancelled`` once every attempt's outcome is
        established; otherwise it is routed to recovery for an operator.
        """
        if self._active is not None and not self._active.done() and self._active_run != run_id:
            raise StateConflict("A different agent run is active")
        result = request_agent_cancellation(self.db, run_id=run_id, key=key)
        if self._cancelling is None or self._cancelling.done():
            self._cancel_requested = run_id
            if self._engine is not None and self._active_run == run_id:
                self._engine.halt()
            self._cancelling = asyncio.create_task(self._stop_and_finalize(run_id))
        return result

    async def _stop_and_finalize(self, run_id: str):
        try:
            active = self._active
            if active is not None and self._active_run == run_id and not active.done():
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self._cancel_timeout
                engine = self._engine
                if engine is None or not engine._running:
                    active.cancel()  # idle (e.g. sleeping while parked): nothing is in flight
                while not active.done() and loop.time() < deadline:
                    if engine is not None and engine._running:
                        engine.halt()
                        for task_id in tuple(engine._active):
                            await engine.cancel(task_id)  # repeated: covers tasks not yet registered
                    await asyncio.wait({active}, timeout=0.1)
                if not active.done():
                    _LOG.error("Agent cancellation timed out for run %s", run_id)
                    return
                await asyncio.gather(active, return_exceptions=True)
            try:
                finalize_agent_cancellation(self.db, run_id=run_id)
            except StateConflict:
                _LOG.error("Agent run %s has an attempt with no established outcome; routing to recovery", run_id)
                self._safe_recover_run(run_id)
        except Exception:
            _LOG.exception("Agent cancellation incomplete for run %s", run_id)

    # --- execution ---------------------------------------------------------
    def _plan(self, run_id: str):
        profile = self.profile
        if profile is None or not profile.enabled:
            raise AgentDisabled("Real agent execution requires an enabled operator Claude CLI profile")
        specs = at.load_task_specs(self.db, run_id)
        if specs is None or not at.is_agent_run(self.db, run_id):
            raise StateConflict("Recorded agent task specs are not trustworthy")
        by_node = dict((n, t) for t, n in self.db.execute("SELECT id,node_id FROM tasks WHERE run_id=?", (run_id,)))
        task_specs = {by_node[n]: s for n, s in specs.items()}
        for s in task_specs.values():
            if not set(s["allowed_tools"]) <= set(profile.allowed_tools) or s["timeout_seconds"] > profile.max_timeout_seconds:
                raise StateConflict("Recorded task exceeds the current operator profile")
        gates = {t: ApprovalStepSpec(s["approval_step"]["operation"], s["approval_step"]["target"],
                                     s["approval_step"]["candidate_sha256"], s["approval_step"]["policy_version"],
                                     timedelta(seconds=s["approval_step"]["ttl_seconds"]))
                 for t, s in task_specs.items() if s["approval_step"] is not None}
        return task_specs, gates

    def _resolve_profile(self, profile_id: str) -> ClaudeCliProfile:
        """The operator profile with the registry profile's executable; raises for anything not registered.

        Credentials, state dir and sandbox settings stay the operator's (the registry does not model them).
        """
        spec = None if self.registry is None else self.registry.get(profile_id)
        if spec is None or not spec.enabled or spec.provider != "claude" or self.profile is None:
            raise ValueError("profile is not available")
        return replace(self.profile, executable=spec.executable)

    def _profile_limits_problem(self, spec: dict) -> str | None:
        """Spawn-time recheck that a queued task still fits its named profile as the registry now declares it.

        The registry can be narrowed between acceptance and spawn (edit the file, restart). A task or a parked
        gated task accepted under wider tools, mode or timeout must not run with them.
        """
        named = spec.get("profile_id")
        if named is None:
            return None
        declared = None if self.registry is None else self.registry.get(named)
        if declared is None:
            return "profile_unknown"
        if not set(spec.get("allowed_tools", ())) <= set(declared.allowed_tools):
            return "profile_narrowed"
        if not mode_within(spec.get("permission_mode"), declared.permission_mode):
            return "profile_narrowed"
        timeout = spec.get("timeout_seconds")
        if not isinstance(timeout, (int, float)) or timeout > declared.max_timeout_seconds:
            return "profile_narrowed"
        return None

    def _profile_problem(self, profile_id: str) -> str | None:
        """Spawn-time recheck of a named profile against the profiles table (synced from the registry)."""
        row = self.db.execute("SELECT provider,config_ref,enabled FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if row is None:
            return "profile_unknown"
        if row[0] != "claude" or not str(row[1]).startswith(REGISTRY_REF_PREFIX):
            return "profile_not_registry"  # the built-in agent row or a mock profile never satisfies a named profile
        if row[2] != 1:
            return "profile_disabled"
        # An operator cooldown only refuses the task; nothing waits or retries (never an evasion of a limit).
        return "profile_cooling_down" if self.cooldowns.active(profile_id) else None

    def _prompt(self, task_specs):
        def load(task_id: str) -> bytes:
            spec = task_specs[task_id]
            data = self.blobs.read(spec["prompt_sha256"])  # digest-verified by the store
            if len(data) != spec["prompt_bytes"]:
                raise StateConflict("Stored prompt differs from its record")
            return data
        return load

    def _parked_only(self, run_id: str) -> bool:
        """Running run with only parked gates (and tasks waiting on them) and no unresolved attempt."""
        specs = at.load_task_specs(self.db, run_id)
        if not specs or not any(s["approval_step"] for s in specs.values()):
            return False
        run = self.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None or run[0] != "running" or cancellation_requested(self.db, run_id):
            return False
        states = {r[0] for r in self.db.execute("SELECT state FROM tasks WHERE run_id=? AND state NOT IN ('completed','failed','cancelled')", (run_id,))}
        if "awaiting_approval" not in states or not states <= {"awaiting_approval", "waiting_dependencies"}:
            return False
        if self.db.execute("SELECT 1 FROM tasks t JOIN task_attempts a ON a.task_id=t.id WHERE t.run_id=? AND a.state NOT IN ('completed','failed','cancelled') LIMIT 1", (run_id,)).fetchone():
            return False
        return self.db.execute("SELECT 1 FROM tasks t JOIN task_approval_gates g ON g.task_id=t.id WHERE t.run_id=? AND t.state='awaiting_approval' AND g.resolved_at IS NULL LIMIT 1", (run_id,)).fetchone() is not None

    async def _run(self, run_id: str):
        try:
            task_specs, gates = self._plan(run_id)
            router = AgentProviderRouter(self.profile, task_specs, self._factory, self._resolve_profile)
            engine = AgentWorkflowEngine(
                self.db, router, self.blobs, task_specs, self._prompt(task_specs), verify=self._verify,
                max_concurrency=self._max_concurrency,
                approval_requester=DaemonApprovalRequester(self.db) if gates else None,
                worktree_check=self._worktree_check, profile_check=self._profile_problem,
                profile_limits_check=self._profile_limits_problem)
            self._engine = engine
            scenarios = {t: ("agent",) for t in task_specs}
            while True:
                outcome = await engine.run(run_id, at.AGENT_PROFILE, scenarios, gates or None)
                if outcome != "blocked":
                    break
                if self._cancel_requested == run_id or cancellation_requested(self.db, run_id):
                    return  # the cancelling task finalizes
                if gates and self._parked_only(run_id):
                    await asyncio.sleep(self._park_poll)
                    continue
                self._safe_recover_run(run_id)
                break
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.exception("Agent execution failed for run %s", run_id)
            if not cancellation_requested(self.db, run_id):
                self._safe_recover_run(run_id)
        finally:
            self._engine = None

    # --- recovery ----------------------------------------------------------
    def _safe_recover_run(self, run_id: str):
        try:
            self._recover_run(run_id)
        except Exception:
            _LOG.exception("Agent recovery failed for run %s", run_id)

    def _reconcile(self, run_id: str):
        # A detached child CAN outlive the daemon, so recovery never trusts that nothing runs: in-flight attempts
        # become unknown, nothing is resumed, and an operator ends the run. The startup fence (ADR 023) kills
        # surviving process groups and records what it could not prove in the agent.worker_fence event; its
        # result informs the operator, it does not make anything resumable.
        recover_mock_run(self.db, run_id=run_id, principal="agent-recovery", no_workers_survive=True, provider_kind="claude")

    def _recover_run(self, run_id: str):
        if not at.is_agent_run(self.db, run_id):
            raise StateConflict("Run is not an agent run this daemon understands")
        self._reconcile(run_id)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT state,state_version FROM runs WHERE id=?", (run_id,)).fetchone()
            if row and row[0] == "running":
                write_run_transition(self.db, RunChange(RunId(run_id), RunState.RUNNING, RunState.RECOVERING, row[1]), principal="agent-recovery")
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def recover_interrupted(self):
        """Invoke only after acquiring exclusive daemon ownership at startup.

        Tasks that were in flight become unknown/blocked and the run moves to
        ``recovering``. Nothing is re-spawned. A run left with only parked
        approval gates is re-driven by ``resume_parked`` instead.
        """
        runs = self.db.execute("SELECT run_id FROM mock_run_config WHERE profile_id=?", (at.AGENT_PROFILE,)).fetchall()
        self._fence_prior_workers()
        for (run_id,) in runs:
            try:
                self._audit_fence(run_id)
                if cancellation_requested(self.db, run_id):
                    self._reconcile(run_id)
                    try:
                        finalize_agent_cancellation(self.db, run_id=run_id)
                    except StateConflict:
                        self._recover_run(run_id)
                elif self._pending_redrive is None and self._parked_only(run_id):
                    self._pending_redrive = run_id
                else:
                    self._recover_run(run_id)
            except Exception:
                _LOG.exception("Agent startup recovery failed for run %s", run_id)

    def _fence_prior_workers(self) -> None:
        """Prove or enforce that no agent process of a previous daemon survives (ADR 023), before any recovery.

        Orphans are killed with their whole process group. What cannot be proven dead (a spawn that crashed before
        its pid was recorded, an ambiguous pid, a group that would not die) is reported ``unverified``: recovery
        still marks the attempt unknown and the run recovering, but the audit event says the operator must check.
        """
        if self._journal is None:
            return
        try:
            self.fence_report = self._journal.fence_all()
        except Exception:
            _LOG.exception("Startup worker fence failed; treating every prior worker as unverified")
            self.fence_report = FenceReport(unverified=1)
        report = self.fence_report
        if report.total:
            (_LOG.info if report.all_clear else _LOG.error)(
                "Startup worker fence: %d provably gone, %d killed, %d UNVERIFIED", report.verified_dead,
                report.killed, report.unverified)

    def _audit_fence(self, run_id: str) -> None:
        """Record the fence result on every run that had unresolved work, so abandon decisions can rely on it."""
        report = self.fence_report
        if report is None or not report.total:
            return
        try:
            unresolved = self.db.execute(
                "SELECT 1 FROM task_attempts a JOIN tasks t ON t.id=a.task_id WHERE t.run_id=? "
                "AND a.state IN ('created','starting','running') LIMIT 1", (run_id,)).fetchone()
            if unresolved is None:
                return
            event = EventEnvelope.create(aggregate_type="run", aggregate_id=run_id, event_type="agent.worker_fence",
                                         payload={"run_id": run_id, "principal": "agent-recovery",
                                                  "verified_dead": report.verified_dead, "killed": report.killed,
                                                  "unverified": report.unverified, "all_clear": report.all_clear})
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
            _LOG.exception("Worker fence result for run %s could not be recorded", run_id)

    def resume_parked(self):
        """Re-drive the run left parked at startup (call from the daemon's event loop)."""
        run_id, self._pending_redrive = self._pending_redrive, None
        if run_id is None:
            return False
        self._launch(run_id)
        return True
