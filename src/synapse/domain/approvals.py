"""Approval lifecycle rules.

Authorization and candidate validation belong to the application service.
"""
from dataclasses import dataclass
from enum import StrEnum

from synapse.domain.contracts import ApprovalId, validate_id
from synapse.domain.workflow import InvalidTransition


class ApprovalState(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class ApprovalChange:
    approval_id: ApprovalId
    before: ApprovalState
    after: ApprovalState
    expected_version: int

    def __post_init__(self) -> None:
        validate_id(self.approval_id)
        if type(self.expected_version) is not int or self.expected_version < 0:
            raise ValueError("expected_version must be a nonnegative integer")
        if not isinstance(self.before, ApprovalState) or not isinstance(self.after, ApprovalState):
            raise InvalidTransition("Both states must be approval states")
        # requested -> any decision; approved -> revoked (a human may withdraw an unused approval).
        legal = ((self.before == ApprovalState.REQUESTED and self.after != ApprovalState.REQUESTED)
                 or (self.before == ApprovalState.APPROVED and self.after == ApprovalState.REVOKED))
        if not legal:
            raise InvalidTransition(f"Illegal approval transition: {self.before} -> {self.after}")
