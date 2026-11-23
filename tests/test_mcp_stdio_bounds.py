"""Limits are applied in the stdio proxy before MCP SDK JSON parsing."""
from contextlib import closing
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

pytest.importorskip('mcp')
from synapse.mcp_gateway.bounded_stdio import MAX_JSON_DEPTH, MAX_STDIO_LINE, _check_depth


def test_depth_scanner_ignores_brackets_in_escaped_strings():
    _check_depth(b'{"text":"\\\"[[[[{{{{"}')
    _check_depth(b'[' * MAX_JSON_DEPTH + b'0' + b']' * MAX_JSON_DEPTH)
    with pytest.raises(ValueError, match='nesting'):
        _check_depth(b'[' * (MAX_JSON_DEPTH + 1) + b'0' + b']' * (MAX_JSON_DEPTH + 1))


@pytest.mark.parametrize('payload', [
    b'x' * (MAX_STDIO_LINE + 1),
    b'[' * (MAX_JSON_DEPTH + 1) + b'0' + b']' * (MAX_JSON_DEPTH + 1) + b'\n',
    b'{"incomplete":',
    b'\xff\n',
])
def test_proxy_terminates_on_unbounded_or_malformed_frames(tmp_path, payload):
    path = tmp_path / 'agent.sock'
    secret = 'x' * 43
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(1)
        env = dict(os.environ)
        env['SYNAPSE_MCP_SOCKET'] = str(path)
        env['SYNAPSE_MCP_CREDENTIAL'] = secret
        proc = subprocess.Popen([sys.executable, '-m', 'synapse.mcp_gateway'], env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate(payload, timeout=8)
        assert proc.returncode != 0
        assert secret.encode() not in stdout + stderr
        assert len(stdout) < 4096 and len(stderr) < 16384
