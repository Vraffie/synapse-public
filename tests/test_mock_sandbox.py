from contextlib import closing
import json
import os
from pathlib import Path
import socket
import subprocess
import time

import pytest

from synapse.infrastructure import mock_sandbox


@pytest.fixture
def managed(tmp_path):
    root = tmp_path / "managed"
    root.mkdir(mode=0o700)
    work = root / "work"
    work.mkdir(mode=0o700)
    return root, work


def test_fixed_mock_runs_or_fails_closed(managed):
    root, work = managed
    try:
        output = mock_sandbox.run_fixed_mock(root, work)
    except mock_sandbox.SandboxUnavailable:
        pytest.skip("bubblewrap or unprivileged namespaces unavailable")
    assert output == b"synapse mock candidate v1\n"


def test_no_unsandboxed_fallback_when_executable_missing(managed, monkeypatch):
    root, work = managed
    monkeypatch.setattr(mock_sandbox, "_BWRAP", "/no-such-bubblewrap")
    with pytest.raises(mock_sandbox.SandboxUnavailable):
        mock_sandbox.run_fixed_mock(root, work)


def test_rejects_outside_or_symlink_worktree(managed, tmp_path):
    root, work = managed
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    link.symlink_to(outside, target_is_directory=True)
    for invalid in (root, outside, link):
        with pytest.raises(ValueError):
            mock_sandbox.run_fixed_mock(root, invalid)


def test_mock_namespace_hides_state_other_worktrees_host_proc_network_and_extra_fds(managed, tmp_path, monkeypatch):
    root, work = managed
    try:
        mock_sandbox.run_fixed_mock(root, work)
    except mock_sandbox.SandboxUnavailable:
        pytest.skip("bubblewrap or unprivileged namespaces unavailable")
    state = tmp_path / "state"
    state.mkdir()
    (state / "operator.token").write_text("test-operator-marker")
    (state / "synapse.sqlite").write_text("test-database-marker")
    other = root / "other"
    other.mkdir()
    (other / "private").write_text("other-worktree-marker")
    fd = os.open(state / "operator.token", os.O_RDONLY)
    os.set_inheritable(fd, True)
    monkeypatch.setenv("SYNAPSE_TEST_SECRET", "environment-marker")
    try:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            program = f'''
import json, os, socket
paths = {[str(state / 'operator.token'), str(state / 'synapse.sqlite'), str(other / 'private'), '/proc/1/root' + str(state / 'operator.token')]!r}
def can_read_fd():
    try:
        return os.read({fd}, 100) == b'test-operator-marker'
    except OSError:
        return False
s = socket.socket()
s.settimeout(.2)
try:
    s.connect(('127.0.0.1', {port}))
    connected = True
except OSError:
    connected = False
with open('/work/created', 'w') as f: f.write('ok')
print(json.dumps({{'paths': [os.path.exists(p) for p in paths], 'network': connected,
                  'fd': can_read_fd(), 'environment': os.getenv('SYNAPSE_TEST_SECRET')}}))
'''
            process = mock_sandbox._start(root, work, program)
            output, error = process.communicate(timeout=10)
            assert process.returncode == 0, error.decode(errors="replace")
            result = json.loads(output)
    finally:
        os.close(fd)
    assert result == {"paths": [False] * 4, "network": False, "fd": False, "environment": None}
    assert (work / "created").read_text() == "ok"


def test_timeout_terminates_owned_process_group(managed, monkeypatch):
    root, work = managed
    try:
        mock_sandbox.run_fixed_mock(root, work)
    except mock_sandbox.SandboxUnavailable:
        pytest.skip("bubblewrap or unprivileged namespaces unavailable")
    started = []
    real_start = mock_sandbox._start
    def capture(*args):
        process = real_start(*args)
        started.append(process)
        return process
    monkeypatch.setattr(mock_sandbox, "_start", capture)
    monkeypatch.setattr(mock_sandbox, "_PROGRAM", "import time; time.sleep(30)")
    with pytest.raises(subprocess.TimeoutExpired):
        mock_sandbox.run_fixed_mock(root, work, timeout=0.3)
    assert started[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(started[0].pid, 0)


def test_timeout_kills_descendant_that_creates_new_session(managed, monkeypatch):
    root, work = managed
    try:
        mock_sandbox.run_fixed_mock(root, work)
    except mock_sandbox.SandboxUnavailable:
        pytest.skip("bubblewrap or unprivileged namespaces unavailable")
    # The descendant tries to leave the worker's process group. A fresh PID
    # namespace plus bubblewrap's lifecycle must still prevent it surviving.
    program = '''
import os, time
if os.fork() == 0:
    os.setsid()
    open('/work/ready', 'w').close()
    time.sleep(2)
    open('/work/survived', 'w').close()
else:
    time.sleep(30)
'''
    monkeypatch.setattr(mock_sandbox, "_PROGRAM", program)
    with pytest.raises(subprocess.TimeoutExpired):
        mock_sandbox.run_fixed_mock(root, work, timeout=0.8)
    assert (work / "ready").exists()
    time.sleep(1.5)
    assert not (work / "survived").exists()
