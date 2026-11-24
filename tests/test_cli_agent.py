"""agentctl agent-start / agent-show / agent-cancel (transport mocked; the demo covers the real socket)."""
import io
import json

import pytest

from synapse.cli import __main__ as cli

SECRET = "PROMPT-SENTINEL-do-not-leak"


@pytest.fixture
def calls(monkeypatch):
    seen = []
    def fake(state_dir, endpoint, **kw):
        seen.append((endpoint, kw))
        return {"run_id": "r1", "state": "running"}
    monkeypatch.setattr(cli, "_request", fake)
    return seen


def run(*args):
    return cli.main(["--state-dir", "/state", *args])


START = ["agent-start", "--repo", "/r", "--tool", "Read", "--tool", "Edit", "--idempotency-key", "k", "--correlation-id", "c"]


def test_start_reads_prompt_from_file_and_sends_json_body(calls, tmp_path, capsys):
    f = tmp_path / "p.txt"
    f.write_text(SECRET)
    assert run(*START, "--prompt-file", str(f), "--approval-operation", "agent.run", "--approval-target", "t", "--base-ref", "main") == 0
    endpoint, kw = calls[0]
    body = json.loads(kw["body"])
    assert endpoint == "/api/v1/agent-runs" and kw["idempotency_key"] == "k" and kw["correlation_id"] == "c"
    assert body == {"repo": "/r", "base_ref": "main", "prompt": SECRET, "allowed_tools": ["Read", "Edit"],
                    "permission_mode": "acceptEdits", "timeout_seconds": 600,
                    "approval_step": {"operation": "agent.run", "target": "t"}}
    assert json.loads(capsys.readouterr().out)["run_id"] == "r1"


def test_start_reads_prompt_from_stdin(calls, monkeypatch):
    class In:
        buffer = io.BytesIO(SECRET.encode())
    monkeypatch.setattr(cli.sys, "stdin", In)
    assert run(*START, "--prompt-file", "-") == 0
    assert json.loads(calls[0][1]["body"])["prompt"] == SECRET


def test_there_is_no_prompt_in_argv_option(calls, capsys):
    # argparse abbreviates --prompt to --prompt-file: the text is then an unreadable path, never echoed
    with pytest.raises(SystemExit):
        run(*START, "--prompt-text", SECRET)
    assert run(*START, "--prompt", SECRET) == 1
    assert SECRET not in capsys.readouterr().err and calls == []


def test_prompt_errors_do_not_echo_it_and_send_nothing(calls, tmp_path, capsys):
    big = tmp_path / "big"
    big.write_text(SECRET + "x" * (32 * 1024))
    binary = tmp_path / "bin"
    binary.write_bytes(b"\xff\xfe" + SECRET.encode())
    link = tmp_path / "link"
    link.symlink_to(big)
    for path in (big, binary, link, tmp_path):
        assert run(*START, "--prompt-file", str(path)) == 1
    assert calls == [] and SECRET not in capsys.readouterr().err


def test_approval_flags_must_pair_and_modes_are_restricted(calls, tmp_path, capsys):
    f = tmp_path / "p"
    f.write_text("x")
    with pytest.raises(SystemExit):
        run(*START, "--prompt-file", str(f), "--approval-operation", "o")
    for mode in ("bypassPermissions", "auto"):
        with pytest.raises(SystemExit):
            run(*START, "--prompt-file", str(f), "--permission-mode", mode)
    assert calls == []


def test_show_and_cancel(calls):
    assert run("agent-show", "run-1") == 0
    assert calls[0][0] == "/api/v1/runs/run-1/agent" and calls[0][1] == {}
    assert run("agent-cancel", "run-1") == 0
    endpoint, kw = calls[1]
    assert endpoint == "/api/v1/runs/run-1/agent-cancel" and kw["body"] == b"" and kw["idempotency_key"] and kw["correlation_id"]
    with pytest.raises(SystemExit):
        run("agent-show", "a/b")


