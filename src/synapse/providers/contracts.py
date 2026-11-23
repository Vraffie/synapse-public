"""Shared in-process provider contract."""
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from synapse.domain.contracts import _freeze_json, validate_id


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    profile_id: str
    task_id: str
    input: Mapping[str, Any]

    def __post_init__(self) -> None:
        validate_id(self.profile_id)
        validate_id(self.task_id)
        if not isinstance(self.input, Mapping):
            raise TypeError("input must be a mapping")
        object.__setattr__(self, "input", _freeze_json(self.input))


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    streaming: bool
    cancellation: bool
    structured_events: bool
    resume: bool
    mcp: bool
    usage_telemetry: bool
    quota_telemetry: bool
    context_limit: int | None


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    profile_id: str
    status: str


@dataclass(frozen=True, slots=True)
class UsageObservation:
    """Provider-reported usage. ``None`` means unknown, never zero."""
    input_tokens: int | None
    output_tokens: int | None
    source: str
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None  # as reported; for a subscription an API-equivalent estimate, not a charge


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    kind: str
    text: str | None = None


class ProviderAdapter(Protocol):
    async def probe(self, profile_id: str) -> ProviderHealth: ...
    async def capabilities(self, profile_id: str) -> ProviderCapabilities: ...
    async def start(self, request: ExecutionRequest) -> str: ...
    def stream(self, handle: str) -> AsyncIterator[ProviderEvent]: ...
    async def cancel(self, handle: str) -> None: ...
    async def usage(self, handle: str) -> UsageObservation | None: ...
