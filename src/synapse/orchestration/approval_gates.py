"""Approval gates for workflow steps on the mock path.

A step with an ``ApprovalStepSpec`` does not execute on first claim. The
coordinator (a) asks the daemon requester authority for an approval, (b) parks
the task in AWAITING_APPROVAL behind a gate, and (c) on a later pass re-verifies
the approval with ``verify_approval_for_execution`` and only then resumes the
task and reserves a fresh attempt. The coordinator holds only the requester
object: it has no way to decide an approval, so a step can never self-grant.
Approval tokens are never read, stored or logged here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import sqlite3
from collections.abc import Callable

from synapse.application.approval_requests import DaemonApprovalRequester, approval_id_for
from synapse.domain.contracts import new_id, validate_id
from synapse.domain.workflow import State, StateChange
from synapse.persistence.approval_binding import (
    ApprovalGrant, ApprovalNotGranted, DenialReason, verify_approval_for_execution,
)
from synapse.persistence.budget_binding import BudgetDenied, write_budget_block
from synapse.persistence.attempt_store import reserve_attempt
from synapse.persistence.gated_attempt import reserve_attempt_after_approval
from synapse.persistence.mock_execution_store import set_attempt
from synapse.persistence.state_store import StateConflict, write_task_transition
from synapse.persistence.task_approval_gate import write_task_approval_gate, write_task_approval_resolution
from synapse.persistence.task_claim import claim_task

_PRINCIPAL = "mock-engine"
# Denials the gate can record against an approved, unexpired approval.
_BINDING_FAILURES = {DenialReason.BINDING_MISMATCH, DenialReason.MALFORMED, DenialReason.WRONG_RUN}


@dataclass(frozen=True, slots=True)
class ApprovalStepSpec:
    """What the step is about to do; the approval must bind exactly this."""
    operation: str
    target: str
    candidate_sha256: str
    policy_version: str
    ttl: timedelta = timedelta(hours=1)

    def __post_init__(self) -> None:
        for value in (self.operation, self.target, self.policy_version):
            validate_id(value)
        if not isinstance(self.ttl, timedelta) or not timedelta(0) < self.ttl <= timedelta(hours=24):
            raise ValueError("ttl must be positive and at most 24 hours")


class ResumeStatus(StrEnum):
    PENDING = "pending"      # still requested (or undecidable now); task stays parked
    RESUMED = "resumed"      # verified; a new attempt is reserved and running
    FAILED = "failed"        # gate resolved the task to FAILED
    BLOCKED = "blocked"      # budget refused the real attempt; task BLOCKED, no attempt reserved


@dataclass(frozen=True, slots=True)
class Resumed:
    status: ResumeStatus
    attempt_id: str | None = None
    attempt_count: int = 0
    task_version: int = 0
    reason: str | None = None


class ApprovalGateCoordinator:
    def __init__(self, connection: sqlite3.Connection, requester: DaemonApprovalRequester, *,
                 principal: str = _PRINCIPAL):
        if type(requester) is not DaemonApprovalRequester:
            raise TypeError("A DaemonApprovalRequester is required")
        validate_id(principal)
        self.db, self._requester, self._principal = connection, requester, principal

    @staticmethod
    def request_key(task_id: str) -> str:
        return "gate:" + task_id

    def approval_id(self, run_id: str, task_id: str) -> str:
        return approval_id_for(run_id, self.request_key(task_id))

    def has_gate(self, task_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM task_approval_gates WHERE task_id=?", (task_id,)).fetchone() is not None

    def _verify(self, run_id: str, task_id: str, spec: ApprovalStepSpec) -> ApprovalGrant:
        return verify_approval_for_execution(
            self.db, approval_id=self.approval_id(run_id, task_id), run_id=run_id,
            operation=spec.operation, target=spec.target, candidate_sha256=spec.candidate_sha256,
            policy_version=spec.policy_version)

    def verify_gated(self, run_id: str, task_id: str, spec: ApprovalStepSpec) -> ApprovalGrant:
        """Re-verify an already-consumed gate's approval (e.g. before a retry attempt)."""
        return self._verify(run_id, task_id, spec)

    def park(self, *, run_id: str, task_id: str, profile_id: str, spec: ApprovalStepSpec,
             max_running: int,
             budget_check: Callable[[sqlite3.Connection, str, str], None] | None = None) -> int:
        """Request the approval (idempotently) and park the READY task behind it.

        The request commits in its own idempotent transaction; claim, a
        completed bookkeeping attempt for the gate step, and the park commit
        together. A crash in between leaves a READY task whose request is simply
        reused on the next pass. ``budget_check`` runs inside the claim; ``BudgetDenied``
        propagates with the transaction rolled back (the caller blocks the READY task).
        Returns the parked task's version.
        """
        approval_id = self.approval_id(run_id, task_id)
        exists = self.db.execute("SELECT 1 FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
        if exists is None:
            self._requester.request_approval(
                run_id=run_id, operation=spec.operation, target=spec.target,
                candidate_sha256=spec.candidate_sha256, policy_version=spec.policy_version,
                expires_at=(datetime.now(timezone.utc) + spec.ttl).isoformat(),
                idempotency_key=self.request_key(task_id), correlation_id="gate-" + task_id)
        attempt_id = new_id()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT state_version FROM tasks WHERE id=? AND run_id=?", (task_id, run_id)).fetchone()
            if row is None:
                raise StateConflict("Task does not belong to run")
            version = claim_task(self.db, task_id=task_id, expected_version=row[0], profile_id=profile_id,
                                 principal=self._principal, max_running=max_running,
                                 budget_check=budget_check)
            reserve_attempt(self.db, task_id=task_id, attempt_id=attempt_id, expected_task_version=version,
                            principal=self._principal)
            version = write_task_transition(
                self.db, StateChange(task_id, State.ASSIGNED, State.RUNNING, version + 1), principal=self._principal)
            set_attempt(self.db, attempt_id=attempt_id, before="created", after="running", run_id=run_id, task_id=task_id)
            set_attempt(self.db, attempt_id=attempt_id, before="running", after="completed", run_id=run_id, task_id=task_id)
            version = write_task_approval_gate(self.db, task_id=task_id, approval_id=approval_id,
                                               expected_task_version=version, principal=self._principal,
                                               bookkeeping_attempt_id=attempt_id)
            self.db.execute("COMMIT")
            return version
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def resume(self, *, run_id: str, task_id: str, spec: ApprovalStepSpec,
               budget_check: Callable[[sqlite3.Connection, str, str], None] | None = None) -> Resumed:
        """Re-verify the binding and, only if it holds, resume with a new attempt.

        Rejected/expired/revoked/lapsed approvals and binding mismatches fail the
        task through gate resolution. A still-pending approval leaves it parked.
        ``budget_check`` runs in the reservation transaction, before the real attempt exists; on
        ``BudgetDenied`` the gate is consumed and the task goes RUNNING -> BLOCKED in that same
        transaction (fail closed, no attempt, no retry) and ``BLOCKED`` is returned.
        """
        row = self.db.execute("SELECT state,state_version FROM tasks WHERE id=? AND run_id=?", (task_id, run_id)).fetchone()
        if row is None or row[0] != State.AWAITING_APPROVAL:
            raise StateConflict("Task is not awaiting approval")
        version = row[1]
        try:
            grant = self._verify(run_id, task_id, spec)
        except ApprovalNotGranted as denied:
            return self._deny(run_id, task_id, version, denied.reason)
        attempt_id = new_id()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            write_task_approval_resolution(self.db, task_id=task_id, expected_task_version=version,
                                           principal=self._principal, binding_recheck_passed=True)
            if budget_check is not None:
                profile_id = self.db.execute("SELECT assigned_profile_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
                try:
                    budget_check(self.db, task_id, profile_id)
                except BudgetDenied as denied:
                    blocked = write_budget_block(self.db, principal=self._principal, run_id=run_id, task_id=task_id,
                                                 version=version + 1, before=State.RUNNING, profile_id=profile_id,
                                                 denied=denied)
                    self.db.execute("COMMIT")
                    return Resumed(ResumeStatus.BLOCKED, task_version=blocked, reason=denied.reason)
            number = reserve_attempt_after_approval(
                self.db, task_id=task_id, attempt_id=attempt_id, expected_task_version=version + 1,
                principal=self._principal, grant=grant)
            set_attempt(self.db, attempt_id=attempt_id, before="created", after="running", run_id=run_id, task_id=task_id)
            self.db.execute("COMMIT")
        except StateConflict as error:
            # Lost a race (decision/expiry moved) or run not running: stay parked
            # and re-evaluate on the next pass; resolution re-reads authoritative state.
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            return Resumed(ResumeStatus.PENDING, reason=type(error).__name__)
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        return Resumed(ResumeStatus.RESUMED, attempt_id=attempt_id, attempt_count=number,
                       task_version=version + 2)

    def _deny(self, run_id: str, task_id: str, version: int, reason: DenialReason) -> Resumed:
        denial = reason.value if reason in _BINDING_FAILURES else None
        self.db.execute("BEGIN IMMEDIATE")
        try:
            outcome = write_task_approval_resolution(
                self.db, task_id=task_id, expected_task_version=version, principal=self._principal,
                binding_denial=denial)
            self.db.execute("COMMIT")
        except StateConflict:
            # Pending approval (not yet decided/expired), or a concurrent change.
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            return Resumed(ResumeStatus.PENDING, reason=reason.value)
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        if outcome != State.FAILED:  # pragma: no cover - resolution without recheck never resumes
            raise StateConflict("Unexpected gate outcome")
        return Resumed(ResumeStatus.FAILED, task_version=version + 1, reason=reason.value)
