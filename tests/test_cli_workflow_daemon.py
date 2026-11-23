import json
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def test_cli_publishes_and_queues_idempotently_over_daemon_socket(tmp_path):
    workflow = tmp_path / "workflow.json"
    workflow.write_text(json.dumps({"workflow_id": "wf", "version": "1", "nodes": [{"id": "task", "dependencies": []}]}))
    proc = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(tmp_path),
                             "--migrations", str(ROOT / "migrations")], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / "agentd.sock").exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError("daemon exited before listening")
            time.sleep(0.02)
        def cli(*args):
            return subprocess.run([sys.executable, "-m", "synapse.cli", "--state-dir", str(tmp_path), *args],
                                  capture_output=True, text=True, timeout=5)
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError("daemon exited before becoming ready")
            if cli("health").returncode == 0:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("daemon did not become ready")
        publish_args = ("publish-workflow", str(workflow), "--idempotency-key", "publish", "--correlation-id", "trace")
        first = cli(*publish_args)
        second = cli(*publish_args)
        assert first.returncode == second.returncode == 0
        assert json.loads(first.stdout) == json.loads(second.stdout)
        queue_args = ("queue-run", "wf", "1", "--idempotency-key", "run", "--correlation-id", "trace")
        first, second = cli(*queue_args), cli(*queue_args)
        assert first.returncode == second.returncode == 0
        assert json.loads(first.stdout) == json.loads(second.stdout)
        assert json.loads(first.stdout)["state"] == "queued"
        assert (tmp_path / "operator.token").read_text() not in first.stdout + first.stderr
        mismatch = cli("queue-run", "wf", "2", "--idempotency-key", "run", "--correlation-id", "trace")
        assert mismatch.returncode == 1 and "HTTP 409" in mismatch.stderr
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=8)
