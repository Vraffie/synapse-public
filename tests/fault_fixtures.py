"""Pytest fixtures shared by the fault-injection test modules (import them into the module)."""
import pytest

from fault_helpers import World
from synapse.providers.mock import MockProvider


@pytest.fixture
def starts(monkeypatch):
    """Counts provider executions: the observable for 'never respawned'."""
    calls = []
    original = MockProvider.start

    async def counting(self, request):
        calls.append(request.task_id)
        return await original(self, request)
    monkeypatch.setattr(MockProvider, "start", counting)
    return calls


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()
