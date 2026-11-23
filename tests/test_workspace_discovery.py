"""Workspace discovery (synapse.application.workspace): bounded, name-only walk; hardened read-only git queries.

Every repository here is a real `git init` in tmp_path; nothing outside tmp_path is scanned.
"""
import os
import shutil
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


def _partial_clone_with_hostile_uploadpack(root: Path, marker: Path) -> Path:
    """Security review repro: a promisor remote whose uploadpack runs on a lazy fetch during `status`."""
    repo = make_repo(root / "promisor", commits=0)
    (repo / "f").write_text("x")
    git(repo, "add", "f")
    git(repo, "commit", "-q", "-m", "f")
    tree = git(repo, "rev-parse", "HEAD^{tree}").strip()
    (repo / ".git" / "objects" / tree[:2] / tree[2:]).unlink()
    git(repo, "config", "core.repositoryformatversion", "1")
    git(repo, "config", "extensions.partialclone", "origin")
    git(repo, "config", "remote.origin.promisor", "true")
    git(repo, "config", "remote.origin.url", str(root / "promisor"))
    git(repo, "config", "remote.origin.uploadpack", "touch '%s'; git-upload-pack" % marker)
    return repo


def test_promisor_uploadpack_is_refused_and_never_runs(tmp_path):
    marker = tmp_path / "marker"
    _partial_clone_with_hostile_uploadpack(tmp_path / "ws", marker)
    info = by_rel(scan(tmp_path / "ws"))["promisor"]
    assert not marker.exists()
    assert not info.usable and "remote.origin.uploadpack" in info.reason


def test_lazy_fetch_is_disabled_even_if_the_guard_missed_a_key(tmp_path, monkeypatch):
    """Defence in depth: with the guard bypassed, GIT_NO_LAZY_FETCH in the runner still stops the fetch."""
    marker = tmp_path / "marker"
    repo = _partial_clone_with_hostile_uploadpack(tmp_path / "ws", marker)
    monkeypatch.setattr(ws, "refuse_unsafe_repo_config", lambda repo, cwd=None: None)
    ws.inspect_repo(str(repo.resolve()))
    assert not marker.exists()


def test_oversized_config_fails_closed(tmp_path):
    repo = make_repo(tmp_path / "ws" / "big")
    with open(repo / ".git" / "config", "a") as fh:
        for i in range(60000):  # > 1 MiB when listed
            fh.write('[pad "p%05d"]\n\tk = %s\n' % (i, "v" * 20))
        fh.write("[core]\n\tsshCommand = evil\n")
    info = ws.inspect_repo(str(repo.resolve()))
    assert not info.usable and "too large" in info.reason


def test_scan_time_limit_marks_truncated(tmp_path):
    root = tmp_path / "ws"
    make_repo(root / "a")
    make_repo(root / "b")
    res = scan(root, deadline_seconds=0)
    assert res.truncated and [r.rel for r in res.repos] == ["a", "b"]
    assert {r.reason for r in res.repos} == {"not inspected: scan time limit reached"}
    assert not any(r.usable for r in res.repos)


def plant_commondir(repo: str, marker) -> None:
    """Same repro as tests/test_worktrees.py: commondir points at a dir with a filter + info/attributes."""
    common = Path(repo).parent / "evil-common"
    shutil.copytree(Path(repo) / ".git", common)
    for key in ("filter.x.clean", "filter.x.smudge"):
        git(repo, "config", "--file", str(common / "config"), key, "touch '%s'; cat" % marker)
    (common / "info").mkdir(exist_ok=True)
    (common / "info" / "attributes").write_text("* filter=x\n")
    (Path(repo) / ".git" / "commondir").write_text(str(common) + "\n")
    future = (Path(repo) / "a.txt").stat().st_mtime + 100
    os.utime(Path(repo) / "a.txt", (future, future))


