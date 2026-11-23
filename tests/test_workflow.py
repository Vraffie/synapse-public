import pytest

from synapse.domain.workflow import (
    InvalidTransition, State, TRANSITIONS, plan_transition, validate_transition,
)


def test_terminal_states_have_no_exits():
    for state in (State.COMPLETED, State.FAILED, State.CANCELLED):
        assert not TRANSITIONS[state]
        with pytest.raises(InvalidTransition):
            validate_transition(state, State.READY)


def test_legal_review_loop():
    validate_transition(State.RUNNING, State.VERIFYING)
    validate_transition(State.VERIFYING, State.CHANGES_REQUIRED)
    validate_transition(State.CHANGES_REQUIRED, State.READY)


def test_invalid_jump_rejected():
    with pytest.raises(InvalidTransition):
        plan_transition("task-1", State.CREATED, State.COMPLETED, 0)


def test_transition_plan_is_immutable():
    p = plan_transition("task-1", State.READY, State.ASSIGNED, 2)
    assert p.expected_version == 2
    with pytest.raises(Exception):
        p.expected_version = 4


@pytest.mark.parametrize("target", [State.RETRY_WAIT, State.BLOCKED, State.CANCELLED])
def test_verification_can_retry_block_or_cancel(target):
    assert plan_transition("task-1", State.VERIFYING, target, 3).after is target


def test_raw_state_strings_are_rejected_at_domain_boundary():
    with pytest.raises(InvalidTransition):
        validate_transition(State.CREATED, "ready")


@pytest.mark.parametrize("version", [-1, True, 1.5])
def test_task_version_must_be_nonnegative_integer(version):
    with pytest.raises(ValueError):
        plan_transition("task", State.CREATED, State.READY, version)


@pytest.mark.parametrize("aggregate_id", ["", "   ", "x" * 257])
def test_task_id_uses_domain_id_validation(aggregate_id):
    with pytest.raises(ValueError):
        plan_transition(aggregate_id, State.CREATED, State.READY, 0)


def test_direct_state_change_validates_transition():
    from synapse.domain.workflow import StateChange

    with pytest.raises(InvalidTransition):
        StateChange("task", State.CREATED, State.COMPLETED, 0)
