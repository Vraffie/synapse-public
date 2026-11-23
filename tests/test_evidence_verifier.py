"""Evidence verifier (ADR 025 part 2): real git worktrees in tmp_path, a scripted fake provider, host-side checks."""
import os
import subprocess
import sys
import time

import pytest

from review_harness import EDIT, environment, say
from synapse.infrastructure.worktrees import WorktreeManager
from synapse.orchestration.agent_engine import VERIFIER_ID, process_outcome_verifier
from synapse.orchestration.evidence_verifier import (
    EVIDENCE_EVENT, EVIDENCE_VERIFIER_ID, CheckCommand, EvidencePolicy, EvidenceVerifier, GitWorktreeEvidence,
    run_check)

ENV = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
       "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
PY = os.path.realpath(sys.executable)
NODES = [{"id": "edit", "dependencies": []}]


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    (r / "src" / "a.py").write_text("x = 1\n")
    (r / "README").write_text("hi\n")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "init")
    return str(r.resolve())


def write(*files, link=None, commit=False):
    """A fake edit agent: writes files (relative to its worktree) then succeeds."""
    def script(data):
        wt = data["worktree"]
        for name, body in files:
            path = os.path.join(wt, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(body)
        if link:
            os.symlink(link[1], os.path.join(wt, link[0]))
        if commit:
            git(wt, "add", "-A")
            git(wt, "commit", "-q", "-m", "agent commit")
        return ["edited"], "succeeded"
    return script


def run_edit(tmp_path, repo, script, policy, *, configured=True, inspect=None):
    with environment(tmp_path, NODES) as env:
        task = env.tasks["edit"]
        mgr = WorktreeManager([repo], str((tmp_path / "wt").resolve()))
        wt = mgr.create(env.run_id, task, repo, "main")
        head = git(wt, "rev-parse", "HEAD").strip()
        specs = {task: env.spec(tools=EDIT, mode="acceptEdits", worktree=wt, head=head)}
        verifier = EvidenceVerifier(env.db, env.blobs, specs, {task: policy} if configured else {},
                                    GitWorktreeEvidence(mgr), fallback=process_outcome_verifier, fallback_id=VERIFIER_ID)
        def worktree_check(run_id, task_id, path):
            st = mgr.status(run_id, task_id)
            return st.head, st.clean
        engine = env.engine(specs, {task: script}, verify=verifier, worktree_check=worktree_check)
        env.run(engine)
        state = env.state("edit")
        verifier_id = env.db.execute("SELECT verifier_id FROM verification_results WHERE task_id=?", (task,)).fetchone()
        if inspect is not None:
            inspect(env, verifier)
        return state, env.events(EVIDENCE_EVENT), verifier_id, env, verifier, wt


# ------------------------------------------------------------------------------------------- end to end
def test_changes_inside_allowed_paths_with_passing_check_complete(tmp_path, repo):
    check = CheckCommand((PY, "-c", "import pathlib,sys; sys.exit(0 if 'y = 2' in pathlib.Path('src/a.py').read_text() else 1)"))
    state, events, vid, *_ = run_edit(tmp_path, repo, write(("src/a.py", "y = 2\n"), ("src/new/b.py", "z\n")),
                                      EvidencePolicy(("src",), check=check))
    assert state == "completed" and vid == (EVIDENCE_VERIFIER_ID,)
    [event] = events
    assert event["passed"] is True and event["reasons"] == []
    assert set(event) == {"task_id", "attempt_id", "candidate_sha256", "evidence_artifact_id", "evidence_sha256",
                          "passed", "reasons"}


def test_evidence_artifact_records_paths_and_check(tmp_path, repo):
    import json
    state, events, _, env, *_ = run_edit(tmp_path, repo, write(("src/a.py", "y = 2\n")),
                                         EvidencePolicy(("src",), check=CheckCommand((PY, "-c", "print('ok')"))))
    body = json.loads(env.blobs.read(events[0]["evidence_sha256"]))
    assert body["changed_paths"] == ["src/a.py"] and body["check"]["exit_code"] == 0
    assert body["check"]["output_tail"].strip() == "ok" and body["candidate_artifact_id"]


@pytest.mark.parametrize("script, policy, reason", [
    (write(), EvidencePolicy(("src",)), "no_changes"),
    (write(("README", "changed\n")), EvidencePolicy(("src",)), "path_outside_allowed"),
    (write(("src/a.py", "y\n"), ("srcx/evil.py", "x\n")), EvidencePolicy(("src",)), "path_outside_allowed"),
    (write(("src/a.py", "y\n"), (".github/workflows/x.yml", "x\n")), EvidencePolicy(("src",)), "path_outside_allowed"),
    (write(link=("src/escape", "/etc/passwd")), EvidencePolicy(("src",)), "symlink_changed"),
    (write(("src/a.py", "y\n"), commit=True), EvidencePolicy(("src",)), "head_moved"),
    (write(("src/a.py", "y\n")), EvidencePolicy(("src",), check=CheckCommand((PY, "-c", "raise SystemExit(3)"))),
     "check_failed"),
    (write(("src/a.py", "y\n")), EvidencePolicy(("src",), check=CheckCommand(("/nonexistent/bin/check",))),
     "check_not_started"),
])
def test_missing_or_out_of_bounds_evidence_is_rejected(tmp_path, repo, script, policy, reason):
    state, events, vid, *_ = run_edit(tmp_path, repo, script, policy)
    assert state == "changes_required" and vid == (EVIDENCE_VERIFIER_ID,)
    assert reason in events[0]["reasons"] and events[0]["passed"] is False


def test_check_is_not_run_when_paths_already_fail(tmp_path, repo):
    marker = tmp_path / "ran"
    check = CheckCommand((PY, "-c", "open(%r,'w').close()" % str(marker)))
    state, events, *_ = run_edit(tmp_path, repo, write(("README", "x\n")), EvidencePolicy(("src",), check=check))
    assert state == "changes_required" and not marker.exists()


def test_unconfigured_node_delegates_to_the_process_outcome_verifier(tmp_path, repo):
    state, events, vid, *_ = run_edit(tmp_path, repo, write(), EvidencePolicy(("src",)), configured=False)
    assert state == "completed" and vid == (VERIFIER_ID,) and events == []


def test_tampered_or_foreign_candidate_bytes_are_rejected(tmp_path, repo):
    import asyncio
    import json

    def inspect(env, verifier):
        task = env.tasks["edit"]
        art = env.db.execute("SELECT candidate_artifact_id FROM verification_results WHERE task_id=?",
                             (task,)).fetchone()[0]
        sha = env.db.execute("SELECT sha256 FROM artifacts WHERE id=?", (art,)).fetchone()[0]
        forged = json.loads(env.blobs.read(sha))
        forged["detail"] = "forged"  # still a valid succeeded summary for this attempt, but not the recorded bytes
        forged = json.dumps(forged).encode()
        assert asyncio.run(process_outcome_verifier(task, forged)) is True  # the weak verifier would accept it
        assert asyncio.run(verifier(task, forged)) is False
        assert "candidate_hash_mismatch" in env.events(EVIDENCE_EVENT)[-1]["reasons"]
        assert asyncio.run(verifier(task, b"{not json")) is False
        assert "process_outcome_rejected" in env.events(EVIDENCE_EVENT)[-1]["reasons"]
        inspected.append(True)
    inspected = []
    run_edit(tmp_path, repo, write(("src/a.py", "y\n")), EvidencePolicy(("src",)), inspect=inspect)
    assert inspected == [True]


# ------------------------------------------------------------------------------------------- config
@pytest.mark.parametrize("argv", [["/bin/true"], "/bin/true -c x", ("true",), ("/bin/../bin/true",), (),
                                  ("/bin/true", "a\x00b"), tuple("/bin/true" for _ in range(65))])
def test_check_command_refuses_shell_strings_and_relative_executables(argv):
    with pytest.raises(ValueError):
        CheckCommand(argv)


@pytest.mark.parametrize("kw", [{"timeout_seconds": 0}, {"timeout_seconds": 7200}, {"timeout_seconds": True},
                                {"max_output_bytes": 0}, {"max_output_bytes": 1 << 30}])
def test_check_command_bounds(kw):
    with pytest.raises(ValueError):
        CheckCommand(("/bin/true",), **kw)


@pytest.mark.parametrize("path", ["", "/etc", "../x", "src/../../x", "a/../b", ".", "./src", ".git", ".git/hooks",
                                  "src\\x", "src\nx", "src//x"])
def test_allowed_paths_cannot_escape_the_worktree(path):
    with pytest.raises(ValueError):
        EvidencePolicy((path,))


def test_allowed_path_prefix_is_component_wise():
    policy = EvidencePolicy(("src/", "docs/a.md"))
    assert policy.allowed_paths == ("src", "docs/a.md")
    assert policy.allows("src/x.py") and policy.allows("docs/a.md")
    assert not policy.allows("srcx/y") and not policy.allows("docs/a.md.bak") and not policy.allows("docs")


def test_verifier_configuration_is_validated(tmp_path):
    with pytest.raises(ValueError):
        EvidenceVerifier(None, None, {"t": {}}, {"t": EvidencePolicy(("src",))}, None,
                         fallback=process_outcome_verifier, fallback_id=VERIFIER_ID)  # no evidence source
    with pytest.raises(ValueError):
        EvidenceVerifier(None, None, {}, {"t": EvidencePolicy(("src",))}, object(),
                         fallback=process_outcome_verifier, fallback_id=VERIFIER_ID)  # unknown task


# ------------------------------------------------------------------------------------------- run_check
def test_run_check_timeout_kills_the_whole_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    code = ("import os,subprocess,sys,time;"
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
            "open(%r,'w').write(str(p.pid)); time.sleep(60)" % str(pidfile))
    started = time.monotonic()
    result = run_check(CheckCommand((PY, "-c", code), timeout_seconds=1.0), str(tmp_path))
    assert result.timed_out and time.monotonic() - started < 10
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with open("/proc/%d/stat" % child) as fh:
                if fh.read().split(") ")[1].startswith("Z"):
                    break
        except FileNotFoundError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("grandchild survived the group kill")


def test_run_check_huge_output_is_bounded_and_fails(tmp_path):
    started = time.monotonic()
    result = run_check(CheckCommand((PY, "-c", "import sys\nwhile True: sys.stdout.write('x'*65536)"),
                                    timeout_seconds=30, max_output_bytes=100_000), str(tmp_path))
    assert result.output_exceeded and not result.timed_out and time.monotonic() - started < 10
    assert len(result.tail) <= 4096


def test_run_check_env_is_hardened_and_no_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret-token")
    monkeypatch.setenv("SYNAPSE_TEST_SECRET", "secret")
    result = run_check(CheckCommand((PY, "-c", "import os,json; print(json.dumps(dict(os.environ)))")), str(tmp_path))
    assert result.exit_code == 0
    import json
    env = json.loads(result.tail)
    assert "secret" not in result.tail.decode() and env["HOME"] == "/nonexistent"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    marker = tmp_path / "pwned"
    # Shell metacharacters are passed as one literal argument, never interpreted.
    result = run_check(CheckCommand(("/bin/echo", "x; touch %s" % marker)), str(tmp_path))
    assert result.exit_code == 0 and not marker.exists()


def test_run_check_exit_codes(tmp_path):
    assert run_check(CheckCommand(("/bin/true",)), str(tmp_path)).exit_code == 0
    assert run_check(CheckCommand(("/bin/false",)), str(tmp_path)).exit_code != 0
    assert run_check(CheckCommand((str(tmp_path / "missing"),)), str(tmp_path)).start_failed
