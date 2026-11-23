"""`agentctl usage` against a real daemon: seeded ledger, unknown run, unknown-is-null totals."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from synapse.infrastructure.daemon_database import daemon_database

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def daemon(tmp_path):
    with daemon_database(tmp_path / "synapse.sqlite", ROOT / "migrations") as db:
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','succeeded',0,'now','now')")
        db.execute("INSERT INTO runs VALUES('empty','wf','1','created',0,'now','now')")
        db.execute("INSERT INTO profiles VALUES('p','claude','P','ref',1,100,1,'unknown',1,'now')")
        for n in (1, 2):
            db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) "
                       f"VALUES('t{n}','run','n{n}','succeeded','now','now')")
            db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) "
                       f"VALUES('a{n}','t{n}',1,'p','succeeded')")
        db.execute("INSERT INTO usage_observations(id,attempt_id,profile_id,source,input_tokens,output_tokens,"
                   "confidence,observed_at,cost_usd) VALUES('u1','a1','p','claude-cli',2,4,'reported','now',0.5)")
    proc = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(tmp_path),
                             "--migrations", str(ROOT / "migrations")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / "agentd.sock").exists() and time.monotonic() < deadline:
            assert proc.poll() is None, "daemon exited before listening"
            time.sleep(0.02)
        yield tmp_path
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)


def cli(state, *args):
    return subprocess.run([sys.executable, "-m", "synapse.cli", "--state-dir", str(state), *args],
                          capture_output=True, text=True, timeout=5)


def test_usage_reports_known_totals_and_per_field_coverage(daemon):
    result = cli(daemon, "usage", "run")
    assert result.returncode == 0
    usage = json.loads(result.stdout)
    assert (usage["attempts_total"], usage["attempts_with_usage"]) == (2, 1)
    assert usage["observations"][0]["cache_read_tokens"] is None  # unknown stays null, never 0
    assert usage["totals"]["input_tokens"] == 2 and usage["totals"]["cache_read_tokens"] is None
    assert usage["coverage"]["cost_usd"] == {"known": 1, "attempts": 2}  # known < attempts: lower bound
    assert (daemon / "operator.token").read_text() not in result.stdout + result.stderr


def test_usage_for_run_without_attempts_is_empty_not_zero(daemon):
    usage = json.loads(cli(daemon, "usage", "empty").stdout)
    assert usage["observations"] == [] and usage["attempts_total"] == 0
    assert all(value is None for value in usage["totals"].values())


def test_usage_for_unknown_run_fails_with_404_and_no_stdout(daemon):
    result = cli(daemon, "usage", "nope")
    assert result.returncode == 1 and result.stdout == ""
    assert "404" in result.stderr
