"""Supervisor forwards daemon flags (review F7/F14) and does not loop on a swapped socket."""
import os
from pathlib import Path
import socket
import subprocess

import pytest

from synapse.daemon import __main__ as daemon_main
from synapse.supervisor import __main__ as sup

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def private(tmp_path):
    tmp_path.chmod(0o700)
    return tmp_path


def _capture(monkeypatch):
    commands = []

    class FakeChild:
        pid, returncode = 1, 0

        def __init__(self, command, **kw):
            commands.append(command)

        def poll(self):
            return 0

    monkeypatch.setattr(sup.subprocess, "Popen", FakeChild)
    return commands


def test_default_command_has_identity_and_no_optional_flags(private, monkeypatch):
    commands = _capture(monkeypatch)
    assert sup.run(private, private / "m") == 0
    assert "--socket-identity" in commands[0]
    assert "--enable-mock-mcp-ipc" not in commands[0]
    assert "--claude-cli-config" not in commands[0]


def test_run_forwards_agent_runtime_flags_with_identity(private, monkeypatch):
    commands = _capture(monkeypatch)
    cfg = private / "agent.json"
    assert sup.run(private, private / "m", enable_mock_mcp_ipc=True, claude_cli_config=cfg) == 0
    cmd = commands[0]
    assert "--enable-mock-mcp-ipc" in cmd
    assert cmd[cmd.index("--claude-cli-config") + 1] == str(cfg)
    assert "--socket-identity" in cmd and "--socket-fd" in cmd


def test_cli_forwards_flags_and_resolves_config_path(private, monkeypatch):
    seen = {}
    monkeypatch.setattr(sup, "run", lambda *a, **kw: seen.update(kw) or 0)
    monkeypatch.chdir(private)
    assert sup.main(["--state-dir", str(private), "--enable-mock-mcp-ipc", "--claude-cli-config", "rel.json"]) == 0
    assert seen["enable_mock_mcp_ipc"] is True
    assert seen["claude_cli_config"] == (private / "rel.json").resolve()


def test_cli_defaults_do_not_enable_agent_runtime(private, monkeypatch):
    seen = {}
    monkeypatch.setattr(sup, "run", lambda *a, **kw: seen.update(kw) or 0)
    assert sup.main(["--state-dir", str(private)]) == 0
    assert seen["enable_mock_mcp_ipc"] is False and seen["claude_cli_config"] is None


def test_supervisor_with_real_swapped_socket_stops_after_one_launch(private, monkeypatch):
    """Real daemon child; the socket path is swapped after bind, before the child starts."""
    real_popen = subprocess.Popen
    launches, children, impostors = [], [], []
    path = private / "agentd.sock"

    def swapping_popen(command, **kw):
        launches.append(command)
        path.unlink()
        impostor = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        impostor.bind(str(path))
        path.chmod(0o600)
        impostor.listen(4)
        impostors.append(impostor)
        kw["env"] = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        child = real_popen(command, stderr=subprocess.PIPE, **kw)
        children.append(child)
        return child

    monkeypatch.setattr(sup.subprocess, "Popen", swapping_popen)
    try:
        code = sup.run(private, private / "migrations", max_restarts=5, base_delay=0.01)
        assert code == 1
        assert len(launches) == 1  # no restart loop
        assert children[0].returncode == daemon_main.EXIT_SOCKET_IDENTITY
        assert b"replaced" in children[0].stderr.read()
    finally:
        for child in children:
            if child.stderr:
                child.stderr.close()
        for impostor in impostors:
            impostor.close()
        if path.exists() or path.is_socket():
            path.unlink()
