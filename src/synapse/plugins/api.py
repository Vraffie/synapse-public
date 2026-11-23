"""The whole surface a plugin sees: ``PluginContext`` in, ``PluginRegistration`` out.

There is deliberately no database handle, connection, repository, filesystem path or approval
object anywhere on it. A plugin reads its own package files from the verified snapshot and
requests privileged operations through the policy gate. Nothing else.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from synapse.agents.roles import AgentRole
from synapse.plugins.policy import Decision, PluginPolicyGate


@dataclass(frozen=True, slots=True)
class PluginRegistration:
    roles: tuple[AgentRole, ...] = ()
    validators: Mapping[str, Callable] = field(default_factory=dict)


class PluginContext:
    __slots__ = ("plugin_id", "version", "granted", "_gate", "_files")

    def __init__(self, plugin_id: str, version: str, granted: frozenset[str], gate: PluginPolicyGate,
                 files: Mapping[str, bytes]) -> None:
        self.plugin_id, self.version, self.granted = plugin_id, version, frozenset(granted)
        self._gate, self._files = gate, dict(files)

    def request(self, action: str, resource: Mapping) -> Decision:
        """Ask for a privileged operation. Permission only; the host performs and binds approval."""
        return self._gate.request(action, resource)

    def read_file(self, relpath: str) -> bytes:
        """A file of this plugin's own verified package (the bytes that were hashed)."""
        if not isinstance(relpath, str) or relpath not in self._files:
            raise FileNotFoundError(relpath)
        return self._files[relpath]
