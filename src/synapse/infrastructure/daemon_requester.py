"""Daemon-side requester authority, distinct from the human approver authority.

The requester is the fixed in-process subject ``daemon:workflow`` with actor kind
``agent``. It authenticates only the unforgeable capability object created with
the authority, which exists in daemon memory and is never serialized, written to
disk, or accepted from a transport. It can only ``approval.request``; it can
never decide. Self-approval is additionally refused by ApprovalService.decide
through subject comparison, and a human approver token cannot request.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from synapse.application.approval_service import Actor, ApprovalAccessDenied

REQUESTER_SUBJECT = "daemon:workflow"
_ACTIONS = frozenset({"approval.request"})


class DaemonRequesterAuthority:
    def __init__(self) -> None:
        self.capability = object()

    @contextmanager
    def authenticated(self, credential: object) -> Iterator[Actor]:
        if credential is not self.capability:
            raise ApprovalAccessDenied("Approval access denied")
        yield Actor(REQUESTER_SUBJECT, "agent")

    def allows(self, actor: Actor, action: str, resource: dict) -> bool:
        return (actor.kind == "agent" and actor.subject == REQUESTER_SUBJECT
                and action in _ACTIONS)
