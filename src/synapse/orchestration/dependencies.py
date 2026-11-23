"""Dependency validation and deterministic selection of ready tasks."""
from collections.abc import Iterable
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

from synapse.domain.contracts import validate_id
from synapse.domain.workflow import State


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    id: str
    state: State
    dependencies: tuple[str, ...] = ()
    priority: int = 100

    def __post_init__(self) -> None:
        validate_id(self.id)
        if not isinstance(self.state, State):
            raise ValueError("state must be a task state")
        if not isinstance(self.dependencies, tuple):
            raise TypeError("dependencies must be a tuple")
        for dependency in self.dependencies:
            validate_id(dependency)
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("dependencies must be distinct")
        if type(self.priority) is not int:
            raise ValueError("priority must be an integer")


def select_ready_tasks(
    tasks: Iterable[TaskSnapshot],
    *,
    available_slots: int,
) -> tuple[str, ...]:
    """Select ready tasks with completed dependencies, ordered by priority and ID.

    The caller must supply a consistent snapshot, compute available worker slots,
    and recheck eligibility when claiming a task.
    """
    if type(available_slots) is not int or not 0 <= available_slots <= 6:
        raise ValueError("available_slots must be between 0 and 6")
    by_id: dict[str, TaskSnapshot] = {}
    for task in tasks:
        if not isinstance(task, TaskSnapshot):
            raise TypeError("tasks must contain TaskSnapshot values")
        if task.id in by_id:
            raise ValueError("duplicate task ID")
        by_id[task.id] = task
    graph: dict[str, tuple[str, ...]] = {}
    for task in by_id.values():
        if any(dependency not in by_id for dependency in task.dependencies):
            raise ValueError("unknown task dependency")
        graph[task.id] = task.dependencies
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        raise ValueError("task dependencies contain a cycle") from exc
    eligible = [
        task for task in by_id.values()
        if task.state == State.READY
        and all(by_id[dependency].state == State.COMPLETED for dependency in task.dependencies)
    ]
    eligible.sort(key=lambda task: (task.priority, task.id))
    return tuple(task.id for task in eligible[:available_slots])
