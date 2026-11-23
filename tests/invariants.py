"""Reusable database invariant checker for fault-injection tests (ARCHITECTURE.md §13).

``check_invariants(db)`` reads one SQLite snapshot and returns every violated invariant it can
establish from the database alone. It never raises for a violation (so a test can assert on the
exact set) and never writes. Invariants that need a live process, the clock or the provider
(single daemon owner, spawn counts, "approval invalid after a candidate change" at execution time)
are NOT decidable from the database; see docs/operations/fault-injection.md for what is covered
elsewhere and how.

Two modes: the default checks a database that may have live workers (a snapshot of a running
daemon, or a crash image before recovery); ``quiescent=True`` additionally asserts that nothing can
still be running (use after a stop, a restart with recovery, or a finished run).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3

TERMINAL_TASK = frozenset({"completed", "failed", "cancelled"})
TERMINAL_RUN = frozenset({"succeeded", "failed", "cancelled"})
TERMINAL_ATTEMPT = frozenset({"completed", "failed", "cancelled"})
LIVE_ATTEMPT = frozenset({"created", "starting", "running"})
# A trigger that must exist for the corresponding "immutable" claim to hold.
REQUIRED_TRIGGERS = (
    "events_prevent_update", "events_prevent_delete", "events_prevent_replacement",
    "messages_prevent_update", "messages_prevent_delete", "messages_prevent_replacement",
    "artifacts_prevent_update", "artifacts_prevent_delete", "artifacts_prevent_replacement",
    "verification_results_no_update", "verification_results_no_delete", "verification_results_no_replace",
    "approval_binding_immutable", "approval_no_delete", "approval_no_replace",
    "workflow_versions_prevent_update", "workflow_versions_prevent_delete", "workflow_versions_prevent_replacement",
)


@dataclass(frozen=True, slots=True)
class Violation:
    invariant: str  # stable id, e.g. "events.outbox"
    message: str

    def __str__(self) -> str:
        return f"{self.invariant}: {self.message}"


def check_invariants(db: sqlite3.Connection, *, quiescent: bool = False,
                     blob_dir: str | Path | None = None) -> list[Violation]:
    """Return all violations found in one read snapshot (empty list = all invariants hold)."""
    out: list[Violation] = []
    owns = not db.in_transaction
    if owns:
        db.execute("BEGIN")
    try:
        for check in (_schema, _events_outbox, _run_history, _task_history, _attempts, _deliveries,
                      _approvals, _verifications, _usage):
            check(db, out, quiescent)
        if blob_dir is not None:
            _artifacts(db, out, Path(blob_dir))
    finally:
        if owns:
            db.execute("ROLLBACK")
    return out


def assert_invariants(db: sqlite3.Connection, **kwargs) -> None:
    violations = check_invariants(db, **kwargs)
    assert not violations, "invariants violated:\n" + "\n".join(f"  - {v}" for v in violations)


# --------------------------------------------------------------------------- helpers
def _add(out: list[Violation], invariant: str, message: str) -> None:
    out.append(Violation(invariant, message))


def _events_by(db, aggregate_type: str, types: tuple[str, ...]):
    """{aggregate_id: [(sequence, type, payload)]} in sequence order; unparseable payloads are skipped."""
    marks = ",".join("?" * len(types))
    found: dict[str, list] = defaultdict(list)
    for seq, agg, kind, payload in db.execute(
            f"SELECT sequence,aggregate_id,type,payload_json FROM events WHERE aggregate_type=? AND type IN ({marks}) "
            "ORDER BY sequence", (aggregate_type, *types)):
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            data = None
        found[agg].append((seq, kind, data if isinstance(data, dict) else {}))
    return found


# --------------------------------------------------------------------------- schema
def _schema(db, out, quiescent) -> None:
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    for name in REQUIRED_TRIGGERS:
        if name not in present:
            _add(out, "schema.immutability", f"required trigger {name} is missing, so its table is not immutable")
    versions = [r[0] for r in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
    if versions != list(range(1, len(versions) + 1)):
        _add(out, "schema.migrations", f"schema_migrations is not contiguous from 1: {versions}")
    for row in db.execute("PRAGMA foreign_key_check").fetchall()[:5]:
        _add(out, "schema.foreign_keys", f"foreign key violation: {tuple(row)}")
    result = db.execute("PRAGMA quick_check").fetchone()
    if result is None or result[0] != "ok":
        _add(out, "schema.integrity", f"quick_check reported {result[0] if result else 'nothing'}")


# --------------------------------------------------------------------------- events / outbox
def _events_outbox(db, out, quiescent) -> None:
    for (event_id,) in db.execute("SELECT e.id FROM events e LEFT JOIN outbox o ON o.event_id=e.id WHERE o.event_id IS NULL LIMIT 5"):
        _add(out, "events.outbox", f"event {event_id} has no outbox row (state change not durably queued)")
    for (event_id,) in db.execute("SELECT o.event_id FROM outbox o LEFT JOIN events e ON e.id=o.event_id WHERE e.id IS NULL LIMIT 5"):
        _add(out, "events.outbox", f"outbox row {event_id} has no event")
    low, high, count = db.execute("SELECT MIN(sequence),MAX(sequence),COUNT(*) FROM events").fetchone()
    if count and (low != 1 or high != count):
        _add(out, "events.sequence", f"event sequence is not gap-free (min {low}, max {high}, count {count})")
    for (event_id, payload) in db.execute("SELECT id,payload_json FROM events"):
        try:
            if not isinstance(json.loads(payload), dict):
                raise ValueError
        except (TypeError, ValueError):
            _add(out, "events.payload", f"event {event_id} payload is not a JSON object")
            break


# --------------------------------------------------------------------------- state histories
def _check_history(out, kind: str, ident: str, state: str, version: int, terminal, events, counted_types) -> None:
    """events: [(seq, type, payload)] already restricted to this aggregate."""
    versions = sorted(p.get("version") for _, t, p in events if t in counted_types)
    if versions != list(range(1, version + 1)):
        _add(out, f"{kind}.history", f"{kind} {ident}: event versions {versions} do not cover 1..{version} exactly once")
    changes = [(s, p) for s, t, p in events if t == f"{kind}.state_changed"]
    changes.sort(key=lambda sp: sp[1].get("version", 0))
    previous = None
    for _, payload in changes:
        before, after = payload.get("before"), payload.get("after")
        if before in terminal:
            _add(out, f"{kind}.terminal", f"{kind} {ident} left terminal state {before!r} (to {after!r})")
        if previous is not None and before != previous:
            _add(out, f"{kind}.history", f"{kind} {ident}: transition from {before!r} but previous ended in {previous!r}")
        previous = after
    if changes and previous != state:
        _add(out, f"{kind}.history", f"{kind} {ident} is {state!r} but its last recorded transition ended in {previous!r}")


def _run_history(db, out, quiescent) -> None:
    runs = _events_by(db, "run", ("run.state_changed",))
    for run_id, state, version in db.execute("SELECT id,state,state_version FROM runs"):
        _check_history(out, "run", run_id, state, version, TERMINAL_RUN, runs.get(run_id, []), {"run.state_changed"})


def _task_history(db, out, quiescent) -> None:
    tasks = _events_by(db, "task", ("task.state_changed", "task.attempt_reserved"))
    for task_id, state, version in db.execute("SELECT id,state,state_version FROM tasks"):
        events = tasks.get(task_id, [])
        _check_history(out, "task", task_id, state, version, TERMINAL_TASK, events,
                       {"task.state_changed", "task.attempt_reserved"})
        if not any(t == "task.state_changed" for _, t, _ in events) and state != "created":
            _add(out, "task.history", f"task {task_id} is {state!r} with no recorded transition")


# --------------------------------------------------------------------------- attempts
def _attempts(db, out, quiescent) -> None:
    history = _events_by(db, "task_attempt", ("task_attempt.state_changed", "task_attempt.unknown",
                                              "task_attempt.mock_reconciled"))
    run_state = {r[0]: r[1] for r in db.execute("SELECT id,state FROM runs")}
    tasks = {r[0]: r[1:] for r in db.execute("SELECT id,run_id,state,attempt_count FROM tasks")}
    per_task: dict[str, list] = defaultdict(list)
    for attempt_id, task_id, number, state in db.execute("SELECT id,task_id,attempt_no,state FROM task_attempts"):
        per_task[task_id].append((number, attempt_id, state))
        run_id, task_state, _ = tasks[task_id]
        if state in LIVE_ATTEMPT:
            if quiescent:
                _add(out, "attempt.live", f"attempt {attempt_id} is {state!r} but no worker can be alive (not recovered)")
            elif run_state.get(run_id) != "running" or task_state not in ("assigned", "running"):
                _add(out, "attempt.live", f"attempt {attempt_id} is {state!r} while run is {run_state.get(run_id)!r} and task {task_state!r}")
        previous = None
        for _, kind, payload in history.get(attempt_id, []):
            before, after = payload.get("before"), payload.get("after")
            if before in TERMINAL_ATTEMPT:
                _add(out, "attempt.terminal", f"attempt {attempt_id} left terminal state {before!r}")
            if previous is not None and before != previous:
                _add(out, "attempt.history", f"attempt {attempt_id}: transition from {before!r} but previous ended in {previous!r}")
            previous = after
        if previous is not None and previous != state:
            _add(out, "attempt.history", f"attempt {attempt_id} is {state!r} but its last recorded transition ended in {previous!r}")
    for task_id, rows in per_task.items():
        numbers = sorted(n for n, _, _ in rows)
        if numbers != list(range(1, len(rows) + 1)) or tasks[task_id][2] != len(rows):
            _add(out, "attempt.count", f"task {task_id}: attempts {numbers} vs attempt_count {tasks[task_id][2]} (a respawn without a recorded attempt?)")
        unresolved = [a for _, a, s in rows if s not in TERMINAL_ATTEMPT]
        if len(unresolved) > 1:
            _add(out, "attempt.unresolved", f"task {task_id} has {len(unresolved)} unresolved attempts {unresolved}")
    for task_id, (run_id, state, count) in tasks.items():
        if count and task_id not in per_task:
            _add(out, "attempt.count", f"task {task_id} has attempt_count {count} but no attempt rows")
    if quiescent:
        for task_id, (run_id, state, _) in tasks.items():
            if state in ("assigned", "running", "verifying") and run_state.get(run_id) != "running":
                _add(out, "task.quiescent", f"task {task_id} is {state!r} but run is {run_state.get(run_id)!r}")


# --------------------------------------------------------------------------- deliveries
def _deliveries(db, out, quiescent) -> None:
    for did, state, owner, until, acked, attempts in db.execute(
            "SELECT id,state,lease_owner,lease_until,acknowledged_at,attempts FROM message_deliveries"):
        if attempts < 0:
            _add(out, "delivery.state", f"delivery {did} has negative attempts")
        if state == "leased" and (not owner or not until):
            _add(out, "delivery.state", f"delivery {did} is leased without an owner and expiry")
        if state != "leased" and (owner or until):
            _add(out, "delivery.state", f"delivery {did} is {state!r} but still carries a lease")
        if state in ("acknowledged", "completed", "failed") and not acked:
            _add(out, "delivery.state", f"delivery {did} is {state!r} without acknowledged_at")
        if state in ("pending", "leased", "dead_letter") and acked:
            _add(out, "delivery.state", f"delivery {did} is {state!r} but was acknowledged")
        if state not in ("pending", "leased", "acknowledged", "completed", "failed", "dead_letter"):
            _add(out, "delivery.state", f"delivery {did} has unknown state {state!r}")
        if state != "pending" and attempts == 0 and state != "dead_letter":
            _add(out, "delivery.state", f"delivery {did} is {state!r} but was never leased")


# --------------------------------------------------------------------------- approvals
def _approvals(db, out, quiescent) -> None:
    for aid, state, decided, principal, requester, expires in db.execute(
            "SELECT id,state,decided_at,decision_principal,requester_subject,expires_at FROM approval_requests"):
        if state in ("approved", "rejected") and (not decided or not principal):
            _add(out, "approval.decision", f"approval {aid} is {state!r} without decided_at/decision_principal")
        if state == "requested" and (decided or principal):
            _add(out, "approval.decision", f"approval {aid} is still requested but carries a decision")
        if requester and principal and requester == principal:
            _add(out, "approval.self", f"approval {aid} was decided by its own requester {principal!r}")
        if state == "approved" and decided and expires and decided > expires:
            _add(out, "approval.expiry", f"approval {aid} was approved at {decided} after it expired at {expires}")
    gates = {r[0]: r[1:] for r in db.execute("SELECT task_id,approval_id,resolved_at FROM task_approval_gates")}
    for task_id, state in db.execute("SELECT id,state FROM tasks"):
        gate = gates.get(task_id)
        if state == "awaiting_approval" and (gate is None or gate[1] is not None):
            _add(out, "approval.gate", f"task {task_id} awaits approval without an unresolved gate")
        if gate is not None and gate[1] is None and state != "awaiting_approval":
            _add(out, "approval.gate", f"task {task_id} has an unresolved gate but is {state!r}")


# --------------------------------------------------------------------------- verifications
def _verifications(db, out, quiescent) -> None:
    artifacts = {r[0]: r[1] for r in db.execute("SELECT id,sha256 FROM artifacts")}
    attempts = {r[0]: r[1:] for r in db.execute("SELECT id,task_id,attempt_no,state FROM task_attempts")}
    approved_for: set[str] = set()
    for vid, task_id, candidate, artifact_id, evidence, attempt_id, outcome in db.execute(
            "SELECT id,task_id,candidate_sha256,candidate_artifact_id,evidence_artifact_id,task_attempt_id,outcome FROM verification_results"):
        if artifact_id is None or artifact_id not in artifacts:
            _add(out, "verification.artifact", f"verification {vid} does not reference an existing candidate artifact")
        elif artifacts[artifact_id] != candidate:
            _add(out, "verification.artifact", f"verification {vid} candidate hash differs from artifact {artifact_id}")
        if evidence is not None and evidence not in artifacts:
            _add(out, "verification.artifact", f"verification {vid} evidence artifact {evidence} is missing")
        attempt = attempts.get(attempt_id)
        if attempt is None or attempt[0] != task_id:
            _add(out, "verification.attempt", f"verification {vid} is not bound to an attempt of its task")
        elif outcome == "approved":
            if attempt[2] != "completed":
                _add(out, "verification.attempt", f"verification {vid} approved an attempt that is {attempt[2]!r}")
            approved_for.add(attempt_id)
    latest = {}
    for attempt_id, (task_id, number, _) in attempts.items():
        if number > latest.get(task_id, (0, None))[0]:
            latest[task_id] = (number, attempt_id)
    for task_id, state in db.execute("SELECT id,state FROM tasks WHERE state='completed'"):
        if task_id not in latest or latest[task_id][1] not in approved_for:
            _add(out, "verification.completed", f"task {task_id} is completed without an approved verification of its latest attempt")


# --------------------------------------------------------------------------- usage
def _usage(db, out, quiescent) -> None:
    for (uid,) in db.execute(
            "SELECT id FROM usage_observations WHERE input_tokens IS NULL AND output_tokens IS NULL "
            "AND cache_read_tokens IS NULL AND cache_write_tokens IS NULL AND reported_total_tokens IS NULL"):
        _add(out, "usage.unknown", f"usage row {uid} reports nothing; unknown usage must record no row (never zero)")


# --------------------------------------------------------------------------- artifacts (optional blob dir)
def _artifacts(db, out, blob_dir: Path) -> None:
    from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
    store = ArtifactBlobStore(blob_dir, max_bytes=1 << 30)
    for aid, sha, size in db.execute("SELECT id,sha256,byte_size FROM artifacts"):
        try:
            data = store.read(sha)
        except Exception as exc:  # missing, unreadable or digest mismatch
            _add(out, "artifact.blob", f"artifact {aid}: blob {sha[:12]} is unavailable ({type(exc).__name__})")
            continue
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
            _add(out, "artifact.blob", f"artifact {aid}: blob does not match its recorded size/hash")
