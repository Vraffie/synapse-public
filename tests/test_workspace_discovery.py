"""Workspace discovery (synapse.application.workspace): bounded, name-only walk; hardened read-only git queries.

Every repository here is a real `git init` in tmp_path; nothing outside tmp_path is scanned.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

from synapse.application import workspace as ws

GIT_ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True).stdout


def make_repo(path: Path, commits=1, branch="main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    for i in range(commits):
        git(path, "commit", "-q", "--allow-empty", "-m", "c%d" % i)
    return path


def scan(path, **kw):
    return ws.discover_repos(str(Path(path).resolve()), **kw)


def by_rel(result):
    return {r.rel: r for r in result.repos}


def test_mixed_workspace_reports_repos_only_with_branch_head_and_dirty(tmp_path):
    root = tmp_path / "BSS"
    a = make_repo(root / "alpha")
    (a / "tracked.txt").write_text("1")
    git(a, "add", "tracked.txt")
    git(a, "commit", "-q", "-m", "t")
    (a / "tracked.txt").write_text("2")             # modified
    (a / "new1").write_text("x")                     # untracked
    (a / "new2").write_text("x")
    b = make_repo(root / "beta", branch="develop")
    git(b, "checkout", "-q", "-b", "feature/x")
    make_repo(root / "tools")
    for plain in ("data/raw", "documentation", "reports", "requirements"):
        (root / plain).mkdir(parents=True)
    (root / "data" / "notes.txt").write_text("not a repo")
    res = scan(root)
    repos = by_rel(res)
    assert sorted(repos) == ["alpha", "beta", "tools"] and not res.root_is_repo and not res.truncated
    alpha, beta = repos["alpha"], repos["beta"]
    assert alpha.usable and alpha.dirty_count == 3 and alpha.has_uncommitted_changes is True
    assert alpha.commit_count == 2 and alpha.branch == "main" and len(alpha.head) == 40
    assert alpha.path == str((root / "alpha").resolve()) and os.path.isabs(alpha.path)
    assert beta.branch == "feature/x" and beta.branches == ("feature/x", "develop") and beta.dirty_count == 0
    assert beta.has_uncommitted_changes is False


def test_folder_that_is_itself_a_repo_is_one_repo(tmp_path):
    repo = make_repo(tmp_path / "solo")
    make_repo(repo / "vendor" / "inner")  # nested: never reported
    res = scan(repo)
    assert res.root_is_repo and [r.rel for r in res.repos] == ["."] and res.repos[0].usable


def test_nested_repo_is_not_double_counted(tmp_path):
    root = tmp_path / "ws"
    make_repo(root / "outer")
    make_repo(root / "outer" / "sub" / "inner")
    assert [r.rel for r in scan(root).repos] == ["outer"]


def test_symlinks_are_reported_not_followed_including_loops(tmp_path):
    outside = make_repo(tmp_path / "outside")
    root = tmp_path / "ws"
    root.mkdir()
    (root / "link-to-repo").symlink_to(outside)
    (root / "loop-a").symlink_to(root / "loop-b")
    (root / "loop-b").symlink_to(root / "loop-a")
    (root / "file-link").symlink_to(tmp_path / "nowhere.txt")
    res = scan(root)
    assert res.repos == ()
    reasons = {Path(s.path).name: s.reason for s in res.skipped}
    assert reasons["link-to-repo"] == "symlink to a directory (not followed)"
    assert "loop" in reasons["loop-a"] and "loop" in reasons["loop-b"]
    assert "file-link" not in reasons


def _hostile_repo(path: Path, marker: Path, key: str) -> Path:
    repo = make_repo(path)
    (repo / "f").write_text("x")
    git(repo, "add", "f")
    git(repo, "commit", "-q", "-m", "f")
    script = path.parent / ("evil-" + path.name)
    script.write_text("#!/bin/sh\necho ran >> '%s'\nexit 0\n" % marker)
    script.chmod(0o755)
    if key == "core.hooksPath":
        hooks = path.parent / ("hooks-" + path.name)
        hooks.mkdir()
        for hook in ("pre-commit", "post-checkout", "post-index-change", "reference-transaction", "pre-auto-gc"):
            (hooks / hook).write_bytes(script.read_bytes())
            (hooks / hook).chmod(0o755)
        git(repo, "config", "core.hooksPath", str(hooks))
    else:
        git(repo, "config", key, str(script))
    return repo


@pytest.mark.parametrize("key", ["core.fsmonitor", "core.hooksPath", "core.pager"])
def test_hostile_config_never_executes(tmp_path, key):
    marker = tmp_path / "marker"
    root = tmp_path / "ws"
    _hostile_repo(root / "evil", marker, key)
    res = scan(root)
    info = by_rel(res)["evil"]
    assert not marker.exists(), "a repo-chosen program ran during discovery"
    if key == "core.pager":  # not a refused key; overridden by the hardened runner, so the repo stays usable
        assert info.usable
    else:
        assert not info.usable and "unsafe config" in info.reason and key.lower() in info.reason
        assert info.dirty_count is None  # git status is never run against a refused repo


def test_fsmonitor_fixture_is_really_hostile_without_hardening(tmp_path):
    """Control: the same fixture DOES run the script under plain git, so the test above is meaningful."""
    marker = tmp_path / "marker"
    repo = _hostile_repo(tmp_path / "ws" / "evil", marker, "core.fsmonitor")
    subprocess.run(["git", "status"], cwd=repo, env=GIT_ENV, capture_output=True)
    assert marker.exists()


def test_repo_without_commits_is_unusable(tmp_path):
    root = tmp_path / "ws"
    make_repo(root / "fresh", commits=0)
    info = by_rel(scan(root))["fresh"]
    assert not info.usable and "no commits" in info.reason and info.head is None and info.branch == "main"


def test_detached_head(tmp_path):
    repo = make_repo(tmp_path / "ws" / "det", commits=2)
    git(repo, "checkout", "-q", "--detach", "HEAD~1")
    info = ws.inspect_repo(str(repo.resolve()))
    assert info.detached and info.branch is None and info.usable and info.commit_count == 1
    assert info.branches == ("main",)


def test_branch_list_is_capped_current_first(tmp_path):
    repo = make_repo(tmp_path / "many")
    head = git(repo, "rev-parse", "HEAD").strip()
    stdin = "".join("create refs/heads/b%03d %s\n" % (i, head) for i in range(70))
    subprocess.run(["git", "update-ref", "--stdin"], cwd=repo, env=GIT_ENV, input=stdin, text=True, check=True)
    git(repo, "checkout", "-q", "b069")
    info = ws.inspect_repo(str(repo.resolve()))
    assert len(info.branches) == ws.MAX_BRANCHES and info.branches_truncated
    assert info.branches[0] == "b069" and list(info.branches[1:]) == sorted(info.branches[1:])


def test_commit_count_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "COMMIT_COUNT_CAP", 3)
    info = ws.inspect_repo(str(make_repo(tmp_path / "long", commits=6).resolve()))
    assert info.commit_count == 4 and info.commit_count_display == "3+"


def test_dirty_count_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "DIRTY_COUNT_CAP", 5)
    repo = make_repo(tmp_path / "dirty")
    for i in range(9):
        (repo / ("u%d" % i)).write_text("x")
    info = ws.inspect_repo(str(repo.resolve()))
    assert info.dirty_count == 5 and info.dirty_truncated and info.has_uncommitted_changes


def test_max_entries_sets_truncated(tmp_path):
    root = tmp_path / "ws"
    for i in range(10):
        (root / ("d%02d" % i)).mkdir(parents=True)
    res = scan(root, max_entries=5)
    assert res.truncated and res.entries_seen == 5
    assert not scan(root).truncated


def test_max_depth_bounds_the_walk(tmp_path):
    root = tmp_path / "ws"
    make_repo(root / "a" / "b" / "deep")
    res = scan(root)  # default depth 2: ws/a (1), ws/a/b (2); ws/a/b/deep is depth 3
    assert res.repos == () and res.depth_limited
    res = scan(root, max_depth=3)
    assert [r.rel for r in res.repos] == ["a/b/deep"] and not res.depth_limited


def test_hidden_and_dependency_dirs_are_not_descended(tmp_path):
    root = tmp_path / "ws"
    make_repo(root / ".hidden" / "r")
    make_repo(root / "node_modules" / "pkg")
    make_repo(root / ".venv")
    res = scan(root)
    assert res.repos == () and res.ignored_dirs == 3


@pytest.mark.parametrize("bad", ["relative/path", "/tmp/x\n", "/tmp/\x1b[31m", "/tmp/../tmp", "", "/does/not/exist"])
def test_bad_input_paths_are_rejected(bad):
    with pytest.raises(ValueError):
        ws.discover_repos(bad)


def test_symlinked_folder_and_file_input_are_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)
    with pytest.raises(ValueError, match="resolved"):
        ws.discover_repos(str(tmp_path.resolve() / "alias"))
    (real / "f").write_text("x")
    with pytest.raises(ValueError, match="directory"):
        ws.discover_repos(str((real / "f").resolve()))
    with pytest.raises(ValueError, match="not a git repository"):
        ws.inspect_repo(str(real.resolve()))


def test_control_character_dir_name_is_skipped(tmp_path):
    root = tmp_path / "ws"
    make_repo(root / "bad\x1bname")
    res = scan(root)
    assert res.repos == () and res.skipped[0].reason == "name contains control characters"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unreadable_dir_is_skipped_not_fatal(tmp_path):
    root = tmp_path / "ws"
    make_repo(root / "ok")
    locked = root / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        res = scan(root)
    finally:
        locked.chmod(stat.S_IRWXU)
    assert [r.rel for r in res.repos] == ["ok"]
    assert [(Path(s.path).name, s.reason) for s in res.skipped] == [("locked", "unreadable")]


def test_spaces_and_unicode_in_paths(tmp_path):
    root = tmp_path / "my wörk space"
    make_repo(root / "prøve æ repo")
    res = scan(root)
    assert [r.rel for r in res.repos] == ["prøve æ repo"] and res.repos[0].usable


def test_bare_and_linked_worktree_are_reported_unusable(tmp_path):
    root = tmp_path / "ws"
    main = make_repo(root / "main")
    git(root, "init", "-q", "--bare", "bare.git")
    git(main, "worktree", "add", "-q", str(root / "linked"))
    repos = by_rel(scan(root))
    assert repos["main"].usable
    assert repos["bare.git"].kind == "bare" and not repos["bare.git"].usable
    assert repos["linked"].kind == "linked" and not repos["linked"].usable


def test_shallow_flag(tmp_path):
    src = make_repo(tmp_path / "src", commits=3)
    git(tmp_path, "clone", "-q", "--depth", "1", "file://%s" % src, "shallow")
    info = ws.inspect_repo(str((tmp_path / "shallow").resolve()))
    assert info.shallow and info.usable and info.commit_count == 1


def test_select_repos_refuses_unknown_outside_and_unusable(tmp_path):
    root = tmp_path / "ws"
    make_repo(root / "good")
    make_repo(root / "fresh", commits=0)
    outside = make_repo(tmp_path / "elsewhere")
    res = scan(root)
    assert [r.rel for r in ws.select_repos(res, ["good", "good", str(root.resolve() / "good")])] == ["good"]
    with pytest.raises(ValueError, match="outside"):
        ws.select_repos(res, ["../elsewhere"])
    with pytest.raises(ValueError, match="not a repository found"):
        ws.select_repos(res, [str(outside.resolve())])
    with pytest.raises(ValueError, match="cannot be registered: no commits"):
        ws.select_repos(res, ["fresh"])
    with pytest.raises(ValueError, match="not a repository found"):
        ws.select_repos(res, ["missing"])
    with pytest.raises(ValueError, match="control"):
        ws.select_repos(res, ["good\n"])
    with pytest.raises(ValueError, match="no repository"):
        ws.select_repos(res, [])


def test_module_stays_out_of_daemon_and_desktop():
    src = Path(ws.__file__).read_text()
    for forbidden in ("synapse.daemon", "synapse.persistence", "synapse.desktop", "synapse.api", "sqlite3",
                      "shell=True"):
        assert forbidden not in src
