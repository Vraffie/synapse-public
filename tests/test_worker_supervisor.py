import asyncio

import pytest

from synapse.orchestration.worker_supervisor import WorkerSupervisor
from synapse.providers.contracts import ExecutionRequest
from synapse.providers.mock import MockOutcome, MockProvider, MockScenario


def request(task_id):
    return ExecutionRequest("mock", task_id, {"scenario": "work"})


def test_supervisor_limits_concurrency_and_cancels_waiting_task():
    async def run():
        provider = MockProvider({"work": MockScenario(("output",), MockOutcome.SUCCEEDED)})
        supervisor = WorkerSupervisor(provider, max_concurrency=1)
        started = asyncio.Event()
        release = asyncio.Event()
        first_events = []
        second_events = []

        async def first_sink(event):
            first_events.append(event.kind)
            if event.kind == "started":
                started.set()
                await release.wait()

        async def second_sink(event):
            second_events.append(event.kind)

        first = asyncio.create_task(supervisor.execute(request("one"), first_sink))
        await started.wait()
        second = asyncio.create_task(supervisor.execute(request("two"), second_sink))
        await asyncio.sleep(0)
        assert await supervisor.cancel("two")
        release.set()
        await asyncio.gather(first, second)
        assert first_events == ["started", "output", "succeeded"]
        assert second_events == ["cancelled"]
        assert not await supervisor.cancel("two")

    asyncio.run(run())


def test_cancel_running_task_and_reject_duplicate():
    async def run():
        provider = MockProvider({"work": MockScenario(("output",), MockOutcome.SUCCEEDED)})
        supervisor = WorkerSupervisor(provider)
        entered = asyncio.Event()
        release = asyncio.Event()
        events = []

        async def sink(event):
            events.append(event.kind)
            if event.kind == "started":
                entered.set()
                await release.wait()

        running = asyncio.create_task(supervisor.execute(request("one"), sink))
        await entered.wait()
        with pytest.raises(RuntimeError, match="already"):
            await supervisor.execute(request("one"), sink)
        assert await supervisor.cancel("one")
        release.set()
        await running
        assert events == ["started", "cancelled"]

    asyncio.run(run())


def test_sink_failure_cleans_up_task_and_slot():
    async def run():
        provider = MockProvider({"work": MockScenario((), MockOutcome.SUCCEEDED)})
        supervisor = WorkerSupervisor(provider, max_concurrency=1)

        async def fail(event):
            raise RuntimeError("sink failure")

        async def sink(event):
            pass

        with pytest.raises(RuntimeError, match="sink failure"):
            await supervisor.execute(request("one"), fail)
        await supervisor.execute(request("one"), sink)

    asyncio.run(run())


@pytest.mark.parametrize("limit", [0, 7, True, 1.5])
def test_invalid_concurrency_rejected(limit):
    with pytest.raises(ValueError):
        WorkerSupervisor(MockProvider({}), max_concurrency=limit)
