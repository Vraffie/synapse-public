"""Conservative restart reconciliation for in-process mock workers only."""
from __future__ import annotations

import sqlite3

from synapse.domain.contracts import validate_id
from synapse.domain.workflow import State, StateChange
from synapse.persistence.attempt_store import mark_interrupted_attempts_unknown
from synapse.persistence.state_store import StateConflict, write_task_transition


def recover_mock_run(connection: sqlite3.Connection, *, run_id: str, principal: str,
                     no_workers_survive: bool, provider_kind: str = "mock") -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Reconcile one run after exclusive startup; never restart any work.

    The caller MUST hold the daemon ownership lock and establish that no
    previous worker can run or issue external effects. The boolean is explicit
    operator/application attestation, not a process fence. All candidate
    profiles must be of ``provider_kind`` (``mock`` unless a real-agent runtime
    passes its own); failure aborts the entire transaction.
    Returns (newly unknown attempt IDs, newly blocked task IDs).
    """
    if no_workers_survive is not True:
        raise ValueError("Explicit assurance that no workers survive is required")
    validate_id(run_id)
    validate_id(principal)
    if connection.in_transaction:
        raise RuntimeError("Recovery requires an autocommit connection")
    connection.execute("BEGIN IMMEDIATE")
    try:
        run = connection.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise ValueError("Run does not exist")
        rows = connection.execute(
            "SELECT t.id,t.state,t.state_version,p.provider FROM tasks t "
            "LEFT JOIN profiles p ON p.id=t.assigned_profile_id "
            "WHERE t.run_id=? AND (t.state IN ('assigned','running','verifying') OR EXISTS "
            "(SELECT 1 FROM task_attempts a WHERE a.task_id=t.id AND a.state NOT IN ('completed','failed','cancelled'))) ORDER BY t.id",
            (run_id,),
        ).fetchall()
        if rows and run[0] in ("succeeded", "failed", "cancelled"):
            raise StateConflict("Terminal run contains unresolved work")
        allowed = {State.ASSIGNED, State.RUNNING, State.VERIFYING, State.READY, State.WAITING_DEPENDENCIES, State.BLOCKED}
        for task_id, state, _, provider in rows:
            if provider != provider_kind:
                raise StateConflict("Recovery requires mock-owned tasks with known profiles")
            if state not in allowed:
                raise StateConflict("Unresolved attempt has an inconsistent task state")
            non_mock = connection.execute(
                "SELECT 1 FROM task_attempts a LEFT JOIN profiles p ON p.id=a.profile_id "
                "WHERE a.task_id=? "
                "AND (p.provider IS NULL OR p.provider <> ?) LIMIT 1", (task_id, provider_kind),
            ).fetchone()
            if non_mock:
                raise StateConflict("Recovery requires mock-owned attempts with known profiles")
        attempts = mark_interrupted_attempts_unknown(connection, run_id=run_id, principal=principal)
        blocked = []
        for task_id, state, version, _ in rows:
            if state != State.BLOCKED:
                write_task_transition(connection, StateChange(task_id, State(state), State.BLOCKED, version), principal=principal)
                blocked.append(task_id)
        connection.execute("COMMIT")
        return attempts, tuple(blocked)
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
