"""Consistent, cursor-paginated run listing (newest first)."""
import base64
import binascii
import json
import sqlite3
from typing import Any

from synapse.domain.runs import RunState

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


class InvalidCursor(ValueError):
    """Malformed or tampered pagination cursor."""


def encode_cursor(created_at: str, run_id: str) -> str:
    raw = json.dumps([created_at, run_id], ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        if not cursor.isascii() or len(cursor) > 1024:
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if (not isinstance(value, list) or len(value) != 2
                or not all(isinstance(item, str) and 0 < len(item) <= 256 for item in value)):
            raise ValueError
        return value[0], value[1]
    except (ValueError, binascii.Error):
        raise InvalidCursor("Invalid cursor") from None


def read_run_list(connection: sqlite3.Connection, *, state: str | None = None,
                  limit: int = DEFAULT_LIMIT, cursor: str | None = None) -> dict[str, Any]:
    """Read one page of runs with task-state counts and pending approvals in one snapshot.

    Order is (created_at, id) descending, a total order, so pages are stable while
    new runs are created. Authorization is the caller's responsibility.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("Invalid page size")
    if state is not None and state not in {s.value for s in RunState}:
        raise ValueError("Invalid state")
    after = decode_cursor(cursor) if cursor else None
    if connection.in_transaction:
        raise RuntimeError("Run list requires its own read transaction")
    where: list[str] = []
    params: list[Any] = []
    if state is not None:
        where.append("state=?")
        params.append(state)
    if after is not None:
        where.append("(created_at,id) < (?,?)")
        params.extend(after)
    connection.execute("BEGIN")
    try:
        rows = connection.execute(
            "SELECT id,workflow_id,workflow_version,state,state_version,created_at,updated_at FROM runs"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY created_at DESC,id DESC LIMIT ?", (*params, limit + 1),
        ).fetchall()
        page = rows[:limit]
        ids = [row[0] for row in page]
        marks = ",".join("?" * len(ids))
        counts: dict[str, dict[str, int]] = {run_id: {} for run_id in ids}
        pending = dict.fromkeys(ids, 0)
        if ids:
            for run_id, task_state, count in connection.execute(
                    f"SELECT run_id,state,COUNT(*) FROM tasks WHERE run_id IN ({marks}) "
                    "GROUP BY run_id,state ORDER BY state", ids):
                counts[run_id][task_state] = count
            for run_id, count in connection.execute(
                    "SELECT run_id,COUNT(*) FROM approval_requests WHERE state='requested' "
                    f"AND run_id IN ({marks}) GROUP BY run_id", ids):
                pending[run_id] = count
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    runs = [dict(id=r[0], workflow_id=r[1], workflow_version=r[2], state=r[3], state_version=r[4],
                 created_at=r[5], updated_at=r[6], task_counts=counts[r[0]], pending_approvals=pending[r[0]])
            for r in page]
    more = len(rows) > limit
    return {"runs": runs, "next_cursor": encode_cursor(page[-1][5], page[-1][0]) if more else None}
