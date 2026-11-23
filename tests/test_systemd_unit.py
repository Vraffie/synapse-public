"""Static checks for the systemd user unit; it must match the real supervisor CLI."""
from __future__ import annotations

import configparser
import importlib.util
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "packaging" / "systemd" / "synapse-daemon.service"


def _unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",), comment_prefixes=("#", ";"))
    parser.optionxform = str
    parser.read(UNIT, encoding="utf-8")
    return parser


def test_hardening_directives():
    service = _unit()["Service"]
    expected = {
        "NoNewPrivileges": "yes", "PrivateTmp": "yes", "ProtectSystem": "strict",
        "RestrictAddressFamilies": "AF_UNIX", "UMask": "0077", "StateDirectoryMode": "0700",
        "ProtectKernelTunables": "yes", "LockPersonality": "yes", "RestrictSUIDSGID": "yes",
        "CapabilityBoundingSet": "", "IPAddressDeny": "any",
    }
    for key, value in expected.items():
        assert service.get(key) == value, key
    assert "AF_INET" not in service["RestrictAddressFamilies"]
    assert service["ReadWritePaths"] == "%S/synapse"
    assert service["StateDirectory"] == "synapse"
    assert _unit()["Install"]["WantedBy"] == "default.target"


def test_exec_start_matches_supervisor_cli():
    argv = shlex.split(_unit()["Service"]["ExecStart"])
    assert argv[1:3] == ["-m", "synapse.supervisor"]
    assert importlib.util.find_spec("synapse.supervisor.__main__") is not None
    flags = argv[3:]
    assert flags[flags.index("--state-dir") + 1] == "%S/synapse"
    assert "--migrations" not in flags  # packaged migrations are the default
    # Run the real argparse, intercepting run() so nothing starts.
    import synapse.supervisor.__main__ as supervisor
    seen = {}
    original = supervisor.run
    supervisor.run = lambda state_dir, migrations, **kw: seen.update(state_dir=state_dir, migrations=migrations, **kw) or 0
    try:
        assert supervisor.main(flags) == 0
    finally:
        supervisor.run = original
    assert str(seen["state_dir"]) == "%S/synapse"
    assert seen["max_restarts"] == 5


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not installed")
def test_systemd_analyze_verify(tmp_path):
    # Point ExecStart at a real interpreter so verify does not fail on the uninstalled venv.
    text = UNIT.read_text(encoding="utf-8").replace("%h/.local/share/synapse/venv/bin/python", sys.executable)
    copy = tmp_path / "synapse-daemon.service"
    copy.write_text(text, encoding="utf-8")
    result = subprocess.run(["systemd-analyze", "--user", "verify", str(copy)], capture_output=True, text=True, timeout=30)
    assert "Unknown key" not in result.stderr and "Unknown section" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