def test_abandon_command_sends_explicit_assertion(calls):
    assert run("agent-abandon", "run-1", "--expected-run-version", "7", "--assert-no-agent-process-remains") == 0
    endpoint, kw = calls[0]
    assert endpoint == "/api/v1/runs/run-1/agent-abandon" and kw["idempotency_key"] and kw["correlation_id"]
    assert json.loads(kw["body"]) == {"expected_run_version": 7, "assertion": "no-agent-process-remains"}
    with pytest.raises(SystemExit):  # the assertion flag and version are mandatory
        run("agent-abandon", "run-1", "--expected-run-version", "7")
    with pytest.raises(SystemExit):
        run("agent-abandon", "run-1", "--assert-no-agent-process-remains")
    assert len(calls) == 1


def test_worktree_remove_command_and_force_flag(calls):
    assert run("agent-worktree-remove", "run-1") == 0
    endpoint, kw = calls[0]
    assert endpoint == "/api/v1/runs/run-1/agent-worktree-remove" and kw["body"] == b"" and kw["idempotency_key"]
    assert run("agent-worktree-remove", "run-1", "--force") == 0
    assert calls[1][0] == "/api/v1/runs/run-1/agent-worktree-remove?force=true"
    with pytest.raises(SystemExit):
        run("agent-worktree-remove", "a/b")


def test_tasks_file_sends_a_multi_task_body_and_excludes_single_task_flags(calls, tmp_path, capsys):
    tasks = [{"id": "a", "prompt": SECRET, "allowed_tools": ["Read"]},
             {"id": "b", "prompt": "y", "allowed_tools": ["Read"], "depends_on": ["a"]}]
    f = tmp_path / "tasks.json"
    f.write_text(json.dumps(tasks))
    assert run("agent-start", "--repo", "/r", "--tasks-file", str(f), "--idempotency-key", "k", "--correlation-id", "c") == 0
    body = json.loads(calls[0][1]["body"])
    assert body["tasks"] == tasks and "prompt" not in body and "allowed_tools" not in body
    with pytest.raises(SystemExit):
        run("agent-start", "--repo", "/r", "--tasks-file", str(f), "--tool", "Read", "--idempotency-key", "k", "--correlation-id", "c")
    with pytest.raises(SystemExit):  # neither shape given
        run("agent-start", "--repo", "/r", "--idempotency-key", "k", "--correlation-id", "c")
    bad = tmp_path / "bad.json"
    bad.write_text("{" + SECRET)
    assert run("agent-start", "--repo", "/r", "--tasks-file", str(bad), "--idempotency-key", "k", "--correlation-id", "c") == 1
    notlist = tmp_path / "obj.json"
    notlist.write_text(json.dumps({"id": SECRET}))
    assert run("agent-start", "--repo", "/r", "--tasks-file", str(notlist), "--idempotency-key", "k", "--correlation-id", "c") == 1
    assert len(calls) == 1 and SECRET not in capsys.readouterr().err


def test_request_accepts_201_and_200_for_start_only():
    import http.client
    class R:
        def __init__(self, status): self.status = status
        def read(self, n): return b"{}"
    class Conn:
        status = 201
        def request(self, *a, **k): pass
        def getresponse(self): return R(Conn.status)
        def close(self): pass
    import tempfile, os, stat, socket
    d = tempfile.mkdtemp(); os.chmod(d, 0o700)
    open(os.path.join(d, "operator.token"), "w").write("a" * 43)
    os.chmod(os.path.join(d, "operator.token"), 0o600)
    s = socket.socket(socket.AF_UNIX); s.bind(os.path.join(d, "agentd.sock")); os.chmod(os.path.join(d, "agentd.sock"), 0o600)
    from pathlib import Path
    orig = cli.UnixConnection
    cli.UnixConnection = lambda path: Conn()
    try:
        for status, endpoint, ok in ((201, "/api/v1/agent-runs", True), (200, "/api/v1/agent-runs", True),
                                     (202, "/api/v1/agent-runs", False), (200, "/api/v1/runs/x/agent-cancel", False),
                                     (202, "/api/v1/runs/x/agent-cancel", True),
                                     (200, "/api/v1/runs/x/agent-abandon", True), (202, "/api/v1/runs/x/agent-abandon", False),

                                     (200, "/api/v1/runs/x/agent-worktree-remove?force=true", True),
                                     (202, "/api/v1/runs/x/agent-worktree-remove", False)):
            Conn.status = status
            if ok:
                assert cli._request(Path(d), endpoint, body=b"{}", idempotency_key="k", correlation_id="c") == {}
            else:
                with pytest.raises(cli.DaemonHTTPError):
                    cli._request(Path(d), endpoint, body=b"{}", idempotency_key="k", correlation_id="c")
    finally:
        cli.UnixConnection = orig
        s.close()


