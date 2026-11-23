"""Daemon-owned, fixed-fixture mock execution; never launches CLI processes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import sqlite3

from synapse.application.approval_requests import DaemonApprovalRequester
from synapse.domain.contracts import RunId, validate_id
from synapse.domain.runs import RunChange, RunState
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.orchestration.approval_gates import ApprovalStepSpec
from synapse.orchestration.mock_engine import MockWorkflowEngine
from synapse.persistence.mock_recovery import recover_mock_run
from synapse.persistence.mock_resume import resume_fixed_mock
from synapse.persistence.mock_cancellation import finalize_mock_cancellation, request_mock_cancellation
from synapse.persistence.idempotency import execute_idempotent
from synapse.persistence import mock_scenarios as ms
from synapse.persistence.run_store import write_run_transition
from synapse.persistence.state_store import StateConflict
from synapse.providers.mock import MockOutcome, MockProvider, MockScenario

_PROFILE = ms.FIXED_PROFILE
_FIXTURE = ms.FIXED_KIND
_CANDIDATE = b"synapse mock candidate v1\n"
_LOG = logging.getLogger(__name__)


async def _verify_fixed_fixture(task_id: str, candidate: bytes) -> bool:
    """Independent deterministic check for this synthetic fixture only."""
    validate_id(task_id)
    return candidate == b"synapse mock candidate v1\n"


class MockRuntime:
    def __init__(self, connection: sqlite3.Connection, blob_directory):
        self.db = connection
        self.blobs = ArtifactBlobStore(blob_directory, max_bytes=65536)
        self._active: asyncio.Task | None = None
        self._active_run: str | None = None
        self._cancelling: asyncio.Task | None = None
        self._pending_redrive: str | None = None
        self._park_poll = 2.0  # seconds between decision checks while every open task is parked

    def start(self, run_id: str, key: str, scenarios: dict | None = None, approval_steps: dict | None = None):
        """Commit launch intent before scheduling. Never launch on replay.

        scenarios=None starts the fixed fixture exactly as before. Otherwise it
        is a client-supplied {node: name | [name, ...]} map over the closed
        mock catalog; it is validated against the run's nodes, persisted
        immutably with the start, and raises ScenarioError (a ValueError)
        before any state change when invalid.

        approval_steps (runtime-level only; no HTTP/CLI surface) is a closed
        {node: {operation, target, candidate_sha256, policy_version[, ttl_seconds]}}
        spec naming gated nodes. It is validated, recorded immutably with the
        start (so it survives restart without being re-supplied) and implies
        the scenario kind; unlisted nodes default to the 'succeed' scenario.
        """
        validate_id(run_id)
        steps = None if approval_steps is None else ms.parse_approval_steps(approval_steps)
        parsed = None if scenarios is None and steps is None else ({} if scenarios is None else ms.parse_scenarios(scenarios))
        kind = _FIXTURE if parsed is None else ms.SCENARIO_KIND
        profile_id = ms.KIND_PROFILES[kind]
        def write(db):
            if ((self._active is not None and not self._active.done())
                    or (self._cancelling is not None and not self._cancelling.done())):
                raise StateConflict("Another mock run is active")
            row = db.execute("SELECT state,state_version FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row[0] != "queued":
                raise StateConflict("Run must be queued")
            if db.execute("SELECT 1 FROM mock_run_config c JOIN runs r ON r.id=c.run_id WHERE r.state='running' LIMIT 1").fetchone():
                raise StateConflict("Another mock run is active")
            canonical = None
            if parsed is not None:
                try:
                    canonical = ms.resolve_for_run(db, run_id, parsed)
                except ms.ScenarioError:
                    raise
                if steps is not None:
                    ms.check_steps_for_run(db, run_id, steps)
            profile = db.execute("SELECT provider,enabled,max_concurrency FROM profiles WHERE id=?", (profile_id,)).fetchone()
            if profile is None:
                db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,enabled,max_concurrency,created_at) VALUES(?,'mock','Built-in mock',?,1,6,?)", (profile_id, "builtin:" + kind, datetime.now(timezone.utc).isoformat()))
            elif profile != ("mock", 1, 6):
                raise StateConflict("Built-in mock profile is unavailable")
            db.execute("INSERT INTO mock_run_config(run_id,profile_id,fixture) VALUES(?,?,?)", (run_id, profile_id, _FIXTURE))
            if canonical is not None:
                ms.record_scenarios(db, run_id, canonical)
                if steps is not None:
                    ms.record_approval_steps(db, run_id, steps)
            write_run_transition(db, RunChange(RunId(run_id), RunState.QUEUED, RunState.RUNNING, row[1]), principal="local-operator")
            return {"run_id": run_id, "state": "running", "fixture": kind}
        request = {"run_id": run_id, "fixture": _FIXTURE} if parsed is None else {
            "run_id": run_id, "fixture": kind, "scenarios": {n: list(c) for n, c in sorted(parsed.items())}}
        if steps is not None:
            request["approval_steps"] = steps
        result = execute_idempotent(self.db, principal="local-operator", operation="mock.start", key=key,
                                    request=request, execute=write)
        if not result.replayed:
            self._active_run = run_id
            self._active = asyncio.create_task(self._run(run_id))
        return result

    def resume(self, run_id: str, key: str, correlation: str, expected_run_version: int, tasks: list[dict], assertion: str):
        """Resume only after this daemon has no active in-process mock worker."""
        active = ((self._active is not None and not self._active.done()) or
                  (self._cancelling is not None and not self._cancelling.done()))
        result = resume_fixed_mock(self.db, run_id=run_id, expected_run_version=expected_run_version,
                                   tasks=tasks, assertion=assertion, key=key, correlation_id=correlation,
                                   no_workers_survive=not active)
        if not result.replayed:
            self._active_run = run_id
            self._active = asyncio.create_task(self._run(run_id))
        return result

    def cancel(self, run_id: str, key: str):
        """Persist intent, then stop only this daemon's synthetic worker."""
        if self._active is not None and not self._active.done() and self._active_run != run_id:
            raise StateConflict("A different mock run is active")
        result = request_mock_cancellation(self.db, run_id=run_id, key=key)
        if self._cancelling is None or self._cancelling.done():
            # Signal synchronously before yielding to any runnable worker.
            if (self._active is not None and self._active_run == run_id
                    and not self._active.done() and not self._active.cancelling()):
                self._active.cancel()
            self._cancelling = asyncio.create_task(self._stop_and_finalize(run_id))
        return result

    _cancel_timeout = 5

    async def _stop_and_finalize(self, run_id: str):
        try:
            if self._active is not None and self._active_run == run_id and not self._active.done():
                if not self._active.cancelling():
                    self._active.cancel()
                done, _ = await asyncio.wait({self._active}, timeout=self._cancel_timeout)
                if not done:
                    _LOG.error("Mock cancellation timed out for run %s", run_id)
                    return
                await asyncio.gather(self._active, return_exceptions=True)
            finalize_mock_cancellation(self.db, run_id=run_id, no_workers_survive=True)
        except Exception:
            # The durable request remains pending; startup reconciliation or an
            # explicit retry may safely retry once no worker remains.
            _LOG.exception("Mock cancellation incomplete for run %s", run_id)

    def _engine_inputs(self, run_id: str):
        """Provider, verifier id, profile and task->scenario map for a known run kind."""
        kind = ms.mock_kind(self.db, run_id)
        tasks = self.db.execute("SELECT id,node_id FROM tasks WHERE run_id=?", (run_id,)).fetchall()
        if kind == _FIXTURE:
            provider = MockProvider({_FIXTURE: MockScenario((_CANDIDATE.decode("ascii"),), MockOutcome.SUCCEEDED)})
            return provider, "builtin.mock.fixture-verifier.v1", _PROFILE, {t: (_FIXTURE,) for t, _ in tasks}
        if kind == ms.SCENARIO_KIND:
            canonical = ms.load_scenarios(self.db, run_id)
            if canonical is None or set(canonical) != {n for _, n in tasks}:
                raise StateConflict("Recorded scenarios do not match the run")
            provider = MockProvider({name: MockScenario(chunks, MockOutcome(outcome))
                                     for name, (chunks, outcome) in ms.CATALOG.items()})
            return provider, "builtin.mock.scenario-verifier.v1", ms.SCENARIO_PROFILE, {t: tuple(canonical[n]) for t, n in tasks}
        raise StateConflict("Run is not a mock run this daemon understands")

    def _approval_specs(self, run_id: str) -> dict[str, ApprovalStepSpec]:
        """Recorded approval steps as task_id -> spec (empty when none were configured)."""
        steps = ms.load_approval_steps(self.db, run_id)
        if steps is None:
            raise StateConflict("Recorded approval steps are not trustworthy")
        if not steps:
            return {}
        by_node = dict((n, t) for t, n in self.db.execute("SELECT id,node_id FROM tasks WHERE run_id=?", (run_id,)))
        if not set(steps) <= set(by_node):
            raise StateConflict("Recorded approval steps do not match the run")
        return {by_node[n]: ApprovalStepSpec(v["operation"], v["target"], v["candidate_sha256"], v["policy_version"],
                                             timedelta(seconds=v["ttl_seconds"])) for n, v in steps.items()}

    def _parked_only(self, run_id: str) -> bool:
        """True when a running run has gated tasks parked and nothing else that can move.

        Such a run is left running (not recovered) so a later decision can resume
        it; any blocked/active/ready task or unresolved attempt means the usual
        conservative recovery applies instead.
        """
        if ms.mock_kind(self.db, run_id) != ms.SCENARIO_KIND or not ms.load_approval_steps(self.db, run_id):
            return False
        run = self.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None or run[0] != "running":
            return False
        if self.db.execute("SELECT 1 FROM mock_cancellation_requests WHERE run_id=?", (run_id,)).fetchone():
            return False
        states = {r[0] for r in self.db.execute("SELECT state FROM tasks WHERE run_id=? AND state NOT IN ('completed','failed','cancelled')", (run_id,))}
        if "awaiting_approval" not in states or not states <= {"awaiting_approval", "waiting_dependencies"}:
            return False
        if self.db.execute("SELECT 1 FROM tasks t JOIN task_attempts a ON a.task_id=t.id WHERE t.run_id=? AND a.state NOT IN ('completed','failed','cancelled') LIMIT 1", (run_id,)).fetchone():
            return False
        return self.db.execute("SELECT 1 FROM tasks t JOIN task_approval_gates g ON g.task_id=t.id WHERE t.run_id=? AND t.state='awaiting_approval' AND g.resolved_at IS NULL LIMIT 1", (run_id,)).fetchone() is not None

    async def _run(self, run_id: str):
        try:
            provider, verifier_id, profile_id, scenarios = self._engine_inputs(run_id)
            specs = self._approval_specs(run_id)
            engine = MockWorkflowEngine(self.db, provider, self.blobs, _verify_fixed_fixture, verifier_id=verifier_id,
                                        approval_requester=DaemonApprovalRequester(self.db) if specs else None)
            while True:
                outcome = await engine.run(run_id, profile_id, scenarios, specs or None)
                if outcome == "blocked" and specs and self._parked_only(run_id):
                    # Parked tasks stay parked until a decision or expiry; re-drive then.
                    await asyncio.sleep(self._park_poll)
                    continue
                break
            if outcome == "blocked":
                self._safe_recover_run(run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.exception("Mock execution failed for run %s", run_id)
            self._safe_recover_run(run_id)

    def _safe_recover_run(self, run_id: str):
        try:
            self._recover_run(run_id)
        except Exception:
            # Startup reconciliation repeats recovery for this run.
            _LOG.exception("Mock recovery failed for run %s", run_id)

    def _recover_run(self, run_id: str):
        # In-process mock workers cannot survive this process; recover_mock_run
        # first commits conservative task/attempt reconciliation. A crash before
        # the following run transition is safe: startup repeats reconciliation.
        if ms.mock_kind(self.db, run_id) is None:
            raise StateConflict("Run is not a mock run this daemon understands")
        recover_mock_run(self.db, run_id=run_id, principal="mock-recovery", no_workers_survive=True)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT state,state_version FROM runs WHERE id=?", (run_id,)).fetchone()
            if row and row[0] == "running":
                write_run_transition(self.db, RunChange(RunId(run_id), RunState.RUNNING, RunState.RECOVERING, row[1]), principal="mock-recovery")
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def recover_interrupted(self):
        """Invoke only after acquiring exclusive daemon ownership at startup."""
        # Real-agent runs share mock_run_config for exclusion but belong to AgentRuntime.
        runs = self.db.execute("SELECT c.run_id FROM mock_run_config c JOIN profiles p ON p.id=c.profile_id WHERE p.provider='mock'").fetchall()
        for (run_id,) in runs:
            # One inconsistent run must not prevent the daemon, or other runs, from starting.
            try:
                if self.db.execute("SELECT 1 FROM mock_cancellation_requests WHERE run_id=?", (run_id,)).fetchone():
                    # Daemon ownership at startup establishes no mock coroutine survived.
                    finalize_mock_cancellation(self.db, run_id=run_id, no_workers_survive=True)
                elif self._pending_redrive is None and self._parked_only(run_id):
                    # Only parked gates remain: nothing was in flight, so keep the run
                    # running; resume_parked() re-drives it once the event loop exists.
                    self._pending_redrive = run_id
                else:
                    self._recover_run(run_id)
            except Exception:
                _LOG.exception("Mock startup recovery failed for run %s", run_id)

    def resume_parked(self):
        """Re-drive the run left parked at startup (call from the daemon's event loop).

        The recorded specs are reloaded from the database; parked tasks stay
        parked until their approval is decided or expires, then resume through
        the gate coordinator with a fresh binding recheck.
        """
        run_id, self._pending_redrive = self._pending_redrive, None
        if run_id is None:
            return False
        self._active_run = run_id
        self._active = asyncio.create_task(self._run(run_id))
        return True
