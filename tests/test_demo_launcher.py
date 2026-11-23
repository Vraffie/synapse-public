"""scripts/demo.py end to end against a REAL daemon subprocess on the mock provider (headless, and GUI offscreen)."""
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "scripts" / "demo.py"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
       "QT_QPA_PLATFORM": "offscreen", "SYNAPSE_REDUCED_MOTION": "1"}
ENV.pop("SYNAPSE_APPROVER_TOKEN", None)


def start(*args):
    return subprocess.Popen([sys.executable, str(DEMO), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=ENV, cwd=ROOT)


def state_dir(output):
    match = re.search(r"state dir (/\S+synapse-demo-\w+)\)", output)
    assert match, output
    return Path(match.group(1))


def daemon_pids(base):
    done = subprocess.run(["pgrep", "-f", str(base)], capture_output=True, text=True)
    return [p for p in done.stdout.split() if int(p) != os.getpid()]


def wait_gone(base):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and (base.exists() or daemon_pids(base)):
        time.sleep(0.1)
    assert not base.exists() and daemon_pids(base) == []


def test_headless_flow_end_to_end_and_cleanup():
    proc = start("--headless", "--pace", "0.3", "--keep-state")
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    base = state_dir(out)
    try:
        assert "deploy-verify=awaiting_approval" in out
        with sqlite3.connect(f"file:{base / 'state' / 'synapse.sqlite'}?mode=ro", uri=True) as db:
            changes = db.execute("SELECT COUNT(*) FROM events WHERE type='task.state_changed' "
                                 "AND payload_json LIKE '%changes_required%'").fetchone()[0]
        assert changes >= 1  # the review step was sent back once, then passed on the retry
        assert "run=succeeded" in out and "Final state: run succeeded" in out
        assert "approval-decide" in out
        state = base / "state"
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        for name in ("approver.token", "operator.token"):  # token values never reach the output
            assert (state / name).read_text().strip() not in out
        assert daemon_pids(base) == []  # --keep-state keeps the files, but the daemon is stopped
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_no_approve_wait_stops_at_the_gate_and_removes_state():
    proc = start("--headless", "--pace", "0.2", "--no-approve-wait")
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out
    assert "deploy-verify=awaiting_approval" in out and "Final state" not in out and "run=succeeded" not in out
    wait_gone(state_dir(out))


def test_ctrl_c_cleans_up_daemon_and_state():
    proc = start("--headless", "--pace", "2")
    seen = ""
    while "started: run" not in seen:
        line = proc.stdout.readline()
        assert line, seen
        seen += line
    base = state_dir(seen)
    proc.send_signal(signal.SIGINT)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode != 0 and "cleaning up" in out
    wait_gone(base)


def test_gui_offscreen_connects_reaches_gate_and_completes_after_cli_approval():
    pytest.importorskip("PySide6")
    proc = start("--pace", "0.3", "--linger", "0.5", "--no-clipboard")
    seen, command = "", None
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        seen += line
        if "approval-decide" in line and "SYNAPSE_APPROVER_TOKEN" in line:
            command = line.strip()
            break
    assert command, seen
    base = state_dir(seen)
    approve = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30, cwd=ROOT, env=ENV)
    assert approve.returncode == 0, approve.stderr
    out, _ = proc.communicate(timeout=45)
    assert proc.returncode == 0, seen + out
    assert "Control Center opened on the live daemon" in seen and "Final state: run succeeded" in out
    wait_gone(base)
