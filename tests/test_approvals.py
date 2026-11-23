import pytest

from synapse.domain.approvals import ApprovalChange, ApprovalState
from synapse.domain.contracts import ApprovalId
from synapse.domain.workflow import InvalidTransition


@pytest.mark.parametrize("target", [
    ApprovalState.APPROVED, ApprovalState.REJECTED,
    ApprovalState.EXPIRED, ApprovalState.REVOKED,
])
def test_requested_approval_can_reach_terminal_state(target):
    change = ApprovalChange(ApprovalId("approval"), ApprovalState.REQUESTED, target, 0)
    assert change.after is target


def test_approved_approval_can_be_revoked_only_to_revoked():
    assert ApprovalChange(ApprovalId("approval"), ApprovalState.APPROVED, ApprovalState.REVOKED, 1).after is ApprovalState.REVOKED
    with pytest.raises(InvalidTransition):
        ApprovalChange(ApprovalId("approval"), ApprovalState.APPROVED, ApprovalState.EXPIRED, 1)


@pytest.mark.parametrize("state", [
    ApprovalState.APPROVED, ApprovalState.REJECTED,
    ApprovalState.EXPIRED, ApprovalState.REVOKED,
])
def test_terminal_approval_cannot_transition(state):
    with pytest.raises(InvalidTransition):
        ApprovalChange(ApprovalId("approval"), state, ApprovalState.APPROVED, 1)


@pytest.mark.parametrize("version", [-1, True, 1.5])
def test_approval_version_must_be_nonnegative_integer(version):
    with pytest.raises(ValueError):
        ApprovalChange(ApprovalId("approval"), ApprovalState.REQUESTED, ApprovalState.REJECTED, version)


def test_invalid_approval_id_and_raw_state_are_rejected():
    with pytest.raises(ValueError):
        ApprovalChange(ApprovalId(" "), ApprovalState.REQUESTED, ApprovalState.APPROVED, 0)
    with pytest.raises(InvalidTransition):
        ApprovalChange(ApprovalId("approval"), "requested", ApprovalState.APPROVED, 0)
