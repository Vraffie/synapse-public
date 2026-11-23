import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from synapse.infrastructure.daemon_database import daemon_database
from synapse.persistence.mock_recovery import recover_mock_run
from synapse.persistence.state_store import StateConflict
from synapse.persistence.task_claim import claim_task

ROOT = Path(__file__).resolve().parents[1]


def _seed(db):
    db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
    db.execute("INSERT INTO runs VALUES('run','wf','1','running',0,'now','now')")
    db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) VALUES('mock','mock','Mock','mock','now')")
    for task, state in (("assigned", "assigned"), ("running", "running"), ("verifying", "verifying")):
        db.execute("INSERT INTO tasks(id,run_id,node_id,state,assigned_profile_id,attempt_count,created_at,updated_at) VALUES(?,'run',?,?, 'mock', 1,'now','now')", (task, task, state))
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('active','running',1,'mock','running')")
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES('done','verifying',1,'mock','completed')")


def test_crash_restart_blocks_work_once_and_does_not_retry(tmp_path):
    state = tmp_path / "private"
    state.mkdir(mode=0o700)
    script = '''
import os, sys
from pathlib import Path
from synapse.infrastructure.daemon_database import daemon_database
from tests.test_mock_recovery import _seed
with daemon_database(Path(sys.argv[1]) / "db.sqlite", Path(sys.argv[2])) as db:
    _seed(db)
    os._exit(0)
'''
    # The test module is not necessarily importable as a package.
    script = script.replace('from tests.test_mock_recovery import _seed', 'from test_mock_recovery import _seed')
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "tests") + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.run([sys.executable, "-c", script, str(state), str(ROOT / "migrations")], check=True, env=env, timeout=10)
    with daemon_database(state / "db.sqlite", ROOT / "migrations") as db:
        unknown, blocked = recover_mock_run(db, run_id="run", principal="daemon", no_workers_survive=True)
        assert unknown == ("active",)
        assert blocked == ("assigned", "running", "verifying")
        assert db.execute("SELECT id,state FROM tasks ORDER BY id").fetchall() == [("assigned", "blocked"), ("running", "blocked"), ("verifying", "blocked")]
        assert db.execute("SELECT id,state FROM task_attempts ORDER BY id").fetchall() == [("active", "unknown"), ("done", "completed")]
        assert db.execute("SELECT COUNT(*) FROM events").fetchone() == db.execute("SELECT COUNT(*) FROM outbox").fetchone() == (4,)
        assert recover_mock_run(db, run_id="run", principal="daemon", no_workers_survive=True) == ((), ())
        assert db.execute("SELECT COUNT(*) FROM task_attempts").fetchone() == (2,)
        db.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(StateConflict):
                claim_task(db, task_id="running", expected_version=1, profile_id="mock", principal="daemon")
        finally:
            db.execute("ROLLBACK")


def test_recovery_requires_assurance_and_rolls_back_for_nonmock(tmp_path):
    with daemon_database(tmp_path / "db.sqlite", ROOT / "migrations") as db:
        _seed(db)
        with pytest.raises(ValueError, match="assurance"):
            recover_mock_run(db, run_id="run", principal="daemon", no_workers_survive=False)
        db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,created_at) VALUES('live','codex','Live','config','now')")
        db.execute("UPDATE tasks SET assigned_profile_id='live' WHERE id='verifying'")
        with pytest.raises(StateConflict, match="mock-owned"):
            recover_mock_run(db, run_id="run", principal="daemon", no_workers_survive=True)
        assert db.execute("SELECT state FROM task_attempts WHERE id='active'").fetchone() == ("running",)
        assert db.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)


def test_outbox_failure_rolls_back_whole_recovery(tmp_path):
    with daemon_database(tmp_path / "db.sqlite", ROOT / "migrations") as db:
        _seed(db)
        db.execute("CREATE TRIGGER fail_recovery BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'outbox failed'); END")
        with pytest.raises(sqlite3.IntegrityError, match="outbox failed"):
            recover_mock_run(db, run_id="run", principal="daemon", no_workers_survive=True)
        assert db.execute("SELECT state FROM task_attempts WHERE id='active'").fetchone() == ("running",)
        assert db.execute("SELECT state FROM tasks WHERE id='running'").fetchone() == ("running",)
        assert db.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)


def test_terminal_run_and_inconsistent_task_fail_closed(tmp_path):
    with daemon_database(tmp_path / "db.sqlite", ROOT / "migrations") as db:
        _seed(db)
        db.execute("UPDATE runs SET state='failed' WHERE id='run'")
        with pytest.raises(StateConflict, match="Terminal run"):
            recover_mock_run(db, run_id="run", principal="daemon", no_workers_survive=True)
        db.execute("UPDATE runs SET state='running' WHERE id='run'")
        db.execute("UPDATE tasks SET state='completed' WHERE id='running'")
        with pytest.raises(StateConflict, match="inconsistent"):
            recover_mock_run(db, run_id="run", principal="daemon", no_workers_survive=True)
        assert db.execute("SELECT state FROM task_attempts WHERE id='active'").fetchone() == ("running",)
        assert db.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
