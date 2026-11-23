from dataclasses import replace
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from synapse.infrastructure import mock_process_identity as identity


def _child(code="import time; time.sleep(30)"):
    return subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, close_fds=True)


def _cleanup(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def test_owned_child_identity_and_bounded_group_escalation(tmp_path):
    ready = tmp_path / "ready"
    survived = tmp_path / "survived"
    code = f'''import os, signal, time
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open({str(ready)!r}, 'w').close()
    time.sleep(1)
    open({str(survived)!r}, 'w').close()
else:
    time.sleep(30)
'''
    process = _child(code)
    try:
        record = identity.capture_mock_child(process)
        assert identity.inspect_mock_child(record) == identity.ProcessStatus.OWNED
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        assert identity.stop_mock_child_group(process, record, grace_seconds=0.1) == identity.ProcessStatus.SIGNALLED_GROUP
        assert process.returncode is not None
        time.sleep(1)
        assert not survived.exists()
        assert identity.inspect_mock_child(record) == identity.ProcessStatus.UNKNOWN
    finally:
        _cleanup(process)


def test_reused_pid_restart_boot_and_wrong_handle_never_signal(monkeypatch):
    process = _child()
    other = _child()
    try:
        record = identity.capture_mock_child(process)
        calls = []
        monkeypatch.setattr(identity.os, "killpg", lambda *args: calls.append(args))
        for bad in (replace(record, start_ticks=record.start_ticks + 1),
                    replace(record, supervisor_pid=record.supervisor_pid + 1),
                    replace(record, supervisor_start_ticks=record.supervisor_start_ticks + 1),
                    replace(record, boot_id="different")):
            assert identity.inspect_mock_child(bad) == identity.ProcessStatus.UNKNOWN
            assert identity.stop_mock_child_group(process, bad, grace_seconds=0) == identity.ProcessStatus.UNKNOWN
        assert identity.stop_mock_child_group(other, record, grace_seconds=0) == identity.ProcessStatus.UNKNOWN
        assert calls == []
    finally:
        monkeypatch.undo()
        _cleanup(process)
        _cleanup(other)


def test_proc_permission_failure_and_signal_permission_failure_are_unknown(monkeypatch):
    process = _child()
    try:
        record = identity.capture_mock_child(process)
        original_stat = identity._stat
        def denied(pid):
            if pid == process.pid:
                raise PermissionError("denied")
            return original_stat(pid)
        calls = []
        with monkeypatch.context() as patch:
            patch.setattr(identity, "_stat", denied)
            patch.setattr(identity.os, "killpg", lambda *args: calls.append(args))
            assert identity.inspect_mock_child(record) == identity.ProcessStatus.UNKNOWN
            assert identity.stop_mock_child_group(process, record, grace_seconds=0) == identity.ProcessStatus.UNKNOWN
        assert calls == []
        with monkeypatch.context() as patch:
            def no_signal(*_args):
                raise PermissionError("denied")
            patch.setattr(identity.os, "killpg", no_signal)
            assert identity.stop_mock_child_group(process, record, grace_seconds=0) == identity.ProcessStatus.UNKNOWN
        assert process.poll() is None
    finally:
        _cleanup(process)


def test_restart_supervisor_identity_is_ambiguous():
    process = _child()
    try:
        record = identity.capture_mock_child(process)
        code = '''from synapse.infrastructure.mock_process_identity import ProcessIdentity, inspect_mock_child
import sys
print(inspect_mock_child(ProcessIdentity(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], int(sys.argv[4]), int(sys.argv[5]))))'''
        result = subprocess.run([sys.executable, "-c", code, str(record.pid), str(record.start_ticks), record.boot_id,
                                 str(record.supervisor_pid), str(record.supervisor_start_ticks)],
                                check=True, capture_output=True, text=True)
        assert result.stdout.strip() == "unknown"
        assert process.poll() is None
    finally:
        _cleanup(process)


def test_capture_requires_session_leader_and_default_sigchld():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(ValueError, match="own process session"):
            identity.capture_mock_child(process)
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_exited_unreaped_child_is_unknown_and_never_signalled(monkeypatch):
    process = _child("import time; time.sleep(.1)")
    record = identity.capture_mock_child(process)
    calls = []
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and identity._stat(process.pid)[0] != "Z":
            time.sleep(0.01)
        assert identity._stat(process.pid)[0] == "Z"
        with monkeypatch.context() as patch:
            patch.setattr(identity.os, "killpg", lambda *args: calls.append(args))
            assert identity.inspect_mock_child(record) == identity.ProcessStatus.UNKNOWN
            assert identity.stop_mock_child_group(process, record, grace_seconds=0) == identity.ProcessStatus.UNKNOWN
        assert calls == []
    finally:
        process.wait(timeout=5)


def test_nondefault_sigchld_refuses_to_signal(monkeypatch):
    process = _child()
    try:
        record = identity.capture_mock_child(process)
        calls = []
        with monkeypatch.context() as patch:
            patch.setattr(identity.signal, "getsignal", lambda _: signal.SIG_IGN)
            patch.setattr(identity.os, "killpg", lambda *args: calls.append(args))
            assert identity.stop_mock_child_group(process, record, grace_seconds=0) == identity.ProcessStatus.UNKNOWN
        assert calls == []
    finally:
        _cleanup(process)
