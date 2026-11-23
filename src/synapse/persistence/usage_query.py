"""Read-only view of the usage ledger for one run (one SQLite read snapshot).

Unknown stays unknown: a field a provider did not report is ``None`` per observation, a total over a field with no
known value is ``None`` (never 0). Coverage is reported PER FIELD: ``coverage[field]["known"]`` is the number of
attempts that reported that field and ``coverage[field]["attempts"]`` the run's attempt count, so a total is a lower
bound exactly when ``known < attempts``. ``attempts_with_usage`` (attempts with ANY field) is kept for compatibility
but says nothing about one particular field.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from synapse.domain.contracts import validate_id
from synapse.persistence.run_snapshot import RunNotFound

_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd")


def read_run_usage(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """Authorization is the caller's responsibility. Unknown runs raise RunNotFound."""
    validate_id(run_id)
    if connection.in_transaction:
        raise RuntimeError("Usage query requires its own read transaction")
    connection.execute("BEGIN")
    try:
        if connection.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            raise RunNotFound("Run does not exist")
        attempts_total = connection.execute(
            "SELECT COUNT(*) FROM task_attempts a JOIN tasks t ON t.id=a.task_id WHERE t.run_id=?", (run_id,)).fetchone()[0]
        rows = connection.execute(
            "SELECT u.id,u.attempt_id,t.id,u.profile_id,u.source,u.confidence,u.observed_at,"
            "u.input_tokens,u.output_tokens,u.cache_read_tokens,u.cache_write_tokens,u.cost_usd "
            "FROM usage_observations u JOIN task_attempts a ON a.id=u.attempt_id JOIN tasks t ON t.id=a.task_id "
            "WHERE t.run_id=? ORDER BY u.observed_at,u.id", (run_id,)).fetchall()
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    observations = [dict(id=r[0], attempt_id=r[1], task_id=r[2], profile_id=r[3], source=r[4], confidence=r[5],
                         observed_at=r[6], **dict(zip(_FIELDS, r[7:]))) for r in rows]
    totals: dict[str, Any] = {}
    coverage: dict[str, Any] = {}
    for field in _FIELDS:
        known = [o[field] for o in observations if o[field] is not None]
        totals[field] = sum(known) if known else None
        coverage[field] = {"known": len({o["attempt_id"] for o in observations if o[field] is not None}),
                           "attempts": attempts_total}
    return {"run_id": run_id, "attempts_total": attempts_total,
            "attempts_with_usage": len({o["attempt_id"] for o in observations}),
            "observations": observations, "totals": totals, "coverage": coverage}
