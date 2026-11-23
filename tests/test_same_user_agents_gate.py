"""Fail-closed same-user agent gate (security review F1/F2)."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from synapse.daemon.same_user_gate import (
    SameUserAgentsRefused, assess_exposure, check_same_user_agents, sandbox_note)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def world(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"executable": str(exe), "allowed_tools": ["Read"], "enabled": True}))
    cfg.chmod(0o600)
    return state, cfg


def _spawn(state, cfg, *extra, env=None):
    log = state.parent / "daemon.log"
    env = {k: v for k, v in os.environ.items() if k != "SYNAPSE_ALLOW_SAME_USER_AGENTS"} | {"PYTHONPATH": str(ROOT / "src")} | (env or {})
    with open(log, "wb") as out:
        proc = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(state), "--migrations",
                                 str(ROOT / "migrations"), "--claude-cli-config", str(cfg), *extra],
                                stdout=out, stderr=subprocess.STDOUT, env=env)
    return proc, log


def test_default_refuses_before_touching_state(world):
    state, cfg = world
    proc, log = _spawn(state, cfg)
    assert proc.wait(timeout=30) == 2
    text = log.read_text()
    assert "refusing to enable agent runs" in text and "--allow-same-user-agents" in text
    assert "review-2026-11-20" in text
    assert not (state / "synapse.sqlite").exists() and not (state / "agentd.sock").exists()


def test_disabled_profile_and_no_config_need_no_acknowledgement(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    assert check_same_user_agents(state, agents_enabled=False, allow=False) is False
    with pytest.raises(SameUserAgentsRefused):
        check_same_user_agents(state, agents_enabled=True, allow=False)
    assert check_same_user_agents(state, agents_enabled=True, allow=True) is True


def test_gate_logs_warning(tmp_path, caplog):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    with caplog.at_level("WARNING", logger="synapse.daemon"):
        check_same_user_agents(state, agents_enabled=True, allow=True)
    assert any(r.levelname == "WARNING" and "NOT a security boundary" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("via_env", [False, True])
def test_acknowledged_start_logs_warning_and_audits(world, via_env):
    state, cfg = world
    proc, log = _spawn(state, cfg, *(() if via_env else ("--allow-same-user-agents",)),
                       env={"SYNAPSE_ALLOW_SAME_USER_AGENTS": "1"} if via_env else None)
    try:
        deadline = time.monotonic() + 30
        while not (state / "agentd.sock").exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert proc.poll() is None, log.read_text()
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    text = log.read_text()
    assert "WARNING" in text and "NOT a security boundary" in text
    with sqlite3.connect(state / "synapse.sqlite") as db:
        rows = db.execute("SELECT principal, decision FROM audit_entries WHERE action='agent_runtime.same_user_acknowledged'").fetchall()
    assert rows == [("daemon:startup", "allowed")]


@pytest.mark.parametrize("owner,agent,sandbox,exposed,reason", [
    (1000, 1000, False, True, "same_uid_no_sandbox"),
    (1000, 1000, True, True, "same_uid_sandboxed"),
    (1000, 1001, False, False, "different_uid"),
    (1000, 1001, True, False, "different_uid"),
    (None, 1000, True, True, "unknown_input"),
    (1000, None, False, True, "unknown_input"),
    ("1000", 1000, True, True, "unknown_input"),
    (True, 1, True, True, "unknown_input"),
    (-1, 1000, True, True, "unknown_input"),
])
def test_assess_exposure_verdicts_fail_closed(owner, agent, sandbox, exposed, reason):
    verdict = assess_exposure(owner, agent, sandbox)
    assert (verdict.exposed, verdict.reason) == (exposed, reason)


@pytest.mark.parametrize("sandbox", [None, "yes", 1, 0])
def test_unknown_sandbox_value_counts_as_unsandboxed(sandbox):
    verdict = assess_exposure(1000, 1000, sandbox)
    assert verdict.exposed and verdict.reason == "same_uid_no_sandbox" and not verdict.sandbox_configured
    assert "no sandbox" in sandbox_note(verdict)


def test_sandbox_does_not_bypass_flag_and_message_states_it(tmp_path, caplog):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    with pytest.raises(SameUserAgentsRefused) as err:
        check_same_user_agents(state, agents_enabled=True, allow=False, sandbox_configured=True)
    assert "--allow-same-user-agents" in str(err.value) and "reduces but does not remove" in str(err.value)
    with pytest.raises(SameUserAgentsRefused) as err:
        check_same_user_agents(state, agents_enabled=True, allow=False, sandbox_configured=False)
    assert "no sandbox is configured" in str(err.value)
    with caplog.at_level("WARNING", logger="synapse.daemon"):
        assert check_same_user_agents(state, agents_enabled=True, allow=True, sandbox_configured=True) is True
    assert any("reduces but does not remove" in r.getMessage() for r in caplog.records)
