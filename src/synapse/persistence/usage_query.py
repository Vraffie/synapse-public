"""Read-only view of the usage ledger for one run (one SQLite read snapshot).

Unknown stays unknown: a field a provider did not report is ``None`` per observation, a total over a field with no
known value is ``None`` (never 0). Coverage is reported PER FIELD: ``coverage[field]["known"]`` is the number of
attempts that reported that field and ``coverage[field]["attempts"]`` the run's attempt count, so a total is a lower
bound exactly when ``known < attempts``. ``attempts_with_usage`` (attempts with ANY field) is kept for compatibility
but says nothing about one particular field.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from synapse.domain.contracts import validate_id
from synapse.persistence.paging import (
    DEFAULT_PAGE_LIMIT, check_page_limit, decode_page_cursor, encode_page_cursor, page_scope,
)
from synapse.persistence.run_snapshot import RunNotFound

_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_FIELDS = _TOKEN_FIELDS + ("cost_usd",)
_RUN_OBSERVATIONS = ("FROM usage_observations u JOIN task_attempts a ON a.id=u.attempt_id JOIN tasks t ON t.id=a.task_id "
                     "WHERE t.run_id=?")


def read_run_usage(connection: sqlite3.Connection, run_id: str, *, limit: int = DEFAULT_PAGE_LIMIT,
                   cursor: str | None = None) -> dict[str, Any]:
    """Authorization is the caller's responsibility. Unknown runs raise RunNotFound.

    ``totals``, ``coverage``, ``attempts_total`` and ``attempts_with_usage`` always describe the WHOLE run (SQL
    aggregates and streamed sums in the same read snapshot as the page); only ``observations`` is paged, ordered by
    (observed_at, id), at most ``limit`` (1..MAX_PAGE_LIMIT) per page. ``next_cursor`` is None on the last page.
    A malformed or foreign cursor raises InvalidPageCursor. An observation committed between pages is counted in
    the next page's totals but listed only if it sorts after the cursor.
    """
    validate_id(run_id)
    check_page_limit(limit)
    scope = page_scope("usage", run_id)
    after = None if cursor is None else decode_page_cursor(cursor, scope, 2)
    if connection.in_transaction:
        raise RuntimeError("Usage query requires its own read transaction")
    connection.execute("BEGIN")
    try:
        if connection.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            raise RunNotFound("Run does not exist")
        attempts_total = connection.execute(
            "SELECT COUNT(*) FROM task_attempts a JOIN tasks t ON t.id=a.task_id WHERE t.run_id=?", (run_id,)).fetchone()[0]
        # Coverage counts in SQL (COUNT(DISTINCT ...) skips NULLs). Totals are summed in Python over streamed
        # scans of the whole run: SQLite SUM() raises "integer overflow" past 2**63-1 (providers accept
        # counts up to that, so two large rows would 500 every page), while Python ints stay exact; cost uses
        # math.fsum, which is correctly rounded and independent of row order and SQLite version.
        counts = connection.execute(
            "SELECT COUNT(DISTINCT u.attempt_id)," + ",".join(
                f"COUNT(DISTINCT CASE WHEN u.{f} IS NOT NULL THEN u.attempt_id END)" for f in _FIELDS)
            + " " + _RUN_OBSERVATIONS, (run_id,)).fetchone()
        totals: dict[str, Any] = dict.fromkeys(_FIELDS)  # None until some observation reports the field
        for values in connection.execute("SELECT " + ",".join(f"u.{f}" for f in _TOKEN_FIELDS) + " " + _RUN_OBSERVATIONS,
                                         (run_id,)):
            for field, value in zip(_TOKEN_FIELDS, values):
                if value is not None:
                    totals[field] = (totals[field] or 0) + value
        if counts[len(_FIELDS)]:  # some attempt reported cost
            totals["cost_usd"] = math.fsum(row[0] for row in connection.execute(
                "SELECT u.cost_usd " + _RUN_OBSERVATIONS + " AND u.cost_usd IS NOT NULL", (run_id,)))
        keyset = "" if after is None else " AND (u.observed_at,u.id) > (?,?)"
        rows = connection.execute(
            "SELECT u.id,u.attempt_id,t.id,u.profile_id,u.source,u.confidence,u.observed_at,"
            "u.input_tokens,u.output_tokens,u.cache_read_tokens,u.cache_write_tokens,u.cost_usd "
            + _RUN_OBSERVATIONS + keyset + " ORDER BY u.observed_at,u.id LIMIT ?",
            (run_id, *(after or ()), limit + 1)).fetchall()
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    page = rows[:limit]
    observations = [dict(id=r[0], attempt_id=r[1], task_id=r[2], profile_id=r[3], source=r[4], confidence=r[5],
                         observed_at=r[6], **dict(zip(_FIELDS, r[7:]))) for r in page]
    coverage = {field: {"known": counts[1 + index], "attempts": attempts_total} for index, field in enumerate(_FIELDS)}
    next_cursor = encode_page_cursor(scope, page[-1][6], page[-1][0]) if len(rows) > limit else None
    return {"run_id": run_id, "attempts_total": attempts_total, "attempts_with_usage": counts[0],
            "observations": observations, "totals": totals, "coverage": coverage, "next_cursor": next_cursor}