def test_commondir_fixture_is_really_hostile_without_hardening(tmp_path):
    marker = tmp_path / "marker"
    repo = make_repo(tmp_path / "ws" / "redirected")
    (repo / "a.txt").write_text("hi\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "a")
    plant_commondir(str(repo), marker)
    subprocess.run(["git", "status"], cwd=repo, env=GIT_ENV, capture_output=True)
    assert marker.exists()


def test_commondir_redirect_is_unusable_and_no_filter_runs(tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    root = tmp_path / "ws"
    repo = make_repo(root / "redirected")
    (repo / "a.txt").write_text("hi\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "a")
    plant_commondir(str(repo.resolve()), marker)
    info = by_rel(scan(root))["redirected"]
    assert not marker.exists() and not info.usable and "commondir" in info.reason
    # Even past the early refusal, the config guard itself refuses it before any git status.
    with pytest.raises(ws.WorktreeError, match="commondir"):
        ws.refuse_unsafe_repo_config(str(repo.resolve()))
    assert not marker.exists()


def test_refused_repo_gets_no_git_query_at_all(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    repo = make_repo(root / "evil")
    git(repo, "config", "core.sshCommand", "evil")
    calls = []
    real = ws._git
    monkeypatch.setattr(ws, "_git", lambda *a, **k: calls.append(a[1]) or real(*a, **k))
    info = ws.inspect_repo(str(repo.resolve()))
    assert not info.usable and "sshcommand" in info.reason and calls == []
    assert info.branch is None and info.head is None


def test_filter_via_info_attributes_is_refused(tmp_path):
    marker = tmp_path / "marker"
    repo = make_repo(tmp_path / "ws" / "filt")
    (repo / "a.txt").write_text("hi\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "a")
    git(repo, "config", "filter.x.clean", "touch '%s'; cat" % marker)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* filter=x\n")
    info = ws.inspect_repo(str(repo.resolve()))
    assert not info.usable and "filter.x.clean" in info.reason and not marker.exists()
    git(repo, "config", "--unset", "filter.x.clean")
    info = ws.inspect_repo(str(repo.resolve()))
    assert not info.usable and "info/attributes" in info.reason and not marker.exists()


def test_dot_git_symlink_is_refused(tmp_path):
    real = make_repo(tmp_path / "real")
    linked = tmp_path / "ws" / "linked"
    linked.mkdir(parents=True)
    (linked / ".git").symlink_to(real / ".git")
    info = by_rel(scan(tmp_path / "ws"))["linked"]
    assert not info.usable and info.reason == ".git is a symlink" and info.head is None


def test_not_owned_by_current_user_is_refused(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "ws" / "theirs")
    monkeypatch.setattr(os, "getuid", lambda: os.stat(repo).st_uid + 1)
    info = ws.inspect_repo(str(repo.resolve()))
    assert not info.usable and info.reason == "not owned by the current user" and info.head is None


def _gitdir_state(repo: Path) -> dict:
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in (repo / ".git").rglob("*") if p.is_file()}


def test_status_writes_nothing_into_the_repository(tmp_path):
    """GIT_OPTIONAL_LOCKS=0: a stat-dirty tracked file and an untracked file must not make our
    `status` refresh the index (or write anything else) in the operator's repository."""
    repo = make_repo(tmp_path / "ws" / "quiet")
    (repo / "a.txt").write_text("hi\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "a")
    future = (repo / "a.txt").stat().st_mtime + 100
    os.utime(repo / "a.txt", (future, future))  # stat-dirty, content unchanged
    (repo / "untracked.txt").write_text("u")
    before = _gitdir_state(repo)
    info = ws.inspect_repo(str(repo.resolve()))
    assert info.usable and info.dirty_count == 1  # only the untracked file: a.txt content is unchanged
    assert _gitdir_state(repo) == before
    subprocess.run(["git", "status"], cwd=repo, env=GIT_ENV, capture_output=True, check=True)
    assert _gitdir_state(repo) != before  # control: plain git status does refresh the index


def test_pick_repos_inspects_only_the_chosen(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    make_repo(root / "chosen")
    make_repo(root / "other")
    seen = []
    real = ws.inspect_repo
    monkeypatch.setattr(ws, "inspect_repo", lambda p, **k: seen.append(os.path.basename(p)) or real(p, **k))
    assert [r.rel for r in ws.pick_repos(str(root.resolve()), ["chosen"])] == ["chosen"]
    assert seen == ["chosen"]
    with pytest.raises(ValueError, match="outside"):
        ws.pick_repos(str(root.resolve()), ["../x"])
