"""Deterministic in-process provider for offline orchestration tests."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from synapse.domain.contracts import validate_id
from synapse.providers.contracts import (
    ExecutionRequest, ProviderCapabilities, ProviderEvent, ProviderHealth,
)


class MockOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MockScenario:
    """A fixed sequence of output chunks followed by one terminal outcome."""

    chunks: tuple[str, ...]
    outcome: MockOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.chunks, tuple) or not all(isinstance(chunk, str) for chunk in self.chunks):
            raise TypeError("chunks must be a tuple of strings")
        if not isinstance(self.outcome, MockOutcome):
            raise ValueError("outcome must be a MockOutcome")


@dataclass(frozen=True, slots=True)
class MockCapabilities(ProviderCapabilities):
    streaming: bool = True
    cancellation: bool = True
    structured_events: bool = True
    resume: bool = False
    mcp: bool = False
    usage_telemetry: bool = False
    quota_telemetry: bool = False
    context_limit: int | None = None


@dataclass(frozen=True, slots=True)
class MockHealth(ProviderHealth):
    status: str = "healthy"


MockEvent = ProviderEvent


@dataclass(slots=True)
class _Execution:
    scenario: MockScenario
    cancelled: bool = False
    streaming: bool = False
    finished: bool = False


class MockProvider:
    """Runs caller-selected fixtures without accessing a live provider."""

    def __init__(self, scenarios: Mapping[str, MockScenario]) -> None:
        self._scenarios = dict(scenarios)
        for name, scenario in self._scenarios.items():
            validate_id(name)
            if not isinstance(scenario, MockScenario):
                raise TypeError("scenarios must contain MockScenario values")
        self._executions: dict[str, _Execution] = {}

    async def probe(self, profile_id: str) -> MockHealth:
        validate_id(profile_id)
        return MockHealth(profile_id)

    async def capabilities(self, profile_id: str) -> MockCapabilities:
        validate_id(profile_id)
        return MockCapabilities()

    async def usage(self, handle: str) -> None:
        self._execution(handle)
        return None

    async def start(self, request: ExecutionRequest | str) -> str:
        if isinstance(request, ExecutionRequest):
            scenario_name = request.input.get("scenario")
            if not isinstance(scenario_name, str):
                raise ValueError("Mock execution requires a scenario name")
        else:
            scenario_name = request
        validate_id(scenario_name)
        try:
            scenario = self._scenarios[scenario_name]
        except KeyError as exc:
            raise ValueError("Unknown mock scenario") from exc
        handle = str(uuid4())
        self._executions[handle] = _Execution(scenario)
        return handle

    async def cancel(self, handle: str) -> None:
        execution = self._execution(handle)
        if not execution.finished:
            execution.cancelled = True

    async def stream(self, handle: str) -> AsyncIterator[MockEvent]:
        execution = self._execution(handle)
        if execution.streaming or execution.finished:
            raise RuntimeError("Execution stream has already been consumed")
        execution.streaming = True
        try:
            yield MockEvent("started")
            for chunk in execution.scenario.chunks:
                await asyncio.sleep(0)
                if execution.cancelled:
                    break
                yield MockEvent("output", chunk)
            if execution.cancelled:
                yield MockEvent("cancelled")
            else:
                yield MockEvent(execution.scenario.outcome.value)
        finally:
            execution.finished = True

    def _execution(self, handle: str) -> _Execution:
        validate_id(handle)
        try:
            return self._executions[handle]
        except KeyError as exc:
            raise ValueError("Unknown mock execution") from exc
