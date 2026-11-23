"""Hermetic tests for scripts/pre-push, scripts/install-hooks.sh and scripts/merge-check.sh (temp git repos, no network)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

STUB_CHECK = """#!/usr/bin/env bash
echo "$@" >> "$(git rev-parse --show-toplevel)/.check-calls"
[ -e "$(git rev-parse --show-toplevel)/BAD" ] && { echo "stub: failing"; exit 1; }
echo "stub: ok"
"""


def git(cwd: Path, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    full_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", **(env or {})}
    return subprocess.run(["git", *args], cwd=cwd, env=full_env, text=True, capture_output=True, check=check)


def write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def commit(repo: Path, rel: str, text: str, msg: str = "c") -> str:
    write(repo, rel, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def make_repo(tmp_path: Path, name: str = "work") -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    if not origin.exists():
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / name
    git(tmp_path, "clone", "-q", str(origin), str(repo), check=False)
    git(repo, "checkout", "-q", "-B", "main")
    (repo / "scripts").mkdir(exist_ok=True)
    for f in ("pre-push", "install-hooks.sh", "merge-check.sh"):
        shutil.copy(SCRIPTS / f, repo / "scripts" / f)
    write(repo, "scripts/check.sh", STUB_CHECK)
    for f in ("pre-push", "install-hooks.sh", "merge-check.sh", "check.sh"):
        (repo / "scripts" / f).chmod(0o755)
    commit(repo, "README.md", "hi", "init")
    git(repo, "push", "-q", "origin", "main")
    return origin, repo


def calls(repo: Path) -> list[str]:
    p = repo / ".check-calls"
    return p.read_text().splitlines() if p.exists() else []


def push(repo: Path, *extra: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return git(repo, "push", "-q", "origin", "HEAD:refs/heads/feature", *extra, env=env, check=False)


# ---- install-hooks.sh ----------------------------------------------------------------------------------------

def install(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(repo / "scripts/install-hooks.sh"), *args], cwd=repo, text=True, capture_output=True)


def test_install_is_symlink_idempotent_and_never_overwrites(tmp_path):
    _, repo = make_repo(tmp_path)
    hook = repo / ".git/hooks/pre-push"
    assert not hook.exists()
    assert install(repo).returncode == 0
    assert hook.is_symlink() and hook.resolve() == (repo / "scripts/pre-push").resolve()
    again = install(repo)
    assert again.returncode == 0 and "already installed" in again.stdout

    hook.unlink()
    hook.write_text("#!/bin/sh\necho mine\n")
    refused = install(repo)
    assert refused.returncode != 0 and "--force" in refused.stderr
    assert hook.read_text().startswith("#!/bin/sh") and not hook.is_symlink()

    assert install(repo, "--force").returncode == 0
    assert hook.is_symlink()
    assert (repo / ".git/hooks/pre-push.bak").read_text().endswith("echo mine\n")
    assert install(repo, "--remove").returncode == 0 and not hook.exists()


def test_install_uses_git_path_hooks_in_a_worktree(tmp_path):
    _, repo = make_repo(tmp_path)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "other", str(wt))
    expected = Path(git(wt, "rev-parse", "--git-path", "hooks").stdout.strip())
    expected = (wt / expected).resolve() / "pre-push"
    assert install(wt).returncode == 0
    assert expected.is_symlink()


# ---- pre-push ------------------------------------------------------------------------------------------------

def test_hook_runs_quick_check_for_code_and_blocks_on_failure(tmp_path):
    _, repo = make_repo(tmp_path)
    install(repo)
    commit(repo, "src/a.py", "x=1")
    ok = push(repo)
    assert ok.returncode == 0
    assert calls(repo) == ["--quick"]

    commit(repo, "BAD", "1", "break")
    commit(repo, "tests/test_a.py", "x=2")
    blocked = push(repo)
    assert blocked.returncode != 0
    assert "push blocked" in blocked.stderr
    assert git(repo, "ls-remote", "origin", "refs/heads/feature").stdout.split()[0] != \
        git(repo, "rev-parse", "HEAD").stdout.strip()


def test_hook_skips_docs_only_push(tmp_path):
    _, repo = make_repo(tmp_path)
    install(repo)
    commit(repo, "BAD", "1", "would fail")
    git(repo, "push", "-q", "origin", "main", "--no-verify")
    open(repo / ".check-calls", "w").close()
    commit(repo, "docs/a.md", "text")
    commit(repo, "README.md", "more")
    res = git(repo, "push", "origin", "HEAD:refs/heads/docs", check=False)
    assert res.returncode == 0
    assert "skipping local check" in res.stderr + res.stdout
    assert calls(repo) == []


def test_hook_checks_code_in_earlier_commit_of_new_branch(tmp_path):
    _, repo = make_repo(tmp_path)
    install(repo)
    commit(repo, "BAD", "1")
    commit(repo, "src/a.py", "x")
    commit(repo, "docs/a.md", "x")
    assert push(repo).returncode != 0


def test_hook_bypasses_and_never_leaks_secrets(tmp_path):
    _, repo = make_repo(tmp_path)
    install(repo)
    commit(repo, "BAD", "1")
    commit(repo, "src/a.py", "x")
    secret = {"MY_API_TOKEN": "s3cr3t-value-123"}
    assert push(repo, env={**secret, "SKIP_CHECK": "1"}).returncode == 0
    assert calls(repo) == []
    commit(repo, "src/b.py", "y")
    assert push(repo, "--no-verify", env=secret).returncode == 0
    commit(repo, "src/c.py", "z")
    blocked = push(repo, env=secret)
    assert blocked.returncode != 0
    assert "s3cr3t-value-123" not in blocked.stdout + blocked.stderr


def test_hook_ignores_branch_deletion(tmp_path):
    _, repo = make_repo(tmp_path)
    install(repo)
    commit(repo, "src/a.py", "x")
    assert push(repo).returncode == 0
    open(repo / ".check-calls", "w").close()
    res = git(repo, "push", "origin", "--delete", "feature", check=False)
    assert res.returncode == 0 and calls(repo) == []


# ---- merge-check.sh ------------------------------------------------------------------------------------------

def make_pr(repo: Path, origin: Path, number: int, rel: str, text: str) -> None:
    git(repo, "checkout", "-q", "-B", f"pr{number}", "main")
    commit(repo, rel, text, f"pr {number}")
    git(repo, "push", "-q", str(origin), f"HEAD:refs/pull/{number}/head")
    git(repo, "checkout", "-q", "main")


def refs(repo: Path) -> str:
    return git(repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout


def merge_check(repo: Path, *prs: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(repo / "scripts/merge-check.sh"), *prs], cwd=repo, text=True, capture_output=True,
                          env={**os.environ, "MERGE_CHECK_CMD": "scripts/check.sh", "TMPDIR": str(repo.parent), **(env or {})})


def test_merge_check_names_first_breaking_pr_and_cleans_up(tmp_path):
    origin, repo = make_repo(tmp_path)
    make_pr(repo, origin, 1, "a.txt", "a")
    make_pr(repo, origin, 2, "BAD", "1")      # this one breaks the stubbed suite
    make_pr(repo, origin, 3, "c.txt", "c")
    before_local, before_origin = refs(repo), refs(origin)
    res = merge_check(repo, "1", "2", "3")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "First breaking PR in the given order: #2" in res.stdout
    assert refs(repo) == before_local and refs(origin) == before_origin
    assert git(repo, "worktree", "list").stdout.count("\n") == 1
    assert not list(tmp_path.glob("merge-check.*"))


def test_merge_check_passes_when_all_good(tmp_path):
    origin, repo = make_repo(tmp_path)
    make_pr(repo, origin, 1, "a.txt", "a")
    make_pr(repo, origin, 2, "b.txt", "b")
    res = merge_check(repo, "1", "2")
    assert res.returncode == 0 and "all 2 PR(s) pass together" in res.stdout
    assert len(calls(repo)) == 0  # ran in the temporary worktree, not the checkout


def test_merge_check_reports_conflict_between_prs(tmp_path):
    origin, repo = make_repo(tmp_path)
    make_pr(repo, origin, 1, "same.txt", "one")
    make_pr(repo, origin, 2, "same.txt", "two")
    res = merge_check(repo, "1", "2")
    assert res.returncode == 1 and "PR #2 does not merge cleanly" in res.stdout
    assert git(repo, "worktree", "list").stdout.count("\n") == 1


def test_merge_check_rejects_bad_arguments(tmp_path):
    _, repo = make_repo(tmp_path)
    assert merge_check(repo).returncode == 2
    assert merge_check(repo, "abc").returncode == 2
    missing = merge_check(repo, "99")
    assert missing.returncode == 2 and "cannot fetch PR #99" in missing.stderr
