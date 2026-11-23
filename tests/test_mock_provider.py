import asyncio

import pytest

from synapse.providers.mock import MockOutcome, MockProvider, MockScenario


def test_fixed_scenario_replays_deterministically_for_separate_runs():
    async def run():
        provider = MockProvider({"review": MockScenario(("finding", "fixed"), MockOutcome.SUCCEEDED)})
        first = await provider.start("review")
        second = await provider.start("review")
        a = [event async for event in provider.stream(first)]
        b = [event async for event in provider.stream(second)]
        assert a == b
        assert [(event.kind, event.text) for event in a] == [
            ("started", None), ("output", "finding"),
            ("output", "fixed"), ("succeeded", None),
        ]
        with pytest.raises(RuntimeError):
            [event async for event in provider.stream(first)]

    asyncio.run(run())


def test_cancel_stops_remaining_chunks_and_is_idempotent():
    async def run():
        provider = MockProvider({"work": MockScenario(("one", "two"), MockOutcome.SUCCEEDED)})
        handle = await provider.start("work")
        stream = provider.stream(handle)
        assert (await anext(stream)).kind == "started"
        assert (await anext(stream)).text == "one"
        await provider.cancel(handle)
        await provider.cancel(handle)
        assert [event.kind async for event in stream] == ["cancelled"]

    asyncio.run(run())


def test_failed_scenario_and_unknown_handles():
    async def run():
        provider = MockProvider({"failure": MockScenario((), MockOutcome.FAILED)})
        handle = await provider.start("failure")
        assert [event.kind async for event in provider.stream(handle)] == ["started", "failed"]
        with pytest.raises(ValueError, match="Unknown mock scenario"):
            await provider.start("unknown")
        with pytest.raises(ValueError, match="Unknown mock execution"):
            await provider.cancel("unknown")

    asyncio.run(run())



def test_capabilities_and_unknown_usage_are_explicit():
    async def run():
        provider = MockProvider({"work": MockScenario((), MockOutcome.SUCCEEDED)})
        assert (await provider.probe("mock-profile")).status == "healthy"
        capabilities = await provider.capabilities("mock-profile")
        assert capabilities.streaming and capabilities.cancellation and capabilities.structured_events
        assert not capabilities.resume and not capabilities.mcp
        assert not capabilities.usage_telemetry and not capabilities.quota_telemetry
        assert capabilities.context_limit is None
        handle = await provider.start("work")
        assert await provider.usage(handle) is None
        with pytest.raises(ValueError):
            await provider.capabilities("")
        with pytest.raises(ValueError, match="Unknown mock execution"):
            await provider.usage("unknown")

    asyncio.run(run())


def test_shared_execution_request_runs_mock_scenario():
    from synapse.providers.contracts import ExecutionRequest, ProviderEvent

    async def run():
        provider = MockProvider({"work": MockScenario(("result",), MockOutcome.SUCCEEDED)})
        source = {"scenario": "work", "nested": [1]}
        request = ExecutionRequest(profile_id="profile", task_id="task", input=source)
        source["scenario"] = "other"
        handle = await provider.start(request)
        events = [event async for event in provider.stream(handle)]
        assert events == [ProviderEvent("started"), ProviderEvent("output", "result"), ProviderEvent("succeeded")]
        with pytest.raises(ValueError, match="scenario"):
            await provider.start(ExecutionRequest("profile", "task", {}))

    asyncio.run(run())
