import pytest

from synapse.domain.workflow import State
from synapse.orchestration.dependencies import TaskSnapshot, select_ready_tasks


def test_selection_obeys_dependencies_priority_and_capacity():
    tasks = [
        TaskSnapshot("done", State.COMPLETED),
        TaskSnapshot("blocked", State.READY, ("running",), 0),
        TaskSnapshot("running", State.RUNNING),
        TaskSnapshot("b", State.READY, ("done",), 1),
        TaskSnapshot("a", State.READY, ("done",), 1),
        TaskSnapshot("later", State.READY, (), 2),
    ]
    assert select_ready_tasks(tasks, available_slots=2) == ("a", "b")
    assert select_ready_tasks(tasks, available_slots=6) == ("a", "b", "later")
    assert select_ready_tasks(tasks, available_slots=0) == ()


def test_failed_and_cancelled_dependencies_do_not_unlock_tasks():
    tasks = [
        TaskSnapshot("failed", State.FAILED),
        TaskSnapshot("cancelled", State.CANCELLED),
        TaskSnapshot("one", State.READY, ("failed",)),
        TaskSnapshot("two", State.READY, ("cancelled",)),
    ]
    assert select_ready_tasks(tasks, available_slots=6) == ()


@pytest.mark.parametrize("tasks", [
    [TaskSnapshot("a", State.READY, ("missing",))],
    [TaskSnapshot("a", State.READY, ("b",)), TaskSnapshot("b", State.READY, ("a",))],
    [TaskSnapshot("a", State.READY, ("a",))],
    [TaskSnapshot("a", State.READY), TaskSnapshot("a", State.READY)],
])
def test_invalid_dependency_graphs_are_rejected(tasks):
    with pytest.raises(ValueError):
        select_ready_tasks(tasks, available_slots=1)


@pytest.mark.parametrize("slots", [-1, 7, True, 1.5])
def test_invalid_capacity_is_rejected(slots):
    with pytest.raises(ValueError):
        select_ready_tasks([], available_slots=slots)
