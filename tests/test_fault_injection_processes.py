"""Process-level fault injection: a SIGKILLed lock owner and a SIGKILLed real daemon (ARCHITECTURE.md §13)."""
import importlib.util
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys

import pytest

from invariants import assert_invariants, check_invariants
from synapse.infrastructure.daemon_database import daemon_database
from synapse.infrastructure.daemon_lock import DaemonAlreadyRunning

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"

OWNER = """
import os, sys, time
from pathlib import Path
from synapse.infrastructure.daemon_database import daemon_database
from synapse.persistence.workflow_store import create_run, publish_workflow
with daemon_database(Path(sys.argv[1]) / "db.sqlite", Path(sys.argv[2])) as db:
    publish_workflow(db, workflow_id="wf", version="1", nodes=[{"id": "build", "dependencies": []}], principal="operator", idempotency_key="p")
    create_run(db, workflow_id="wf", version="1", principal="operator", idempotency_key="c")
    db.execute("BEGIN IMMEDIATE")  # a half-finished write that must never become visible
    db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,type,schema_version,payload_json,occurred_at) VALUES('half','x','x','x',1,'{}','now')")
    print("owning", flush=True)
    time.sleep(60)
"""


def test_a_killed_lock_owner_releases_ownership_and_loses_only_its_uncommitted_write(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    owner = subprocess.Popen([sys.executable, "-c", OWNER, str(state), str(MIGRATIONS)], stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert owner.stdout.readline().strip() == "owning"
        with pytest.raises(DaemonAlreadyRunning):  # at most one daemon owns the database
            with daemon_database(state / "db.sqlite", MIGRATIONS):
                pytest.fail("a second daemon acquired the database")
        owner.send_signal(signal.SIGKILL)
        owner.wait(10)
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait()
    with daemon_database(state / "db.sqlite", MIGRATIONS) as db:  # the kernel released the lock with the process
        assert db.execute("SELECT COUNT(*) FROM events WHERE id='half'").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)
        assert_invariants(db, quiescent=True)


def _load_demo():
    spec = importlib.util.spec_from_file_location("demo_mock_for_fault_injection", ROOT / "scripts" / "demo_mock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_daemon_sigkilled_while_parked_leaves_a_database_that_satisfies_every_invariant(tmp_path):
    """Plain run, gated run (daemon SIGKILLed while parked, restarted, human approves), expiring run."""
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    demo = _load_demo().Demo(state, quiet=True)
    try:
        report = demo.run_all(restore_check=False)
    finally:
        demo.close()
    assert report["daemon_starts"] == 2 and all(report["checks"].values())
    with sqlite3.connect(f"file:{state / 'synapse.sqlite'}?mode=ro", uri=True) as db:
        violations = check_invariants(db, quiescent=True, blob_dir=state / "artifacts")
        assert violations == [], "\n".join(map(str, violations))
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3
