from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

from synapse.infrastructure.daemon_database import daemon_database
from synapse.persistence.approval_store import write_approval_request

ROOT = Path(__file__).resolve().parents[1]


def test_cli_queries_real_daemon(tmp_path):
    with daemon_database(tmp_path / "synapse.sqlite", ROOT / "migrations") as db:
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES('task','run','node','created','now','now')")
        db.execute("BEGIN IMMEDIATE")
        write_approval_request(db, approval_id="approval", run_id="run", operation="push", target="repo",
                               candidate_sha256="a" * 64, policy_version="1",
                               expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        db.execute("COMMIT")
    proc = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(tmp_path),
                             "--migrations", str(ROOT / "migrations")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / "agentd.sock").exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError("daemon exited before listening")
            time.sleep(0.02)
        def cli(*args):
            result = subprocess.run([sys.executable, "-m", "synapse.cli", "--state-dir", str(tmp_path), *args],
                                    capture_output=True, text=True, timeout=5, check=True)
            assert (tmp_path / "operator.token").read_text() not in result.stdout + result.stderr
            return json.loads(result.stdout)
        assert cli("run", "run")["run"]["id"] == "run"
        assert cli("tasks", "run")["tasks"][0]["id"] == "task"
        assert cli("approvals", "--limit", "1")["approvals"][0]["id"] == "approval"
        assert cli("approvals", "--after", "approval")["approvals"] == []
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
