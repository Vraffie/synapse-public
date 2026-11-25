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


def test_manifest_commands_send_the_documented_routes(calls):
    sha = "ab" * 32
    base = "/api/v1/runs/run-1/candidate-manifests"
    assert run("manifest-build", "run-1", "--source", "t1=2", "--idempotency-key", "k", "--correlation-id", "c") == 0
    endpoint, kw = calls[-1]
    assert endpoint == base and json.loads(kw["body"]) == {"sources": {"t1": 2}} and kw["idempotency_key"] == "k"
    assert run("manifest-build", "run-1") == 0 and json.loads(calls[-1][1]["body"]) == {}
    assert run("manifest-list", "run-1") == 0 and calls[-1] == (base, {})
    assert run("manifest-show", "run-1", sha) == 0 and calls[-1] == (base + "/" + sha, {})
    assert run("manifest-recheck", "run-1", sha) == 0
    assert calls[-1][0] == base + "/" + sha + "/recheck" and calls[-1][1]["body"] == b"" and calls[-1][1]["idempotency_key"]
    assert run("manifest-approve", "run-1", sha, "--environment", "STAGING") == 0
    endpoint, kw = calls[-1]
    assert endpoint == base + "/" + sha + "/approval"
    assert json.loads(kw["body"]) == {"environment": "STAGING", "policy_version": "1", "ttl_seconds": 3600}
    for bad in (["manifest-show", "run-1", "XYZ"], ["manifest-approve", "run-1", sha, "--environment", "PROD"],
                ["manifest-approve", "run-1", sha, "--environment", "DEV"],
                ["manifest-build", "run-1", "--source", "t1=%d" % 2**31],
                ["manifest-build", "run-1", "--source", "t1"], ["manifest-build", "run-1", "--source", "t1=0"],
                ["manifest-build", "../x"]):
        n = len(calls)
        with pytest.raises(SystemExit):
            run(*bad)
        assert len(calls) == n


def _prune_fake(monkeypatch, runs, refuse=()):
    seen = []
    def fake(state_dir, endpoint, **kw):
        seen.append(endpoint)
        if endpoint.startswith("/api/v1/runs?"):
            state = endpoint.split("state=")[1].split("&")[0]
            return {"runs": [r for r in runs if r["state"] == state], "next_cursor": None}
        if endpoint.split("/")[4] in refuse:
            raise RuntimeError("409 uncommitted changes")
        return {"run_id": "x", "removed": True, "tasks": []}
    monkeypatch.setattr(cli, "_request", fake)
    return seen


def _run(rid, state, updated, workflow="synapse.agent.single"):
    return {"id": rid, "state": state, "updated_at": updated, "workflow_id": workflow}


def test_worktree_prune_removes_only_old_finished_agent_runs_and_never_forces(monkeypatch, capsys):
    runs = [_run("old", "succeeded", "2020-01-01T00:00:00+00:00"),
            _run("dirty", "failed", "2020-01-01T00:00:00+00:00"),
            _run("new", "cancelled", "2999-01-01T00:00:00+00:00"),
            _run("mock", "succeeded", "2020-01-01T00:00:00+00:00", workflow="synapse.mock.x")]
    seen = _prune_fake(monkeypatch, runs, refuse={"dirty"})
    assert run("agent-worktree-prune") == 0
    removals = [e for e in seen if e.endswith("/agent-worktree-remove")]
    assert removals == ["/api/v1/runs/old/agent-worktree-remove", "/api/v1/runs/dirty/agent-worktree-remove"]
    assert not any("force" in e for e in seen)
    out = capsys.readouterr().out
    assert "removed old" in out and "kept dirty" in out and "removed: 1, kept: 1" in out


def test_worktree_prune_dry_run_removes_nothing_and_rejects_bad_age(monkeypatch, capsys):
    seen = _prune_fake(monkeypatch, [_run("old", "succeeded", "2020-01-01T00:00:00+00:00")])
    assert run("agent-worktree-prune", "--dry-run", "--older-than", "12h") == 0
    assert not any(e.endswith("/agent-worktree-remove") for e in seen)
    assert "would remove old" in capsys.readouterr().out
    assert run("agent-worktree-prune", "--older-than", "0d") == 2
    assert run("agent-worktree-prune", "--older-than", "week") == 2


def _restart_fake(monkeypatch, *, state="recovering", tasks=1, prompt=SECRET, step=None):
    import hashlib
    seen = []
    spec = {"repo": "/r", "base_ref": "main", "allowed_tools": ["Read"], "permission_mode": "plan", "timeout_seconds": 90.0,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "approval_step": step}
    old = {"run_state": state, "worktree": {"diff_stat": " a | 1 +"}, "tasks": [{"spec": spec}] * tasks}
    def fake(state_dir, endpoint, **kw):
        seen.append((endpoint, kw))
        return old if endpoint.endswith("/agent") else {"run_id": "new", "state": "running"}
    monkeypatch.setattr(cli, "_request", fake)
    return seen


def test_restart_starts_a_new_run_from_the_old_request_when_the_prompt_matches(monkeypatch, tmp_path, capsys):
    seen = _restart_fake(monkeypatch, step={"operation": "agent.run", "target": "t", "ttl_seconds": 60})
    f = tmp_path / "p.txt"
    f.write_text(SECRET)
    assert run("agent-restart", "old-1", "--prompt-file", str(f)) == 0
    endpoint, kw = seen[1]
    assert endpoint == "/api/v1/agent-runs" and kw["idempotency_key"]
    assert json.loads(kw["body"]) == {"repo": "/r", "base_ref": "main", "prompt": SECRET, "allowed_tools": ["Read"],
                                      "permission_mode": "plan", "timeout_seconds": 90.0,
                                      "approval_step": {"operation": "agent.run", "target": "t", "ttl_seconds": 60}}
    assert SECRET not in capsys.readouterr().err


