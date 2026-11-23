"""Pure workflow state transitions and domain-level invariants."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from synapse.domain.contracts import validate_id


class State(StrEnum):
    CREATED = "created"
    WAITING_DEPENDENCIES = "waiting_dependencies"
    READY = "ready"
    ASSIGNED = "assigned"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    RETRY_WAIT = "retry_wait"
    CHANGES_REQUIRED = "changes_required"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AWAITING_APPROVAL = "awaiting_approval"


TRANSITIONS: dict[State, frozenset[State]] = {
    State.CREATED: frozenset({State.WAITING_DEPENDENCIES, State.READY, State.CANCELLED}),
    State.WAITING_DEPENDENCIES: frozenset({State.READY, State.BLOCKED, State.CANCELLED}),
    State.READY: frozenset({State.ASSIGNED, State.BLOCKED, State.CANCELLED}),
    State.ASSIGNED: frozenset({State.RUNNING, State.READY, State.BLOCKED, State.FAILED, State.CANCELLED}),
    State.RUNNING: frozenset({State.VERIFYING, State.AWAITING_APPROVAL, State.BLOCKED, State.RETRY_WAIT, State.FAILED, State.CANCELLED}),
    State.VERIFYING: frozenset({
        State.COMPLETED, State.CHANGES_REQUIRED, State.AWAITING_APPROVAL,
        State.RETRY_WAIT, State.BLOCKED, State.FAILED, State.CANCELLED,
    }),
    State.CHANGES_REQUIRED: frozenset({State.READY, State.CANCELLED}),
    State.BLOCKED: frozenset({State.READY, State.CANCELLED}),
    State.RETRY_WAIT: frozenset({State.READY, State.FAILED, State.CANCELLED}),
    # Two entry paths exist. VERIFYING -> AWAITING_APPROVAL is the original
    # post-verification wait. RUNNING -> AWAITING_APPROVAL is the gated wait; it
    # leaves only through persistence.task_approval_gate (RUNNING or FAILED).
    State.AWAITING_APPROVAL: frozenset({
        State.COMPLETED, State.CHANGES_REQUIRED, State.RUNNING, State.FAILED, State.CANCELLED,
    }),
    State.COMPLETED: frozenset(),
    State.FAILED: frozenset(),
    State.CANCELLED: frozenset(),
}


# Transitions that must go through persistence.task_approval_gate because they
# need a linked approval (and, to resume, execution-time binding evidence).
GATE_ONLY_TRANSITIONS: frozenset[tuple[State, State]] = frozenset({
    (State.RUNNING, State.AWAITING_APPROVAL),
    (State.AWAITING_APPROVAL, State.RUNNING),
    (State.AWAITING_APPROVAL, State.FAILED),
})


class InvalidTransition(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StateChange:
    aggregate_id: str
    before: State
    after: State
    expected_version: int

    def __post_init__(self) -> None:
        validate_id(self.aggregate_id)
        if type(self.expected_version) is not int or self.expected_version < 0:
            raise ValueError("expected_version must be a nonnegative integer")
        validate_transition(self.before, self.after)


def validate_transition(before: State, after: State) -> None:
    if not isinstance(before, State) or not isinstance(after, State):
        raise InvalidTransition("Both states must be task states")
    if after not in TRANSITIONS[before]:
        raise InvalidTransition(f"Illegal transition: {before.value} -> {after.value}")


def plan_transition(aggregate_id: str, state: State, target: State, version: int) -> StateChange:
    return StateChange(aggregate_id, state, target, version)
