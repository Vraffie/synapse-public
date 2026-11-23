"""Privileged operations: the only way a plugin acts beyond registering things.

A plugin never talks to a repository, the database or the approval service. It calls
``PluginContext.request(action, resource)``; this gate decides, failing closed on every path:
an unknown action, an action the operator did not grant, any policy error, any answer that is not
exactly ``Decision(allowed=True)``, or a failed audit write all deny. The gate does not execute
anything: an allowed decision is permission for the host to proceed, and the host's own approval
binding (candidate manifest, human approver) still applies.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from typing import Any, Protocol

from synapse.plugins.manifest import PRIVILEGED_ACTIONS

MAX_RESOURCE_BYTES = 4096


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str


class PolicyPort(Protocol):
    """ARCHITECTURE.md section 4. The real implementation routes through the approval service."""

    def authorize(self, principal: str, action: str, resource: dict) -> Decision: ...


class DenyAllPolicy:
    def authorize(self, principal: str, action: str, resource: dict) -> Decision:
        return Decision(False, "no policy is configured")


def _plain(resource: Any) -> dict | None:
    if not isinstance(resource, Mapping):
        return None
    try:
        text = json.dumps(resource, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return json.loads(text) if len(text) <= MAX_RESOURCE_BYTES else None


class PluginPolicyGate:
    def __init__(self, plugin_id: str, granted: frozenset[str], policy: PolicyPort,
                 audit: Callable[[dict], None]) -> None:
        self._principal = f"plugin:{plugin_id}"
        self._granted = frozenset(granted)
        self._policy = policy
        self._audit = audit

    def request(self, action: Any, resource: Any) -> Decision:
        decision = self._decide(action, resource)
        try:
            self._audit({"principal": self._principal, "action": action if isinstance(action, str) else None,
                         "allowed": decision.allowed, "reason": decision.reason})
        except Exception:
            return Decision(False, "audit_failed")  # no privileged decision without a record
        return decision

    def _decide(self, action: Any, resource: Any) -> Decision:
        if not isinstance(action, str) or action not in PRIVILEGED_ACTIONS:
            return Decision(False, "unknown_action")
        if action not in self._granted:
            return Decision(False, "not_granted")
        plain = _plain(resource)
        if plain is None:
            return Decision(False, "invalid_resource")
        try:
            answer = self._policy.authorize(self._principal, action, plain)
        except Exception:
            return Decision(False, "policy_error")
        if type(answer) is not Decision or answer.allowed is not True or not isinstance(answer.reason, str):
            return Decision(False, "policy_denied" if type(answer) is Decision else "policy_invalid")
        return Decision(True, answer.reason[:200])