def test_profiles_and_cooldown_commands_use_plain_requests(calls, capsys):
    assert run("profiles", "--json") == 0
    assert calls[-1] == ("/api/v1/profiles", {})
    assert run("profile-cooldown", "reader", "--seconds", "90") == 0
    endpoint, kw = calls[-1]
    assert endpoint == "/api/v1/profiles/reader/cooldown" and kw["plain_method"] == "POST"
    assert json.loads(kw["body"]) == {"seconds": 90.0} and "idempotency_key" not in kw
    assert run("profile-cooldown", "reader", "--clear") == 0
    endpoint, kw = calls[-1]
    assert endpoint == "/api/v1/profiles/reader/cooldown" and kw == {"plain_method": "DELETE"}


@pytest.mark.parametrize("args", [
    ["profile-cooldown", "reader"],                                  # neither --seconds nor --clear
    ["profile-cooldown", "reader", "--seconds", "5", "--clear"],     # both
    ["profile-cooldown", "reader", "--seconds", "0"],
    ["profile-cooldown", "reader", "--seconds", "86401"],
    ["profile-cooldown", "reader", "--seconds", "nan"],
    ["profile-cooldown", "../x", "--clear"],
    ["profile-cooldown", "a/b", "--seconds", "5"],
    ["profile-cooldown", "--clear", "--", "-x"],                     # an option-like ID
])
def test_cooldown_command_refuses_bad_input_before_any_request(calls, args):
    with pytest.raises(SystemExit):
        run(*args)
    assert calls == []


def test_worktree_commit_command(calls):
    assert run("agent-worktree-commit", "run-1", "-m", "fix: it") == 0
    endpoint, kw = calls[0]
    assert endpoint == "/api/v1/runs/run-1/agent-worktree-commit" and kw["idempotency_key"]
    assert json.loads(kw["body"]) == {"message": "fix: it"}
    with pytest.raises(SystemExit):  # the message is mandatory
        run("agent-worktree-commit", "run-1")


def test_start_sends_profile_only_when_given_and_validates_it(calls, tmp_path, capsys):
    f = tmp_path / "p.txt"
    f.write_text("hello")
    assert run(*START, "--prompt-file", str(f)) == 0
    assert "profile" not in json.loads(calls[-1][1]["body"])
    assert run(*START, "--prompt-file", str(f), "--profile", "claude-personal-ios") == 0
    assert json.loads(calls[-1][1]["body"])["profile"] == "claude-personal-ios"
    n = len(calls)
    for bad in ("", "a b", "x" * 65, "../etc", "a/b"):
        with pytest.raises(SystemExit):
            run(*START, "--prompt-file", str(f), "--profile", bad)
    assert len(calls) == n


def test_tasks_file_profile_is_passed_per_task_and_validated(calls, tmp_path, capsys):
    tasks = tmp_path / "t.json"
    tasks.write_text(json.dumps([{"id": "a", "prompt": "p", "allowed_tools": ["Read"], "profile": "claude-work"}]))
    assert run("agent-start", "--repo", "/r", "--tasks-file", str(tasks), "--idempotency-key", "k", "--correlation-id", "c") == 0
    assert json.loads(calls[-1][1]["body"])["tasks"][0]["profile"] == "claude-work"
    n = len(calls)
    tasks.write_text(json.dumps([{"id": "a", "prompt": "p", "allowed_tools": ["Read"], "profile": "no good"}]))
    assert run("agent-start", "--repo", "/r", "--tasks-file", str(tasks), "--idempotency-key", "k", "--correlation-id", "c") == 1
    assert len(calls) == n


def test_start_shows_the_daemons_422_reason_and_exits_nonzero(monkeypatch, tmp_path, capsys):
    def refuse(*_a, **_k):
        raise cli.DaemonHTTPError(422, reason="profile is disabled")
    monkeypatch.setattr(cli, "_request", refuse)
    f = tmp_path / "p.txt"
    f.write_text("hello")
    assert run(*START, "--prompt-file", str(f), "--profile", "claude-work") == 1
    err = capsys.readouterr().err
    assert "422" in err and "profile is disabled" in err and "hello" not in err


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def getheader(self, _name):
        return None

    def read(self, n=None):
        return self._body[:n]


