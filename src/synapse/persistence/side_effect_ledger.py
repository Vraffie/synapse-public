"""Side-effect ledger: no blind replay of irreversible steps (ADR-021).

Before an irreversible step (deployment, publish, external write) runs, the daemon records an *intent* keyed by
``(task_id, attempt_no, candidate_sha256, step)``. Only the caller whose INSERT created the row may execute the step.
After a crash, an intent without a completion is moved to ``unknown`` by :func:`recover_side_effects` and is NEVER
auto-replayed. ``unknown`` resolves to ``done`` or ``not_done`` only through :func:`resolve_unknown` (explicit operator
action) or :func:`reconcile_with_probe` (an injected, read-only probe). Retrying after ``not_done`` needs a new attempt
number, hence a new key.

No network and no deployment code live here. Each function opens and commits its own ``BEGIN IMMEDIATE`` transaction on
an autocommit connection (as ``mock_recovery`` does). Schema: see ``SCHEMA`` (promote to migration 0017 once 0015/0016
are on main; migrations must be contiguous; a migration must cover both the fresh and the pre-environment shape).
Resolution provenance (``source``/``actor``) is caller-supplied and unauthenticated.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import enum
import re
import sqlite3

from synapse.domain.contracts import validate_id
from synapse.persistence.approval_environment import validate_environment

SCHEMA = """
CREATE TABLE IF NOT EXISTS side_effect_ledger (
  task_id TEXT NOT NULL, attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
  candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256) = 64),
  step TEXT NOT NULL, environment TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL CHECK (state IN ('intent','done','unknown','not_done')),
  intent_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  resolution_source TEXT CHECK (resolution_source IN ('worker','operator','probe')),
  resolved_by TEXT, evidence TEXT,
  PRIMARY KEY (task_id, attempt_no, candidate_sha256, step, environment)
);
CREATE TABLE IF NOT EXISTS side_effect_ledger_transitions (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, candidate_sha256 TEXT NOT NULL, step TEXT NOT NULL, environment TEXT NOT NULL DEFAULT '',
  from_state TEXT, to_state TEXT NOT NULL, source TEXT NOT NULL, actor TEXT, evidence TEXT, at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS side_effect_transitions_no_update BEFORE UPDATE ON side_effect_ledger_transitions
BEGIN SELECT RAISE(ABORT, 'side-effect transitions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS side_effect_transitions_no_delete BEFORE DELETE ON side_effect_ledger_transitions
BEGIN SELECT RAISE(ABORT, 'side-effect transitions are append-only'); END;
"""

_SHA256 = re.compile(r"[0-9a-f]{64}")


class LedgerError(RuntimeError):
    """The requested ledger transition is not permitted."""


class Entry(str, enum.Enum):
    PROCEED = "proceed"          # this caller newly recorded the intent and may run the step exactly once
    ALREADY_DONE = "done"        # step known complete; do not run, treat as success
    IN_FLIGHT = "intent"         # another claimant recorded intent; do not run
    UNKNOWN = "unknown"          # outcome unknown; do not run; needs reconciliation
    NOT_DONE = "not_done"        # confirmed not done for THIS attempt; retry needs a new attempt number


class ProbeResult(str, enum.Enum):
    DONE = "done"
    NOT_DONE = "not_done"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class StepKey:
    task_id: str
    attempt_no: int
    candidate_sha256: str
    step: str
    environment: str = ""  # "" = legacy, no environment; else allowlisted id (ADR-022), part of the key

    def __post_init__(self) -> None:
        validate_id(self.task_id)
        validate_id(self.step)
        if self.environment != "":
            validate_environment(self.environment)
        if type(self.attempt_no) is not int or self.attempt_no < 1:
            raise ValueError("attempt_no must be a positive integer")
        if not isinstance(self.candidate_sha256, str) or not _SHA256.fullmatch(self.candidate_sha256):
            raise ValueError("candidate_sha256 must be 64 lowercase hex characters")

    @classmethod
    def from_stored(cls, task_id: str, attempt_no: int, candidate_sha256: str, step: str, environment: str) -> "StepKey":
        """Key for a row already in the ledger: shape is checked, but the environment is NOT checked against the
        current allowlist (an environment may have left it; recovery/resolution must still work). Never use this for
        keys that originate from a caller; those go through the validating constructor."""
        probe = cls(task_id, attempt_no, candidate_sha256, step)  # validates everything except the environment
        if not isinstance(environment, str):
            raise ValueError("stored environment must be a string")
        object.__setattr__(probe, "environment", environment)
        return probe

    def params(self) -> tuple:
        return (self.task_id, self.attempt_no, self.candidate_sha256, self.step, self.environment)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in connection.execute(f"PRAGMA table_info({table})")]


def _run_script(connection: sqlite3.Connection, script: str) -> None:
    buf = ""
    for line in script.splitlines():
        buf += line + "\n"
        if sqlite3.complete_statement(buf):
            connection.execute(buf)
            buf = ""


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the ledger tables if absent; upgrade a pre-environment ledger (PR #138 shape) in place. Idempotent.

    Old shape: no ``environment`` column and a 4-column primary key. Upgrade, in one transaction: add ``environment``
    (default '') to the transitions table; rename the ledger, create the new one, copy rows with environment='', drop
    the renamed table. Rows and transitions are preserved.
    """
    if connection.in_transaction:
        raise RuntimeError("ensure_schema requires an autocommit connection")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if _columns(connection, "side_effect_ledger_transitions") and \
                "environment" not in _columns(connection, "side_effect_ledger_transitions"):
            connection.execute("ALTER TABLE side_effect_ledger_transitions ADD COLUMN environment TEXT NOT NULL DEFAULT ''")
        old = _columns(connection, "side_effect_ledger")
        if old and "environment" not in old:
            connection.execute("ALTER TABLE side_effect_ledger RENAME TO side_effect_ledger_pre_env")
            _run_script(connection, SCHEMA)
            connection.execute(
                "INSERT INTO side_effect_ledger(task_id,attempt_no,candidate_sha256,step,environment,state,intent_at,"
                "updated_at,resolution_source,resolved_by,evidence) SELECT task_id,attempt_no,candidate_sha256,step,'',"
                "state,intent_at,updated_at,resolution_source,resolved_by,evidence FROM side_effect_ledger_pre_env")
            connection.execute("DROP TABLE side_effect_ledger_pre_env")
        else:
            _run_script(connection, SCHEMA)
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


class _Tx:
    def __init__(self, connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            raise RuntimeError("Ledger operations require an autocommit connection")
        self.c = connection

    def __enter__(self) -> sqlite3.Connection:
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.c.execute("COMMIT")
        elif self.c.in_transaction:
            self.c.execute("ROLLBACK")


def _log(c, key: StepKey, frm, to, source, actor, evidence, at) -> None:
    c.execute(
        "INSERT INTO side_effect_ledger_transitions"
        "(task_id,attempt_no,candidate_sha256,step,environment,from_state,to_state,source,actor,evidence,at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*key.params(), frm, to, source, actor, evidence, at))


def _row(c, key: StepKey):
    return c.execute("SELECT state,resolution_source FROM side_effect_ledger WHERE task_id=? AND attempt_no=? "
                     "AND candidate_sha256=? AND step=? AND environment=?", key.params()).fetchone()


def get_state(connection: sqlite3.Connection, key: StepKey) -> str | None:
    row = _row(connection, key)
    return row[0] if row else None


def record_intent(connection: sqlite3.Connection, key: StepKey, *, now: str | None = None) -> Entry:
    """Record intent BEFORE the step. Only ``Entry.PROCEED`` authorizes running it, and only for the one caller."""
    at = now or _now()
    with _Tx(connection) as c:
        row = _row(c, key)
        if row is None:
            c.execute("INSERT INTO side_effect_ledger(task_id,attempt_no,candidate_sha256,step,environment,state,intent_at,updated_at)"
                      " VALUES(?,?,?,?,?,'intent',?,?)", (*key.params(), at, at))
            _log(c, key, None, "intent", "worker", None, None, at)
            return Entry.PROCEED
        return {"done": Entry.ALREADY_DONE, "intent": Entry.IN_FLIGHT, "unknown": Entry.UNKNOWN,
                "not_done": Entry.NOT_DONE}[row[0]]


def record_completion(connection: sqlite3.Connection, key: StepKey, *, now: str | None = None) -> None:
    """Mark the step done after it ran. Idempotent when already done; refused from unknown/not_done."""
    at = now or _now()
    with _Tx(connection) as c:
        row = _row(c, key)
        state = row[0] if row else None
        if state == "done":
            return
        if state != "intent":
            raise LedgerError(f"Cannot complete a step in state {state!r}; unknown requires reconciliation")
        c.execute("UPDATE side_effect_ledger SET state='done',updated_at=?,resolution_source='worker' WHERE task_id=? "
                  "AND attempt_no=? AND candidate_sha256=? AND step=? AND environment=? AND state='intent'", (at, *key.params()))
        _log(c, key, "intent", "done", "worker", None, None, at)


def recover_side_effects(connection: sqlite3.Connection, *, no_workers_survive: bool,
                         now: str | None = None) -> tuple[StepKey, ...]:
    """After a crash: every intent without completion becomes ``unknown``. Never replays anything.

    The caller MUST hold daemon ownership; the boolean is an attestation, not a process fence.
    """
    if no_workers_survive is not True:
        raise ValueError("Explicit assurance that no workers survive is required")
    at = now or _now()
    with _Tx(connection) as c:
        rows = c.execute("SELECT task_id,attempt_no,candidate_sha256,step,environment FROM side_effect_ledger "
                         "WHERE state='intent' ORDER BY intent_at,task_id,attempt_no,step").fetchall()
        keys = tuple(StepKey.from_stored(*r) for r in rows)  # raw rows: not re-validated against the allowlist
        for key in keys:
            c.execute("UPDATE side_effect_ledger SET state='unknown',updated_at=? WHERE task_id=? AND attempt_no=? "
                      "AND candidate_sha256=? AND step=? AND environment=? AND state='intent'", (at, *key.params()))
            _log(c, key, "intent", "unknown", "recovery", None, None, at)
        return keys


def resolve_unknown(connection: sqlite3.Connection, key: StepKey, *, outcome: str, actor: str,
                    evidence: str | None = None, source: str = "operator", now: str | None = None) -> str:
    """Explicit resolution of an ``unknown`` step to ``done`` or ``not_done``.

    Idempotent for the same outcome (and an operator/probe resolution); a conflicting outcome or any other state raises.
    Returns the resulting state.
    """
    if outcome not in ("done", "not_done"):
        raise ValueError("outcome must be 'done' or 'not_done'")
    if source not in ("operator", "probe"):
        raise ValueError("source must be 'operator' or 'probe'")
    validate_id(actor)
    at = now or _now()
    with _Tx(connection) as c:
        row = _row(c, key)
        state = row[0] if row else None
        if state == outcome and row[1] in ("operator", "probe"):
            return state
        if state != "unknown":
            raise LedgerError(f"Only an unknown step can be resolved (state is {state!r})")
        c.execute("UPDATE side_effect_ledger SET state=?,updated_at=?,resolution_source=?,resolved_by=?,evidence=? "
                  "WHERE task_id=? AND attempt_no=? AND candidate_sha256=? AND step=? AND environment=? AND state='unknown'",
                  (outcome, at, source, actor, evidence, *key.params()))
        _log(c, key, "unknown", outcome, source, actor, evidence, at)
        return outcome


def reconcile_with_probe(connection: sqlite3.Connection, key: StepKey,
                         probe: Callable[[StepKey], "tuple[ProbeResult, str] | ProbeResult"], *,
                         actor: str = "probe", now: str | None = None) -> str:
    """Resolve an unknown step with an injected READ-ONLY probe; inconclusive/erroring probes leave it unknown.

    The probe runs outside any ledger transaction. It must only observe (never mutate) the external system.
    """
    if get_state(connection, key) != "unknown":
        raise LedgerError("Only an unknown step can be reconciled")
    try:
        result = probe(key)
    except Exception:
        return "unknown"
    evidence = None
    if isinstance(result, tuple):
        result, evidence = result
    if result == ProbeResult.INCONCLUSIVE:
        return "unknown"
    return resolve_unknown(connection, key, outcome=ProbeResult(result).value, actor=actor,
                           evidence=evidence, source="probe", now=now)
