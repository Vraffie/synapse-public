"""Bounded in-process provider execution supervision."""
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from synapse.providers.contracts import ExecutionRequest, ProviderAdapter, ProviderEvent


@dataclass(slots=True)
class _ActiveExecution:
    cancelled: bool = False
    handle: str | None = None


class WorkerSupervisor:
    """Limit active provider executions and route their events to a sink.

    This in-process supervisor does not persist attempts or reconcile workers
    after restart. A daemon application must own durable lifecycle transitions.
    """

    def __init__(self, provider: ProviderAdapter, *, max_concurrency: int = 6) -> None:
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 6:
            raise ValueError("max_concurrency must be between 1 and 6")
        self._provider = provider
        self._slots = asyncio.Semaphore(max_concurrency)
        self._active: dict[str, _ActiveExecution] = {}

    async def execute(
        self,
        request: ExecutionRequest,
        emit: Callable[[ProviderEvent], Awaitable[None]],
    ) -> None:
        if request.task_id in self._active:
            raise RuntimeError("Task already has an active execution")
        execution = _ActiveExecution()
        self._active[request.task_id] = execution
        try:
            async with self._slots:
                if execution.cancelled:
                    await emit(ProviderEvent("cancelled"))
                    return
                execution.handle = await self._provider.start(request)
                try:
                    if execution.cancelled:
                        await self._provider.cancel(execution.handle)
                    async for event in self._provider.stream(execution.handle):
                        await emit(event)
                except BaseException:
                    await asyncio.shield(self._provider.cancel(execution.handle))
                    raise
        finally:
            self._active.pop(request.task_id, None)

    async def cancel(self, task_id: str) -> bool:
        execution = self._active.get(task_id)
        if execution is None:
            return False
        execution.cancelled = True
        if execution.handle is not None:
            await self._provider.cancel(execution.handle)
        return True