@pytest.mark.parametrize("body,expected", [
    (b'{"detail": "unknown profile"}', "unknown profile"),
    (b'{"detail": "bad\\u001b[31m thing"}', "bad[31m thing"),
    (b'{"detail": [{"msg": "x", "input": "SECRET"}]}', None),
    (b"not json", None),
    (b'{"detail": "' + b"x" * 500 + b'"}', "x" * 200),
])
def test_refusal_reason_is_string_detail_only_sanitized_and_bounded(body, expected):
    assert cli._refusal_reason(_Resp(422, body)) == expected


def test_profiles_prints_id_enabled_cooldown_and_tolerates_missing_fields(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {"profiles": [
        {"id": "claude-work", "enabled": True, "cooldown_remaining_seconds": 0.0},
        {"id": "claude-personal", "enabled": True, "cooldown_remaining_seconds": 90.0},
        {"id": "claude-personal-ios", "enabled": False}, "junk"]})
    assert run("profiles") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["ID", "ENABLED", "COOLDOWN"]
    assert out[1].split() == ["claude-work", "yes", "-"]
    assert out[2].split()[:2] == ["claude-personal", "yes"] and "cooling down 90 s" in out[2]
    assert out[3].split()[:2] == ["claude-personal-ios", "no"] and len(out) == 4


def test_agent_show_prints_the_real_task_profiles_and_a_top_level_one_and_tolerates_absence(monkeypatch, capsys):
    tasks = [{"task_id": "t1", "profile_id": "claude-work", "spec": {"profile": "claude-personal"}},
             {"task_id": "t2", "profile_id": None, "spec": {"profile": "claude-personal-ios"}},
             {"task_id": "t3", "profile_id": "claude-work", "spec": {}}, {"task_id": "t4", "profile_id": "bad id\n"}]
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {"outcome": "ok", "tasks": tasks})
    assert run("agent-show", "r1") == 0
    captured = capsys.readouterr()
    assert captured.err.splitlines() == ["profile: claude-work", "profile: claude-personal-ios"]
    assert json.loads(captured.out)["tasks"][0]["profile_id"] == "claude-work"
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {"outcome": "ok", "profile": "claude-work"})
    assert run("agent-show", "r1") == 0
    assert "profile: claude-work" in capsys.readouterr().err
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {"outcome": "ok"})
    assert run("agent-show", "r1") == 0
    assert "profile" not in capsys.readouterr().err


def test_profile_flag_goes_top_level_next_to_tasks(calls, tmp_path):
    tasks = tmp_path / "t.json"
    tasks.write_text(json.dumps([{"id": "a", "prompt": "p", "allowed_tools": ["Read"]}]))
    assert run("agent-start", "--repo", "/r", "--tasks-file", str(tasks), "--profile", "claude-work",
               "--idempotency-key", "k", "--correlation-id", "c") == 0
    body = json.loads(calls[-1][1]["body"])
    assert body["profile"] == "claude-work" and "profile" not in body["tasks"][0]


def test_request_attaches_the_422_reason_from_a_real_response(tmp_path, monkeypatch):
    class Conn:
        def __init__(self, *_): pass
        def request(self, *a, **k): pass
        def getresponse(self): return _Resp(422, b'{"detail": "profile is disabled"}')
        def close(self): pass
    monkeypatch.setattr(cli, "UnixConnection", Conn)
    monkeypatch.setattr(cli, "_load_token", lambda _d: "T" * 43)
    import socket
    sock = tmp_path / "agentd.sock"
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(sock))
    sock.chmod(0o600)
    try:
        with pytest.raises(cli.DaemonHTTPError) as caught:
            cli._request(tmp_path, "/api/v1/agent-runs", body=b"{}", idempotency_key="k", correlation_id="c")
    finally:
        s.close()
    assert caught.value.status == 422 and caught.value.reason == "profile is disabled"
    assert "profile is disabled" in str(caught.value)
