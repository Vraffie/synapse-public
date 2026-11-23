"""Fault injection: kill or fail the system at precise points and assert recovery invariants.

Invariants: no lost or duplicated side effects, no approval granted without a matching
decision, runs end in a consistent recoverable state, restart is idempotent.

Kill points use real SIGKILL of a child process (deterministic: the child kills itself at the
injected point and the parent waits on the exit status, no sleeps). Failure points use a wrapper
connection that raises SQLite errors at the Nth write. Tests add no source changes and reuse
the fake CLI harness from tests/test_agent_runtime.py.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import asyncio
import json
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys

import pytest

from synapse.application.approval_service import ApprovalService
from synapse.domain.approvals import ApprovalChange, ApprovalState
from synapse.domain.contracts import ApprovalId
from synapse.domain.workflow import State, plan_transition
from synapse.persistence.approval_store import write_approval_request, write_approval_transition
from synapse.persistence.attempt_store import reserve_attempt
from synapse.persistence.backup import create_backup, restore_backup
from synapse.persistence.dispatcher import dispatch_batch
from synapse.persistence.event_store import pending_outbox
from synapse.persistence.idempotency import execute_idempotent
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.state_store import transition_task
from tests.test_agent_runtime import Env, GATE, finish

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
PRELUDE = (
    "import os, signal, sys, json\n"
    "def die():\n"
    "    os.kill(os.getpid(), signal.SIGKILL)\n"
)
DIGEST = "a" * 64


def run_child(code, *args, timeout=30):
    """Run ``code`` in a fresh interpreter; return (exit status, stderr); negative = signal."""
    done = subprocess.run([sys.executable, "-c", PRELUDE + code, *map(str, args)], cwd=ROOT,
                          capture_output=True, text=True, timeout=timeout)
    return done.returncode, done.stderr


def seed(connection):
    connection.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
    connection.execute("INSERT INTO runs VALUES('run','wf','1','running',0,'now','now')")
    connection.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) VALUES('profile','mock','Mock','mock','now')")
    for task, state, version in (("task-a", "created", 0), ("task-b", "assigned", 2)):
        connection.execute(
            "INSERT INTO tasks(id,run_id,node_id,state,state_version,assigned_profile_id,created_at,updated_at) "
            "VALUES(?,'run',?,?,?,'profile','now','now')", (task, task, state, version))


@pytest.fixture
def dbpath(tmp_path):
    path = tmp_path / "state.sqlite"
    with closing(open_database(path)) as connection:
        apply_migrations(connection, MIGRATIONS)
        seed(connection)
    return path


def snapshot(connection):
    """Every row of every table whose partial update would violate an invariant."""
    out = {}
    for table in ("tasks", "task_attempts", "events", "outbox", "idempotency_keys", "approval_requests", "audit_entries"):
        out[table] = connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
    return out


def assert_event_outbox_consistent(connection):
    assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    # Every event has exactly one outbox row and vice versa: no lost or orphaned side effect.
    assert connection.execute("SELECT count(*) FROM events WHERE id NOT IN (SELECT event_id FROM outbox)").fetchone() == (0,)
    assert connection.execute("SELECT count(*) FROM outbox WHERE event_id NOT IN (SELECT id FROM events)").fetchone() == (0,)


# --- SIGKILL around event write / outbox dispatch -------------------------------------------
CHILD_OUTBOX = (
    "from synapse.domain.workflow import State, plan_transition\n"
    "from synapse.persistence.migrations import open_database\n"
    "from synapse.persistence.state_store import transition_task, write_task_transition\n"
    "from synapse.persistence.dispatcher import dispatch_batch\n"
    "change = plan_transition('task-a', State.CREATED, State.READY, 0)\n"
    "path, mode, log = sys.argv[1:4]\n"
    "db = open_database(path)\n"
    "if mode == 'mid_txn':\n"
    "    db.execute('BEGIN IMMEDIATE')\n"
    "    write_task_transition(db, change, principal='daemon')\n"
    "    die()  # event, outbox and state rows written but never committed\n"
    "transition_task(db, change, principal='daemon')\n"
    "if mode == 'after_commit':\n"
    "    die()  # committed, nothing dispatched yet\n"
    "def publish(event):\n"
    "    with open(log, 'a') as f:\n"
    "        f.write(event['id'] + '\\n'); f.flush(); os.fsync(f.fileno())\n"
    "    if mode == 'after_publish':\n"
    "        die()  # delivered but never acknowledged\n"
    "dispatch_batch(db, publish)\n"
)


def test_sigkill_mid_transaction_leaves_no_partial_rows(dbpath, tmp_path):
    with closing(open_database(dbpath)) as db:
        before = snapshot(db)
    code, _ = run_child(CHILD_OUTBOX, dbpath, "mid_txn", tmp_path / "log")
    assert code == -signal.SIGKILL
    with closing(open_database(dbpath)) as db:  # restart: WAL recovery discards the open txn
        assert snapshot(db) == before
        assert_event_outbox_consistent(db)
        # The same transition can still be committed exactly once afterwards.
        assert transition_task(db, plan_transition("task-a", State.CREATED, State.READY, 0), principal="daemon") == 1
        assert db.execute("SELECT count(*) FROM events").fetchone() == (1,)


def test_sigkill_between_event_commit_and_dispatch_delivers_exactly_once(dbpath, tmp_path):
    code, _ = run_child(CHILD_OUTBOX, dbpath, "after_commit", tmp_path / "log")
    assert code == -signal.SIGKILL
    published = []
    with closing(open_database(dbpath)) as db:
        ids = pending_outbox(db)
        assert len(ids) == 1  # the event survived the kill and is still owed to subscribers
        assert db.execute("SELECT state,state_version FROM tasks WHERE id='task-a'").fetchone() == ("ready", 1)
        summary = dispatch_batch(db, lambda event: published.append(event["id"]))
        assert (summary.delivered, summary.retried, summary.missing) == (1, 0, 0)
        # Restarting again must not redeliver an acknowledged event.
        assert dispatch_batch(db, lambda event: published.append(event["id"])).delivered == 0
        assert published == ids and pending_outbox(db) == []
        assert_event_outbox_consistent(db)


def test_sigkill_after_publish_before_ack_redelivers_same_id_never_loses_it(dbpath, tmp_path):
    log = tmp_path / "log"
    code, _ = run_child(CHILD_OUTBOX, dbpath, "after_publish", log)
    assert code == -signal.SIGKILL
    first = log.read_text().split()
    assert len(first) == 1
    with closing(open_database(dbpath)) as db:
        assert pending_outbox(db) == first  # not acknowledged, so not lost
        dispatch_batch(db, lambda event: log.write_text(log.read_text() + event["id"] + "\n"))
        assert pending_outbox(db) == []
        assert db.execute("SELECT attempts FROM outbox").fetchone() == (1,)
    delivered = log.read_text().split()
    # At-least-once: the id repeats, and an id-deduplicating consumer sees exactly one effect.
    assert delivered == first * 2 and len(set(delivered)) == 1


# --- failing connection: sqlite write error / disk full ------------------------------------
class FailingConnection:
    """Delegating connection that raises SQLITE_FULL at the Nth write or COMMIT statement."""

    def __init__(self, real, fail_at):
        self._real, self._fail_at, self.writes = real, fail_at, 0

    def execute(self, sql, *args):
        head = sql.lstrip().split(None, 1)[0].upper()
        if head in ("INSERT", "UPDATE", "DELETE", "COMMIT"):
            self.writes += 1
            if self.writes == self._fail_at:
                raise sqlite3.OperationalError("database or disk is full")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def exhaust_failure_points(db, operation):
    """Fail ``operation`` at every write in turn; each failure must leave state untouched and the
    first run with no injected failure must succeed. Returns the number of failure points."""
    baseline = snapshot(db)
    n = 1
    while True:
        wrapper = FailingConnection(db, n)
        try:
            operation(wrapper)
        except sqlite3.OperationalError as exc:
            assert "disk is full" in str(exc)
            assert not db.in_transaction
            assert snapshot(db) == baseline, f"partial state after failure at write {n}"
            assert_event_outbox_consistent(db)
            n += 1
            continue
        break
    assert n > 1, "operation performed no injectable write"
    assert snapshot(db) != baseline
    assert_event_outbox_consistent(db)
    return n - 1


def test_write_failure_during_task_transition_rolls_back_every_step(dbpath):
    with closing(open_database(dbpath)) as db:
        points = exhaust_failure_points(
            db, lambda c: transition_task(c, plan_transition("task-a", State.CREATED, State.READY, 0), principal="daemon"))
        assert points >= 3  # state update, event, outbox, commit
        assert db.execute("SELECT state_version FROM tasks WHERE id='task-a'").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM events").fetchone() == (1,)


def test_write_failure_during_attempt_reservation_never_leaves_a_half_reserved_task(dbpath):
    def op(conn):
        return execute_idempotent(
            conn, principal="daemon", operation="reserve", key="k", request={"t": "task-b"},
            execute=lambda c: reserve_attempt(c, task_id="task-b", attempt_id="att", expected_task_version=2, principal="daemon"))
    with closing(open_database(dbpath)) as db:
        points = exhaust_failure_points(db, op)
        assert points >= 5
        assert db.execute("SELECT attempt_count,state_version FROM tasks WHERE id='task-b'").fetchone() == (1, 3)
        assert db.execute("SELECT count(*) FROM task_attempts").fetchone() == (1,)
        assert op(db).replayed  # a retry after the failures replays rather than reserving twice
        assert db.execute("SELECT count(*) FROM task_attempts").fetchone() == (1,)


def test_write_failure_during_approval_decision_never_grants_approval(dbpath):
    with closing(open_database(dbpath)) as db:
        db.execute("BEGIN IMMEDIATE")
        write_approval_request(db, approval_id="appr", run_id="run", operation="push", target="repo",
                               candidate_sha256=DIGEST, policy_version="1",
                               expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        db.execute("COMMIT")

        def decide(conn):
            return execute_idempotent(
                conn, principal="human", operation="approval.decide", key="d", request={"a": "appr"},
                execute=lambda c: write_approval_transition(
                    c, ApprovalChange(ApprovalId("appr"), ApprovalState.REQUESTED, ApprovalState.APPROVED, 0),
                    principal="human", candidate_sha256=DIGEST, policy_version="1"))
        exhaust_failure_points(db, decide)
        row = db.execute("SELECT state,state_version,decision_principal FROM approval_requests").fetchone()
        assert row == ("approved", 1, "human")
        assert db.execute("SELECT count(*) FROM events WHERE type='approval.state_changed'").fetchone() == (1,)


# --- crash mid-migration --------------------------------------------------------------------
CHILD_MIGRATE = (
    "from pathlib import Path\n"
    "from synapse.persistence.migrations import open_database, apply_migrations\n"
    "path, directory, kill_at = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])\n"
    "class Killer:\n"
    "    def __init__(self, real): self._real = real\n"
    "    def execute(self, sql, *args):\n"
    "        if sql.startswith('INSERT INTO schema_migrations') and args[0][0] == kill_at:\n"
    "            die()  # this migration's DDL is applied but its transaction is not committed\n"
    "        return self._real.execute(sql, *args)\n"
    "    def __getattr__(self, name): return getattr(self._real, name)\n"
    "apply_migrations(Killer(open_database(path)), directory)\n"
)


def schema_versions(path):
    with closing(sqlite3.connect(path)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone() is None:
            return []
        return [r[0] for r in db.execute("SELECT version FROM schema_migrations ORDER BY 1")]


def migration_count():
    return len(list(MIGRATIONS.glob("*.sql")))


@pytest.mark.parametrize("kill_at", ["first", "last"])
def test_sigkill_during_fresh_migration_restart_completes(tmp_path, kill_at):
    path = tmp_path / "fresh.sqlite"
    total = migration_count()
    code, _ = run_child(CHILD_MIGRATE, path, MIGRATIONS, 1 if kill_at == "first" else total)
    assert code == -signal.SIGKILL
    assert schema_versions(path) == []  # all-or-nothing: the single migration txn never committed
    with closing(open_database(path)) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert apply_migrations(db, MIGRATIONS) == list(range(1, total + 1))
        assert apply_migrations(db, MIGRATIONS) == []  # idempotent restart
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_sigkill_during_upgrade_preserves_data_and_restart_finishes(tmp_path):
    total = migration_count()
    if total < 2:
        pytest.skip("needs at least two migrations")
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    for p in sorted(MIGRATIONS.glob("*.sql"))[:-1]:
        shutil.copy(p, prefix / p.name)
    path = tmp_path / "upgrade.sqlite"
    with closing(open_database(path)) as db:
        apply_migrations(db, prefix)
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
    code, _ = run_child(CHILD_MIGRATE, path, MIGRATIONS, total)
    assert code == -signal.SIGKILL
    assert schema_versions(path) == list(range(1, total))  # still the old, usable version
    with closing(open_database(path)) as db:
        assert apply_migrations(db, MIGRATIONS) == [total]
        assert schema_versions(path) == list(range(1, total + 1))
        assert db.execute("SELECT count(*) FROM workflow_versions").fetchone() == (1,)
        assert apply_migrations(db, MIGRATIONS) == []


# --- agent runtime: kill at precise points --------------------------------------------------
CHILD_AGENT = (
    "import asyncio\n"
    "from pathlib import Path\n"
    "from synapse.daemon.agent_runtime import AgentRuntime\n"
    "from synapse.persistence.migrations import open_database\n"
    "from synapse.providers.claude_cli import ClaudeCliProfile, ClaudeCliProvider\n"
    "cfg = json.loads(sys.argv[1]); point = cfg['point']\n"
    "profile = ClaudeCliProfile(executable=cfg['exe'], allowed_tools=tuple(cfg['tools']), state_dir=cfg['state'],\n"
    "                           enabled=True, term_grace_seconds=0.3, permission_mode='plan')\n"
    "class Provider(ClaudeCliProvider):\n"
    "    async def start(self, request):\n"
    "        if point == 'before_start':\n"
    "            die()  # attempt committed as running; provider never entered, so nothing can spawn\n"
    "        return await super().start(request)\n"
    "    async def stream(self, handle):\n"
    "        if point == 'before_spawn':\n"
    "            die()  # last instant before the CLI would be spawned\n"
    "        async for event in super().stream(handle):\n"
    "            yield event\n"
    "if point == 'in_reservation':\n"
    "    import synapse.orchestration.mock_engine as engine_module\n"
    "    real = engine_module.reserve_attempt\n"
    "    def reserve_then_die(*a, **k):\n"
    "        real(*a, **k)\n"
    "        die()  # attempt rows written inside the open transaction, never committed\n"
    "    engine_module.reserve_attempt = reserve_then_die\n"
    "db = open_database(cfg['db'])\n"
    "runtime = AgentRuntime(db, Path(cfg['blobs']), profile=profile, provider_factory=lambda *a, **k: Provider(*a, **k))\n"
    "runtime._park_poll = 3600  # the engine must not react to a decision before being killed\n"
    "async def main():\n"
    "    runtime.start(cfg['run_id'], 'child-start', {'work': cfg['spec']})\n"
    "    if point == 'parked':\n"
    "        while db.execute('SELECT state FROM tasks').fetchone()[0] != 'awaiting_approval':\n"
    "            await asyncio.sleep(0.01)\n"
    "        print('parked', flush=True)\n"
    "        await asyncio.sleep(3600)\n"
    "    await runtime._active\n"
    "asyncio.run(main())\n"
)


def child_config(env, point, **spec_over):
    return json.dumps({"point": point, "exe": str(env.exe), "tools": ["Read", "Edit", "Bash(git status)"],
                       "state": str(env.state), "db": str(env.path), "blobs": str(env.blobs),
                       "run_id": env.run_id, "spec": env.spec(**spec_over)})


def restart(env):
    env.db.close()
    env.db = open_database(env.path)
    env.runtime = env.new_runtime()
    env.runtime.recover_interrupted()


def counts(env):
    return {t: env.db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("events", "outbox", "task_attempts", "audit_entries", "approval_requests")}


def park_child(env):
    """Start a daemon-like child whose gated task is parked; returns once it is awaiting approval."""
    proc = subprocess.Popen([sys.executable, "-c", PRELUDE + CHILD_AGENT, child_config(env, "parked", approval_step=GATE)],
                            cwd=ROOT, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "parked"  # blocks until the condition holds
    return proc


def kill_child(proc):
    try:
        proc.kill()
        assert proc.wait(timeout=10) == -signal.SIGKILL
    finally:
        proc.stdout.close()


@pytest.mark.parametrize("point,attempts", [("in_reservation", []), ("before_start", ["unknown"]), ("before_spawn", ["unknown"])])
def test_sigkill_between_attempt_reservation_and_spawn_never_double_spawns(tmp_path, point, attempts):
    env = Env(tmp_path)
    try:
        code, err = run_child(CHILD_AGENT, child_config(env, point))
        assert code == -signal.SIGKILL, err
        assert env.spawns() == []
        restart(env)
        run_state, tasks = env.states()
        assert run_state in ("recovering", "running") and set(tasks.values()) <= {"blocked", "ready", "created"}
        assert env.attempts() == attempts  # a possibly-started attempt is unknown, never retried
        assert not env.runtime.resume_parked()
        first, before = counts(env), env.states()
        restart(env)  # idempotent restart: nothing new, still no spawn
        assert counts(env) == first and env.states() == before
        assert env.spawns() == []
        assert_event_outbox_consistent(env.db)
        assert env.db.execute("SELECT count(*) FROM task_attempts WHERE state IN ('created','starting','running')").fetchone() == (0,)
    finally:
        env.close()


def test_sigkill_after_approval_decision_committed_before_engine_reacts(tmp_path):
    env = Env(tmp_path)
    try:
        proc = park_child(env)
        try:
            assert env.spawns() == []
            env.decide()  # decision committed while the daemon is alive
        finally:
            kill_child(proc)  # engine never saw it (park poll is an hour)
        assert env.spawns() == []
        assert env.db.execute("SELECT state FROM approval_requests").fetchone() == ("approved",)
        restart(env)
        assert env.runtime._pending_redrive == env.run_id

        async def go():
            assert env.runtime.resume_parked()
            await finish(env)
        asyncio.run(go())
        assert env.states() == ("succeeded", {"work": "completed"})
        assert env.spawns() == ["ok"]  # exactly one side effect, after the matching decision
        done = counts(env)
        restart(env)  # restart of a finished run changes nothing and spawns nothing
        assert not env.runtime.resume_parked()
        assert counts(env) == done and env.spawns() == ["ok"]
        assert_event_outbox_consistent(env.db)
    finally:
        env.close()


def test_crash_while_parked_without_decision_or_with_mismatched_one_never_spawns(tmp_path):
    env = Env(tmp_path)
    try:
        kill_child(park_child(env))
        restart(env)
        assert env.runtime._pending_redrive == env.run_id

        async def go():
            env.runtime.resume_parked()
            await env.until(lambda: env.states()[1]["work"] == "awaiting_approval")
            # A decision bound to a different candidate is refused and grants nothing.
            aid = env.db.execute("SELECT id FROM approval_requests").fetchone()[0]
            with pytest.raises(Exception):
                ApprovalService(env.db, env.authority).decide(
                    env.token, approval_id=aid, expected_version=0, decision="approved", operation="agent.run",
                    target="repo", candidate_sha256="0" * 64, policy_version="1",
                    idempotency_key="d-mismatch", correlation_id="t")
            assert env.db.execute("SELECT state FROM approval_requests").fetchone() == ("requested",)
            env.runtime._active.cancel()
            await asyncio.gather(env.runtime._active, return_exceptions=True)
        asyncio.run(go())
        assert env.spawns() == []
        assert env.states()[1]["work"] == "awaiting_approval"
        assert env.db.execute("SELECT count(*) FROM approval_requests").fetchone() == (1,)  # not re-requested
    finally:
        env.close()


# --- backup during writes / interrupted backup and restore ---------------------------------
@pytest.fixture
def state(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    with closing(open_database(state / "synapse.sqlite")) as db:
        apply_migrations(db, MIGRATIONS)
        seed(db)
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    return state, out


def test_backup_during_open_write_transaction_excludes_uncommitted_rows(state):
    state_dir, out = state
    writer = open_database(state_dir / "synapse.sqlite")
    try:
        transition_task(writer, plan_transition("task-a", State.CREATED, State.READY, 0), principal="daemon")
        writer.execute("BEGIN IMMEDIATE")
        # Half of a command: state and event written, outbox row not yet.
        writer.execute("UPDATE tasks SET state='running',state_version=state_version+1 WHERE id='task-a'")
        writer.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
                       "VALUES('half','task','task-a','run','task.state_changed',1,'{}','now')")
        create_backup(state_dir, out / "snap.sqlite", MIGRATIONS)
        writer.execute("ROLLBACK")
    finally:
        writer.close()
    with closing(sqlite3.connect(out / "snap.sqlite")) as snap:
        assert snap.execute("SELECT state,state_version FROM tasks WHERE id='task-a'").fetchone() == ("ready", 1)
        assert_event_outbox_consistent(snap)
        assert snap.execute("SELECT count(*) FROM events").fetchone() == (1,)
    restore_backup(state_dir, out / "snap.sqlite", MIGRATIONS)
    with closing(open_database(state_dir / "synapse.sqlite")) as db:
        assert db.execute("SELECT state,state_version FROM tasks WHERE id='task-a'").fetchone() == ("ready", 1)
        assert_event_outbox_consistent(db)
        assert len(pending_outbox(db)) == 1  # undelivered events survive the restore and still dispatch
        assert dispatch_batch(db, lambda e: None).delivered == 1


CHILD_BACKUP = (
    "from pathlib import Path\n"
    "import synapse.persistence.backup as backup\n"
    "state, dest, migrations, mode = sys.argv[1:5]\n"
    "if mode == 'backup':\n"
    "    real = backup._snapshot\n"
    "    def snapshot_then_die(source, target):\n"
    "        real(source, target); die()  # temp copy complete, never published under its final name\n"
    "    backup._snapshot = snapshot_then_die\n"
    "    backup.create_backup(Path(state), Path(dest), Path(migrations))\n"
    "else:\n"
    "    real_link = os.link\n"
    "    def link(src, dst, **kw):\n"
    "        if dst == 'synapse.sqlite':\n"
    "            die()  # old database moved aside, new one not yet linked in\n"
    "        return real_link(src, dst, **kw)\n"
    "    os.link = link\n"
    "    backup.restore_backup(Path(state), Path(dest), Path(migrations))\n"
)


def test_sigkill_during_backup_publishes_nothing_and_retry_succeeds(state):
    state_dir, out = state
    code, _ = run_child(CHILD_BACKUP, state_dir, out / "b.sqlite", MIGRATIONS, "backup")
    assert code == -signal.SIGKILL
    assert not (out / "b.sqlite").exists()  # a partial file is never visible under the final name
    info = create_backup(state_dir, out / "b.sqlite", MIGRATIONS)
    assert info.schema_version == migration_count()
    with closing(sqlite3.connect(state_dir / "synapse.sqlite")) as live:
        assert live.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_sigkill_during_restore_keeps_old_db_and_retry_completes(state):
    state_dir, out = state
    create_backup(state_dir, out / "good.sqlite", MIGRATIONS)
    with closing(open_database(state_dir / "synapse.sqlite")) as db:
        db.execute("INSERT INTO workflow_versions VALUES('wf2','1','{}','hash','now')")  # newer than the backup
    code, _ = run_child(CHILD_BACKUP, state_dir, out / "good.sqlite", MIGRATIONS, "restore")
    assert code == -signal.SIGKILL
    asides = [a for a in state_dir.glob("synapse.sqlite.pre-restore-*") if not a.name.endswith(("-wal", "-shm"))]
    assert len(asides) == 1  # replaced data is kept aside, not lost
    with closing(sqlite3.connect(asides[0])) as old:
        assert old.execute("SELECT count(*) FROM workflow_versions").fetchone() == (2,)
    # The stale lock file from the dead process must not wedge the retry.
    restore_backup(state_dir, out / "good.sqlite", MIGRATIONS)
    with closing(open_database(state_dir / "synapse.sqlite")) as db:
        assert db.execute("SELECT count(*) FROM workflow_versions").fetchone() == (1,)
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
