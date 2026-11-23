"""Pure lifecycle rules for orchestration runs."""
from dataclasses import dataclass
from enum import StrEnum

from synapse.domain.contracts import RunId, validate_id
from synapse.domain.workflow import InvalidTransition


class RunState(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    PAUSED = "paused"
    RECOVERING = "recovering"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TRANSITIONS = {
    RunState.CREATED: frozenset({RunState.QUEUED, RunState.CANCELLED}),
    RunState.QUEUED: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.RUNNING: frozenset({
        RunState.AWAITING_APPROVAL, RunState.PAUSED, RunState.RECOVERING,
        RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED,
    }),
    RunState.AWAITING_APPROVAL: frozenset({
        RunState.RUNNING, RunState.FAILED, RunState.CANCELLED,
    }),
    RunState.PAUSED: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.RECOVERING: frozenset({
        RunState.RUNNING, RunState.FAILED, RunState.CANCELLED,
    }),
    RunState.SUCCEEDED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class RunChange:
    run_id: RunId
    before: RunState
    after: RunState
    expected_version: int

    def __post_init__(self) -> None:
        validate_id(self.run_id)
        if type(self.expected_version) is not int or self.expected_version < 0:
            raise ValueError("expected_version must be a nonnegative integer")
        if not isinstance(self.before, RunState) or not isinstance(self.after, RunState):
            raise InvalidTransition("Both states must be run states")
        if self.after not in _TRANSITIONS[self.before]:
            raise InvalidTransition(f"Illegal run transition: {self.before} -> {self.after}")