def test_restart_refuses_a_changed_prompt_a_multi_task_run_and_an_active_run(monkeypatch, tmp_path, capsys):
    f = tmp_path / "p.txt"
    f.write_text("something else")
    seen = _restart_fake(monkeypatch)
    assert run("agent-restart", "old-1", "--prompt-file", str(f)) == 1
    assert len(seen) == 1 and "differs" in capsys.readouterr().err
    assert run("agent-restart", "old-1", "--prompt-file", str(f), "--allow-changed-prompt") == 0
    for kw in ({"state": "running"}, {"tasks": 2}):
        seen = _restart_fake(monkeypatch, **kw)
        assert run("agent-restart", "old-1", "--prompt-file", str(f), "--allow-changed-prompt") == 1
        assert len(seen) == 1


def test_agent_start_names_the_operators_default_profile_unless_another_is_given(monkeypatch, tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / "default-profile").write_text("claude-work\n")
    f = tmp_path / "p.txt"
    f.write_text("do it")
    sent = []
    monkeypatch.setattr(cli, "_request", lambda state_dir, endpoint, **kw: sent.append(json.loads(kw["body"])) or {"run_id": "r1"})
    base = ["--state-dir", str(state), "agent-start", "--repo", "/r", "--tool", "Read", "--prompt-file", str(f),
            "--idempotency-key", "k", "--correlation-id", "c"]
    assert cli.main(base) == 0
    assert cli.main([*base, "--profile", "other"]) == 0
    assert [b["profile"] for b in sent] == ["claude-work", "other"]
    (state / "default-profile").unlink()
    assert cli.main(base) == 0 and "profile" not in sent[2]


@pytest.mark.parametrize("content", ["default\n", "synapse.builtin.agent.claude.v1\n", "bad id\n", "x" * 200 + "\n", ""])
def test_default_profile_file_rejects_reserved_and_malformed_ids(tmp_path, content):
    from synapse.default_profile import read_default_profile
    (tmp_path / "default-profile").write_text(content)
    assert read_default_profile(tmp_path) is None


def test_default_profile_file_must_be_private_and_not_a_link(tmp_path):
    from synapse.default_profile import read_default_profile
    real = tmp_path / "real"
    real.write_text("claude-work\n")
    (tmp_path / "default-profile").symlink_to(real)
    assert read_default_profile(tmp_path) is None
    (tmp_path / "default-profile").unlink()
    real.rename(tmp_path / "default-profile")
    assert read_default_profile(tmp_path) == "claude-work"
    (tmp_path / "default-profile").chmod(0o666)
    assert read_default_profile(tmp_path) is None


def _multi_sent(monkeypatch, tmp_path, extra, prompt="do it"):
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    f = tmp_path / "p.txt"
    f.write_text(prompt)
    sent = []
    monkeypatch.setattr(cli, "_request", lambda state_dir, endpoint, **kw: sent.append(json.loads(kw["body"])) or {"run_id": "r1"})
    base = ["--state-dir", str(state), "agent-start", "--idempotency-key", "k", "--correlation-id", "c"]
    return cli.main([*base, *extra(f)]), sent


def test_repo_ids_build_one_task_that_writes_every_repository(monkeypatch, tmp_path):
    code, sent = _multi_sent(monkeypatch, tmp_path, lambda f: [
        "--repo-id", "product-config=refs/heads/main", "--repo-id", "portfolio", "--repo-id", "tenant",
        "--prompt-file", str(f), "--tool", "Read", "--tool", "Edit", "--permission-mode", "plan"])
    assert code == 0
    body = sent[0]
    assert "repo" not in body and "base_ref" not in body and "prompt" not in body
    assert body["repos"] == [{"repo_id": "product-config", "baseline_ref": "refs/heads/main"}, {"repo_id": "portfolio"}, {"repo_id": "tenant"}]
    task, = body["tasks"]
    assert task["prompt"] == "do it" and task["allowed_tools"] == ["Read", "Edit"]
    assert task["repos"] == [{"repo_id": i, "access": "write"} for i in ("product-config", "portfolio", "tenant")]
    assert body["permission_mode"] == "plan"


def test_repo_ids_with_a_tasks_file_keep_each_tasks_own_repos(monkeypatch, tmp_path):
    tasks = tmp_path / "t.json"
    t = [{"id": "a", "prompt": "p", "allowed_tools": ["Read"], "repos": [{"repo_id": "x", "access": "write"}]}]
    tasks.write_text(json.dumps(t))
    code, sent = _multi_sent(monkeypatch, tmp_path, lambda f: ["--repo-id", "x", "--tasks-file", str(tasks)])
    assert code == 0 and sent[0]["tasks"] == t and sent[0]["repos"] == [{"repo_id": "x"}] and "repo" not in sent[0]


@pytest.mark.parametrize("extra", [
    ["--repo", "/r", "--repo-id", "a"],           # both shapes
    [],                                           # neither
    ["--repo-id", "a", "--repo-id", "a"],         # repeated id
    ["--repo-id", "bad id"],                      # id pattern
    ["--repo-id", "a=main"],                      # baseline must be refs/heads/...
    ["--repo-id", "a"],                           # no --tool with --prompt-file
])
def test_bad_repo_selection_is_refused_before_any_request(monkeypatch, tmp_path, extra):
    def args(f):
        return [*extra, "--prompt-file", str(f)]
    with pytest.raises(SystemExit):
        _multi_sent(monkeypatch, tmp_path, args)
