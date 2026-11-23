"""Consistent run snapshots and event replay cursors."""
import sqlite3
from typing import Any

from synapse.domain.contracts import validate_id
from synapse.persistence.paging import (
    DEFAULT_PAGE_LIMIT, check_page_limit, decode_page_cursor, encode_page_cursor, page_scope,
)



class RunNotFound(LookupError, ValueError):
    """Unknown run; still a ValueError for existing callers."""


def read_run_snapshot(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """Read a run and global event high-water mark in one SQLite snapshot.

    The caller must authorize run access. The returned cursor can be used with
    read_events to replay events committed after this snapshot.
    """
    validate_id(run_id)
    if connection.in_transaction:
        raise RuntimeError("Run snapshot requires its own read transaction")
    connection.execute("BEGIN")
    try:
        run = connection.execute(
            "SELECT id,workflow_id,workflow_version,state,state_version,created_at,updated_at "
            "FROM runs WHERE id=?", (run_id,),
        ).fetchone()
        if run is None:
            raise RunNotFound("Run does not exist")
        config = connection.execute(
            "SELECT c.fixture,c.profile_id,p.provider FROM mock_run_config c "
            "LEFT JOIN profiles p ON p.id=c.profile_id WHERE c.run_id=?", (run_id,)
        ).fetchone()
        rows = connection.execute(
            "SELECT id,node_id,state,state_version,assigned_profile_id,attempt_count,created_at,updated_at "
            "FROM tasks WHERE run_id=? ORDER BY id", (run_id,),
        ).fetchall()
        dependencies = connection.execute(
            "SELECT d.task_id,d.depends_on_id,dep.run_id FROM task_dependencies d "
            "JOIN tasks t ON t.id=d.task_id LEFT JOIN tasks dep ON dep.id=d.depends_on_id "
            "WHERE t.run_id=? ORDER BY d.task_id,d.depends_on_id",
            (run_id,),
        ).fetchall()
        if any(dependency_run != run_id for _, _, dependency_run in dependencies):
            raise RuntimeError("Run contains a missing or cross-run task dependency")
        sequence = connection.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0]
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return {
        "run": dict(id=run[0], workflow_id=run[1], workflow_version=run[2],
                    state=run[3], state_version=run[4], created_at=run[5], updated_at=run[6]),
        "mock_config": None if config is None else {"fixture": config[0], "profile_id": config[1], "provider": config[2]},
        "tasks": [
            dict(id=row[0], node_id=row[1], state=row[2], state_version=row[3],
                 assigned_profile_id=row[4], attempt_count=row[5], created_at=row[6], updated_at=row[7])
            for row in rows
        ],
        "dependencies": [
            {"task_id": task_id, "depends_on_id": dependency_id}
            for task_id, dependency_id, _ in dependencies
        ],
        "last_sequence": sequence,
    }


def read_run_tasks(connection: sqlite3.Connection, run_id: str, *, limit: int = DEFAULT_PAGE_LIMIT,
                   cursor: str | None = None) -> dict[str, Any]:
    """Read one page of a run's tasks (ordered by id) with their attempts under one SQLite snapshot.

    Authorization is the caller's responsibility. Unknown runs raise RunNotFound (a ValueError); a limit outside
    1..MAX_PAGE_LIMIT raises ValueError; a malformed or foreign cursor raises InvalidPageCursor. Without paging
    arguments this returns the first DEFAULT_PAGE_LIMIT tasks, never the whole run. ``next_cursor`` is None on
    the last page. Pages are keyset pages (``id > last id``): tasks added between pages appear on a later page
    only when their id sorts after the cursor.
    """
    validate_id(run_id)
    check_page_limit(limit)
    scope = page_scope("tasks", run_id)
    after = None if cursor is None else decode_page_cursor(cursor, scope, 1)[0]
    if connection.in_transaction:
        raise RuntimeError("Run task query requires its own read transaction")
    connection.execute("BEGIN")
    try:
        if connection.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            raise RunNotFound("Run does not exist")
        rows = connection.execute(
            "SELECT id,node_id,state,state_version,assigned_profile_id,attempt_count,created_at,updated_at "
            "FROM tasks WHERE run_id=? AND (? IS NULL OR id > ?) ORDER BY id LIMIT ?",
            (run_id, after, after, limit + 1),
        ).fetchall()
        page = rows[:limit]
        attempts = []
        if page:
            marks = ",".join("?" * len(page))
            attempts = connection.execute(
                "SELECT a.id,a.task_id,a.attempt_no,a.profile_id,a.state,a.session_id,"
                "a.worktree_path,a.started_at,a.ended_at,a.exit_code FROM task_attempts a "
                f"WHERE a.task_id IN ({marks}) ORDER BY a.task_id,a.attempt_no",
                tuple(row[0] for row in page),
            ).fetchall()
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    grouped: dict[str, list[dict[str, Any]]] = {row[0]: [] for row in page}
    for a in attempts:
        grouped[a[1]].append(dict(id=a[0], attempt_no=a[2], profile_id=a[3], state=a[4],
                                  session_id=a[5], worktree_path=a[6], started_at=a[7],
                                  ended_at=a[8], exit_code=a[9]))
    return {"tasks": [dict(id=row[0], node_id=row[1], state=row[2], state_version=row[3],
                           assigned_profile_id=row[4], attempt_count=row[5], created_at=row[6],
                           updated_at=row[7], attempts=grouped[row[0]]) for row in page],
            "next_cursor": encode_page_cursor(scope, page[-1][0]) if len(rows) > limit else None}
