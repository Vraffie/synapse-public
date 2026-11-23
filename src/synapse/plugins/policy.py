"""Privileged operations: the only way a plugin acts beyond registering things.

A plugin never talks to a repository, the database or the approval service. It calls
``PluginContext.request(action, resource)``; this gate decides, failing closed on every path:
an unknown action, an action the operator did not grant, any policy error, any answer that is not
exactly ``Decision(allowed=True)``, a failed audit write, or a plugin over its request budget all
deny. The gate does not execute anything: an allowed decision is permission for the host to
proceed, and the host's own approval binding (candidate manifest, human approver) still applies.

The policy is told *which code* is asking: principal ``plugin:<id>`` plus the exact ``version`` and
``package_sha256`` that were verified at load, so an approval recorded for one package cannot be
silently reused by a changed one. The audit row carries the same identity. Only allowlisted action
names are ever written to the audit row; anything else is recorded as ``action=None``.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from typing import Any, Protocol

from synapse.plugins.manifest import PRIVILEGED_ACTIONS
from synapse.plugins.safe_text import bounded

MAX_RESOURCE_BYTES = 4096
MAX_REASON_CHARS = 200
MAX_REQUESTS = 256  # per loaded plugin (one gate per PluginContext); later requests are denied


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str


class PolicyPort(Protocol):
    """ARCHITECTURE.md section 4. The real implementation routes through the approval service.

    ``version`` and ``package_sha256`` identify the exact verified package. An implementation that
    stores approvals must key them on these, not on ``principal`` alone.
    """

    def authorize(self, principal: str, action: str, resource: dict, *, version: str,
                  package_sha256: str) -> Decision: ...


class DenyAllPolicy:
    def authorize(self, principal: str, action: str, resource: dict, *, version: str,
                  package_sha256: str) -> Decision:
        return Decision(False, "no policy is configured")


def _plain(resource: Any) -> dict | None:
    if not isinstance(resource, Mapping):
        return None
    try:
        text = json.dumps(resource, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None
    return json.loads(text) if len(text) <= MAX_RESOURCE_BYTES else None


def _known_action(action: Any) -> str | None:
    """The action if it is exactly an allowlisted name; ``None`` otherwise (never hashes plugin objects)."""
    if type(action) is not str or len(action) > 64:
        return None
    return action if action in PRIVILEGED_ACTIONS else None


class PluginPolicyGate:
    def __init__(self, plugin_id: str, granted: frozenset[str], policy: PolicyPort,
                 audit: Callable[[dict], None], *, version: str, package_sha256: str) -> None:
        self._principal = f"plugin:{plugin_id}"
        self._version = version
        self._package_sha256 = package_sha256
        self._granted = frozenset(granted)
        self._policy = policy
        self._audit = audit
        self._count = 0

    def request(self, action: Any, resource: Any) -> Decision:
        self._count += 1
        known = _known_action(action)
        if self._count > MAX_REQUESTS:
            decision = Decision(False, "request_limit")
            if self._count > MAX_REQUESTS + 1:
                return decision  # the first over-budget denial is audited; the flood after it is not
        else:
            decision = self._decide(known, resource)
        try:
            self._audit({"principal": self._principal, "version": self._version,
                         "package_sha256": self._package_sha256, "action": known,
                         "allowed": decision.allowed, "reason": decision.reason})
        except Exception:
            return Decision(False, "audit_failed")  # no privileged decision without a record
        return decision

    def _decide(self, action: str | None, resource: Any) -> Decision:
        if action is None:
            return Decision(False, "unknown_action")
        if action not in self._granted:
            return Decision(False, "not_granted")
        plain = _plain(resource)
        if plain is None:
            return Decision(False, "invalid_resource")
        try:
            answer = self._policy.authorize(self._principal, action, plain, version=self._version,
                                            package_sha256=self._package_sha256)
        except Exception:
            return Decision(False, "policy_error")
        if type(answer) is not Decision or answer.allowed is not True or type(answer.reason) is not str:
            return Decision(False, "policy_denied" if type(answer) is Decision else "policy_invalid")
        return Decision(True, bounded(answer.reason, MAX_REASON_CHARS))
