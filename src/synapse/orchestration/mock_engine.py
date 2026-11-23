"""Offline mock workflow execution using daemon-owned durable repositories."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
import sqlite3

from synapse.application.approval_requests import DaemonApprovalRequester
from synapse.domain.contracts import EventEnvelope, encode_payload, new_id, validate_id
from synapse.persistence.mock_resume import MOCK_MAX_ATTEMPTS
from synapse.domain.runs import RunChange, RunState
from synapse.domain.workflow import State, StateChange
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.orchestration.approval_gates import ApprovalGateCoordinator, ApprovalStepSpec, ResumeStatus
from synapse.orchestration.worker_supervisor import WorkerSupervisor
from synapse.persistence.approval_binding import ApprovalNotGranted
from synapse.persistence.artifact_store import publish_artifact
from synapse.persistence.attempt_store import reserve_attempt
from synapse.persistence.mock_execution_store import record_verification, set_attempt
from synapse.persistence.run_store import write_run_transition
from synapse.persistence.state_store import StateConflict, write_task_transition
from synapse.persistence.budget_binding import BudgetDenied, write_budget_block
from synapse.persistence.task_claim import claim_task
from synapse.providers.contracts import ExecutionRequest, ProviderEvent
from synapse.providers.mock import MockProvider


class _RunInterrupted(Exception):
    pass


class MockWorkflowEngine:
    """Execute mock tasks; no live process, external side effects or auto-recovery.

    Caller owns the daemon lock, connection, scenario input, and verifier trust.
    The verifier must independently examine the supplied candidate bytes.
    """

    # Extension points for the real-agent sibling engine (agent_engine.py).
    _provider_type: type = MockProvider
    _profile_provider = "mock"
    _principal = "mock-engine"
    _budget_check = None  # (db, task_id, profile_id) -> None, raising to refuse a claim; set by the agent engine

    def __init__(self, connection: sqlite3.Connection, provider: MockProvider, blobs: ArtifactBlobStore,
                 verify: Callable[[str, bytes], Awaitable[bool]], *, verifier_id: str,
                 max_concurrency: int = 6, max_attempts: int = MOCK_MAX_ATTEMPTS, max_output_bytes: int = 65536,
                 approval_requester: DaemonApprovalRequester | None = None):
        if type(provider) is not self._provider_type:
            raise TypeError("Only the deterministic mock provider is supported")
        validate_id(verifier_id)
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if type(max_output_bytes) is not int or max_output_bytes < 0:
            raise ValueError("max_output_bytes must be nonnegative")
        self.db = connection
        self.gates = None if approval_requester is None else ApprovalGateCoordinator(connection, approval_requester, principal=self._principal)
        self.provider = WorkerSupervisor(provider, max_concurrency=max_concurrency)
        self.blobs, self.verify, self.verifier_id = blobs, verify, verifier_id
        self.max_concurrency, self.max_attempts, self.max_output_bytes = max_concurrency, max_attempts, max_output_bytes
        self._running = False
        self._active: dict[str, asyncio.Task] = {}
        self._reservations: dict[str, tuple[str, str]] = {}
        self._halt = False  # stop claiming/resuming; let active work finish

    def halt(self) -> None:
        """Stop starting new work; already active tasks are left to finish."""
        self._halt = True

    def _retryable(self, attempt_count: int) -> bool:
        return attempt_count < self.max_attempts

    @contextmanager
    def _transaction(self):
        if self.db.in_transaction:
            raise RuntimeError("Engine requires an autocommit connection")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def _change(self, task_id: str, before: State, after: State, version: int) -> int:
        return write_task_transition(self.db, StateChange(task_id, before, after, version), principal=self._principal)

    def _profile_for(self, run_id: str, task_id: str, default_profile_id: str) -> str:
        """The profile a task claims capacity under and runs with; the run's profile unless overridden."""
        return default_profile_id

    def _assigned_profile(self, task_id: str) -> str | None:
        """The profile the task's capacity was actually claimed under (authoritative after a park/resume)."""
        row = self.db.execute("SELECT assigned_profile_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        return None if row is None else row[0]

    def _block_for_budget(self, run_id: str, task_id: str, version: int, profile_id: str, denied: BudgetDenied) -> None:
        """Record a refused claim (ADR 020 D4): the claim rolled back, so nothing was assigned; the task goes
        READY -> BLOCKED (no silent downgrade, no retry) with an event and an audit row naming the reason."""
        with self._transaction():
            self._check_running(run_id)
            write_budget_block(self.db, principal=self._principal, run_id=run_id, task_id=task_id, version=version,
                               before=State.READY, profile_id=profile_id, denied=denied)

    def _check_running(self, run_id: str) -> None:
        row = self.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None or row[0] != "running":
            raise _RunInterrupted()

    async def cancel(self, task_id: str) -> bool:
        """Request cancellation of an active mock task; completion remains durable."""
        return await self.provider.cancel(task_id)

    async def run(self, run_id: str, profile_id: str, scenarios: Mapping[str, tuple[str, ...]],
                  approval_steps: Mapping[str, ApprovalStepSpec] | None = None) -> str:
        """Run until done, blocked or interrupted.

        ``approval_steps`` maps task IDs to steps that need a human approval
        before their provider work starts (see ``approval_gates``). Such a task
        is parked and the run reports ``blocked`` until a later ``run`` call
        (e.g. after a restart) finds the approval decided; supply the same specs
        again then. The approval wait consumes one attempt, so ``max_attempts``
        must be at least 2.
        """
        validate_id(run_id)
        validate_id(profile_id)
        steps = dict(approval_steps or {})
        if steps:
            if self.gates is None:
                raise ValueError("approval_steps require an approval_requester")
            if self.max_attempts < 2:
                raise ValueError("approval steps need max_attempts >= 2")
            if not set(steps) <= set(scenarios) or any(type(v) is not ApprovalStepSpec for v in steps.values()):
                raise ValueError("approval_steps must map run tasks to ApprovalStepSpec")
        if self._running:
            raise RuntimeError("Engine is already running")
        profile = self.db.execute("SELECT provider FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if profile is None or profile[0] != self._profile_provider:
            raise ValueError("An existing mock profile is required")
        row = self.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None or row[0] != "running":
            raise StateConflict("Run must be running")
        for task_id, choices in scenarios.items():
            validate_id(task_id)
            if not isinstance(choices, tuple) or not choices or any(not isinstance(x, str) or not x for x in choices):
                raise ValueError("Every task needs a nonempty scenario tuple")
        task_ids = {row[0] for row in self.db.execute("SELECT id FROM tasks WHERE run_id=?", (run_id,))}
        if not task_ids or task_ids != set(scenarios):
            raise ValueError("Scenarios must exactly match the run's tasks")
        self._running = True
        try:
            while True:
                run_state = self.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
                if run_state is None:
                    raise StateConflict("Run disappeared")
                if run_state[0] != "running":
                    return run_state[0]
                # Promote created and dependency-ready tasks only after the
                # database confirms every predecessor completed.
                for task_id, state, version, count in self.db.execute("SELECT id,state,state_version,attempt_count FROM tasks WHERE run_id=? ORDER BY id", (run_id,)).fetchall():
                    if state == State.CREATED:
                        has_deps = self.db.execute("SELECT 1 FROM task_dependencies WHERE task_id=?", (task_id,)).fetchone()
                        with self._transaction():
                            self._check_running(run_id)
                            self._change(task_id, State.CREATED, State.WAITING_DEPENDENCIES if has_deps else State.READY, version)
                    elif state == State.WAITING_DEPENDENCIES:
                        pending = self.db.execute("SELECT 1 FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_id WHERE d.task_id=? AND (t.run_id<>? OR t.state<>?)", (task_id, run_id, State.COMPLETED)).fetchone()
                        if not pending:
                            with self._transaction():
                                self._check_running(run_id)
                                self._change(task_id, State.WAITING_DEPENDENCIES, State.READY, version)
                    elif state in (State.RETRY_WAIT, State.CHANGES_REQUIRED) and self._retryable(count):
                        with self._transaction():
                            self._check_running(run_id)
                            self._change(task_id, State(state), State.READY, version)
                for task_id, spec in steps.items():
                    if self._halt or len(self._active) >= self.max_concurrency:
                        break
                    if self.db.execute("SELECT 1 FROM tasks t JOIN task_approval_gates g ON g.task_id=t.id WHERE t.id=? AND t.state='awaiting_approval' AND g.resolved_at IS NULL", (task_id,)).fetchone() is None:
                        continue
                    resumed = self.gates.resume(run_id=run_id, task_id=task_id, spec=spec, budget_check=self._budget_check)
                    if resumed.status == ResumeStatus.RESUMED:
                        self._reservations[task_id] = (run_id, resumed.attempt_id)
                        self._active[task_id] = asyncio.create_task(self._execute(run_id, task_id, resumed.attempt_id, self._assigned_profile(task_id) or self._profile_for(run_id, task_id, profile_id), self._scenario(scenarios[task_id], resumed.attempt_count - 2), resumed.task_version, resumed.attempt_count))
                for task_id, version, count in self.db.execute("SELECT id,state_version,attempt_count FROM tasks WHERE run_id=? AND state='ready' ORDER BY id", (run_id,)).fetchall():
                    if self._halt or len(self._active) >= self.max_concurrency:
                        break
                    spec = steps.get(task_id)
                    task_profile = self._profile_for(run_id, task_id, profile_id)
                    if spec is not None:
                        if not self.gates.has_gate(task_id):
                            try:
                                self.gates.park(run_id=run_id, task_id=task_id, profile_id=task_profile, spec=spec, max_running=self.max_concurrency, budget_check=self._budget_check)
                            except StateConflict:
                                pass
                            except BudgetDenied as denied:
                                self._block_for_budget(run_id, task_id, version, task_profile, denied)
                            continue
                        try:
                            # A retry after an approved attempt must still be covered.
                            self.gates.verify_gated(run_id, task_id, spec)
                        except ApprovalNotGranted:
                            with self._transaction():
                                self._check_running(run_id)
                                self._change(task_id, State.READY, State.BLOCKED, version)
                            continue
                    attempt_id = new_id()
                    try:
                        with self._transaction():
                            current = self.db.execute("SELECT attempt_count FROM tasks WHERE id=? AND run_id=?", (task_id, run_id)).fetchone()
                            if current is None or current[0] >= self.max_attempts:
                                continue
                            version = claim_task(self.db, task_id=task_id, expected_version=version, profile_id=task_profile, principal=self._principal, max_running=self.max_concurrency, budget_check=self._budget_check)
                            reserve_attempt(self.db, task_id=task_id, attempt_id=attempt_id, expected_task_version=version, principal=self._principal)
                            version += 1
                            self._change(task_id, State.ASSIGNED, State.RUNNING, version)
                            set_attempt(self.db, attempt_id=attempt_id, before="created", after="running", run_id=run_id, task_id=task_id)
                    except StateConflict:
                        continue
                    except BudgetDenied as denied:
                        self._block_for_budget(run_id, task_id, version, task_profile, denied)
                        continue
                    scenario = self._scenario(scenarios[task_id], count - 1 if spec is not None else count)
                    self._reservations[task_id] = (run_id, attempt_id)
                    self._active[task_id] = asyncio.create_task(self._execute(run_id, task_id, attempt_id, task_profile, scenario, version + 1, count + 1))
                if self._active:
                    done, _ = await asyncio.wait(self._active.values(), timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
                    for task_id, future in tuple(self._active.items()):
                        if future in done:
                            del self._active[task_id]
                            await future
                            self._reservations.pop(task_id, None)
                    continue
                rows = self.db.execute("SELECT state FROM tasks WHERE run_id=?", (run_id,)).fetchall()
                states = {r[0] for r in rows}
                if states <= {State.COMPLETED, State.FAILED, State.CANCELLED}:
                    target = RunState.SUCCEEDED if states == {State.COMPLETED} else RunState.CANCELLED if states == {State.CANCELLED} else RunState.FAILED
                    with self._transaction():
                        run = self.db.execute("SELECT state,state_version FROM runs WHERE id=?", (run_id,)).fetchone()
                        if run is None:
                            raise StateConflict("Run disappeared")
                        if run[0] != "running":
                            return run[0]
                        write_run_transition(self.db, RunChange(run_id, RunState(run[0]), target, run[1]), principal=self._principal)
                    return target.value
                return "blocked"
        except _RunInterrupted:
            row = self.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise StateConflict("Run disappeared")
            return row[0]
        finally:
            # Cancelling the outer orchestration coroutine cancels and awaits
            # its workers so they can record unknown outcomes before returning.
            if self._active:
                for future in self._active.values():
                    future.cancel()
                await asyncio.gather(*self._active.values(), return_exceptions=True)
                self._active.clear()
            # A coroutine cancelled before it first runs cannot execute its own
            # exception handler. Reconcile every still-owned reservation after
            # workers stop, and never retry an ambiguous outcome.
            for task_id, (reserved_run, attempt_id) in self._reservations.items():
                with self._transaction():
                    row = self.db.execute("SELECT t.state,t.state_version,a.state FROM tasks t JOIN task_attempts a ON a.task_id=t.id WHERE t.id=? AND t.run_id=? AND a.id=?", (task_id, reserved_run, attempt_id)).fetchone()
                    if row is not None and row[0] == State.RUNNING and row[2] == "running":
                        set_attempt(self.db, attempt_id=attempt_id, before="running", after="unknown", run_id=reserved_run, task_id=task_id)
                        self._change(task_id, State.RUNNING, State.BLOCKED, row[1])
            self._reservations.clear()
            self._running = False

    @staticmethod
    def _scenario(choices: tuple[str, ...], index: int) -> str:
        return choices[max(0, min(index, len(choices) - 1))]

    async def _execute(self, run_id: str, task_id: str, attempt_id: str, profile_id: str, scenario: str, version: int, attempt_no: int) -> None:
        data = bytearray()
        terminal: str | None = None
        async def emit(event: ProviderEvent) -> None:
            nonlocal terminal
            if terminal is not None:
                raise RuntimeError("Provider emitted after terminal outcome")
            if event.kind == "output":
                if not isinstance(event.text, str):
                    raise ValueError("Mock output must be text")
                data.extend(event.text.encode("utf-8"))
                if len(data) > self.max_output_bytes:
                    raise ValueError("Mock output exceeds configured limit")
            elif event.kind in ("succeeded", "failed", "cancelled"):
                terminal = event.kind
            elif event.kind != "started":
                raise ValueError("Unexpected mock provider event")
        try:
            self._check_running(run_id)
            await self.provider.execute(ExecutionRequest(profile_id, task_id, {"scenario": scenario}), emit)
            if terminal is None:
                raise RuntimeError("Mock provider ended without an outcome")
            target = State.VERIFYING if terminal == "succeeded" else State.CANCELLED if terminal == "cancelled" else State.RETRY_WAIT if attempt_no < self.max_attempts else State.FAILED
            with self._transaction():
                self._check_running(run_id)
                set_attempt(self.db, attempt_id=attempt_id, before="running", after="completed" if terminal == "succeeded" else terminal, run_id=run_id, task_id=task_id)
                self._change(task_id, State.RUNNING, target, version)
            if terminal != "succeeded":
                return
            artifact_id = new_id()
            candidate = bytes(data)
            ref = publish_artifact(self.db, self.blobs, chunks=[candidate], artifact_id=artifact_id,
                media_type="text/plain", provenance={"run_id": run_id, "task_id": task_id, "attempt_id": attempt_id},
                principal=self._principal, idempotency_key=attempt_id).response
            approved = await self.verify(task_id, candidate)
            if type(approved) is not bool:
                raise ValueError("Verifier must return bool")
            with self._transaction():
                self._check_running(run_id)
                record_verification(self.db, run_id=run_id, task_id=task_id, task_version=version + 1,
                    candidate_sha256=ref["sha256"], artifact_id=artifact_id, verifier_id=self.verifier_id, approved=approved)
                if not approved and attempt_no >= self.max_attempts:
                    # CHANGES_REQUIRED is deliberately retained for human action;
                    # it must never be silently reported as successful.
                    pass
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
