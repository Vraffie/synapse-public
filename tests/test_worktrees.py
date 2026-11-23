"""WorktreeManager tests using real git in tmp_path."""
import os
import subprocess
import threading

import pytest

from synapse.infrastructure.worktrees import WorktreeError, WorktreeManager
from synapse.providers.claude_cli import validate_worktree

ENV = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
       "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("hi\n")
    run_git(r, "add", "a.txt")
    run_git(r, "commit", "-q", "-m", "init")
    return str(r.resolve())


@pytest.fixture
def mgr(repo, tmp_path):
    return WorktreeManager([repo], str((tmp_path / "wt").resolve()))


def test_create_remove_and_provider_accepts(mgr, repo, tmp_path):
    p = mgr.create("run1", "task1", repo, "main")
    assert p == os.path.join(mgr._root, "run1", "task1")
    assert os.path.isfile(os.path.join(p, ".git"))  # worktree .git is a FILE
    assert validate_worktree(p, str(tmp_path / "state")) == p
    assert run_git(p, "symbolic-ref", "--short", "HEAD").strip() == "synapse/run1/task1"
    st = mgr.status("run1", "task1")
    assert st.clean and st.branch == "synapse/run1/task1" and len(st.head) == 40
    assert mgr.remove("run1", "task1") is True
    assert not os.path.exists(p)
    assert mgr.remove("run1", "task1") is False


def test_create_idempotent_and_recreate(mgr, repo):
    p = mgr.create("r", "t", repo, "main")
    assert mgr.create("r", "t", repo, "main") == p
    mgr.remove("r", "t")
    assert mgr.create("r", "t", repo, "main") == p


def test_dirty_refused_unless_force(mgr, repo):
    p = mgr.create("r", "t", repo, "main")
    with open(os.path.join(p, "a.txt"), "a") as f:
        f.write("more\n")
    open(os.path.join(p, "new.txt"), "w").write("x")
    st = mgr.status("r", "t")
    assert not st.clean and st.changed_paths == 2 and "a.txt" in st.diff_stat
    assert "+more" in mgr.diff("r", "t").text
    with pytest.raises(WorktreeError):
        mgr.remove("r", "t")
    assert os.path.exists(p)
    assert mgr.remove("r", "t", force=True)
    assert not os.path.exists(p)


def test_diff_bounded(mgr, repo):
    p = mgr.create("r", "t", repo, "main")
    open(os.path.join(p, "a.txt"), "w").write("z\n" * 10000)
    d = mgr.diff("r", "t", max_bytes=500)
    assert d.truncated and len(d.text) <= 500


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", ".", "", " ", "-x", "a b", "x.lock", "a\x00b", "a..b"])
def test_bad_ids(mgr, repo, bad):
    with pytest.raises(ValueError):
        mgr.create(bad, "t", repo, "main")
    with pytest.raises(ValueError):
        mgr.create("r", bad, repo, "main")
    with pytest.raises(ValueError):
        mgr.remove(bad, "t")


@pytest.mark.parametrize("ref", ["--upload-pack=x", "-b", "", "nope", "main\n--x"])
def test_bad_base_ref(mgr, repo, ref):
    with pytest.raises(ValueError):
        mgr.create("r", "t", repo, ref)
    assert not os.path.exists(os.path.join(mgr._root, "r", "t"))


def test_non_allowlisted_repo(mgr, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    run_git(other, "init", "-q")
    with pytest.raises(ValueError):
        mgr.create("r", "t", str(other.resolve()), "main")


def test_allowlist_rejects_symlink_relative_nogit(repo, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(repo)
    root = str(tmp_path / "root")
    with pytest.raises(ValueError):
        WorktreeManager([str(link)], root)
    with pytest.raises(ValueError):
        WorktreeManager(["repo"], root)
    with pytest.raises(ValueError):
        WorktreeManager([repo + "/../repo"], root)
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError):
        WorktreeManager([str(plain)], root)
    with pytest.raises(ValueError):
        WorktreeManager([], root)
    with pytest.raises(ValueError):  # root inside repo
        WorktreeManager([repo], repo + "/wt")


def test_symlinked_root_rejected(repo, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "ln").symlink_to(real)
    with pytest.raises(ValueError):
        WorktreeManager([repo], str(tmp_path / "ln"))


def test_symlink_in_root_refused(mgr, repo, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("k")
    os.symlink(victim, os.path.join(mgr._root, "evil"))
    with pytest.raises(WorktreeError):
        mgr.remove("evil", "keep")
    with pytest.raises(WorktreeError):
        mgr.create("evil", "t", repo, "main")
    assert (victim / "keep").exists()


def test_hooks_not_run_and_filters_refused(mgr, repo, tmp_path):
    marker = tmp_path / "hook-ran"
    hook = os.path.join(repo, ".git", "hooks", "post-checkout")
    open(hook, "w").write(f"#!/bin/sh\ntouch {marker}\n")
    os.chmod(hook, 0o755)
    mgr.create("r", "t", repo, "main")
    assert not marker.exists()
    run_git(repo, "config", "filter.x.smudge", f"touch {marker}")
    with pytest.raises(WorktreeError):
        mgr.create("r", "t2", repo, "main")
    assert not marker.exists()


def test_status_unknown_and_unrelated_dir(mgr):
    with pytest.raises(WorktreeError):
        mgr.status("r", "none")
    os.makedirs(os.path.join(mgr._root, "r", "plain"))
    with pytest.raises(WorktreeError):
        mgr.remove("r", "plain")


def test_concurrent_create_remove(mgr, repo):
    errs, paths = [], []

    def go(i):
        try:
            paths.append(mgr.create("r", f"t{i}", repo, "main"))
        except Exception as e:  # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and len(set(paths)) == 6
    ts = [threading.Thread(target=mgr.remove, args=("r", f"t{i}")) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not any(os.path.exists(p) for p in paths)


# --------------------------------------------------------------------------- F4 / F5 hardening
import time

import synapse.infrastructure.worktrees as wtmod


def _dirty(mgr, repo, tmp_path, name="t"):
    path = mgr.create("r", name, repo, "main")
    with open(os.path.join(path, "a.txt"), "a") as fh:
        fh.write("more changes\n")
    return path


def test_planted_clean_filter_is_refused_not_executed(mgr, repo, tmp_path):
    path = _dirty(mgr, repo, tmp_path)
    marker = tmp_path / "filter-ran"
    run_git(path, "config", "filter.x.clean", f"touch {marker}; cat")  # shared repo config
    open(os.path.join(path, ".gitattributes"), "w").write("* filter=x\n")
    for op in (mgr.status, mgr.diff):
        with pytest.raises(WorktreeError, match="filter.x.clean"):
            op("r", "t")
    with pytest.raises(WorktreeError):
        mgr.remove("r", "t")
    assert not marker.exists()


def test_gitattributes_filter_never_runs_even_if_config_check_is_bypassed(mgr, repo, tmp_path, monkeypatch):
    """Defence in depth: with the config refusal disabled (TOCTOU), tree attributes are still ignored."""
    path = _dirty(mgr, repo, tmp_path)
    marker = tmp_path / "filter-ran"
    run_git(path, "config", "filter.x.clean", f"touch {marker}; cat")
    open(os.path.join(path, ".gitattributes"), "w").write("* filter=x\n")
    monkeypatch.setattr(WorktreeManager, "_refuse_dangerous_config", lambda *a, **k: None)
    mgr.status("r", "t")
    mgr.diff("r", "t")
    assert not marker.exists()


def test_info_attributes_refused(mgr, repo, tmp_path):
    _dirty(mgr, repo, tmp_path)
    info = os.path.join(repo, ".git", "info", "attributes")
    open(info, "w").write("* filter=x\n")
    with pytest.raises(WorktreeError, match="info/attributes"):
        mgr.status("r", "t")
    with pytest.raises(WorktreeError):
        mgr.diff("r", "t")
    open(info, "w").write("# comment only\n\n")
    mgr.status("r", "t")


@pytest.mark.parametrize("key,value", [
    ("alias.x", "!touch pwned"), ("core.fsmonitor", "touch pwned"), ("core.hooksPath", "/tmp"),
    ("extensions.worktreeConfig", "true"), ("include.path", "/tmp/x"), ("diff.x.textconv", "cat"),
    ("diff.external", "cat"), ("core.sshCommand", "cat"), ("filter.y.process", "cat")])
def test_dangerous_config_refused_before_every_tree_git_call(mgr, repo, tmp_path, key, value):
    _dirty(mgr, repo, tmp_path)
    mgr.status("r", "t")  # fine before the plant
    run_git(repo, "config", key, value)
    with pytest.raises(WorktreeError, match="not allowed"):
        mgr.status("r", "t")
    with pytest.raises(WorktreeError, match="not allowed"):
        mgr.diff("r", "t")


def test_harmless_alias_and_fsmonitor_false_allowed(mgr, repo, tmp_path):
    _dirty(mgr, repo, tmp_path)
    run_git(repo, "config", "alias.st", "status -s")
    run_git(repo, "config", "core.fsmonitor", "false")
    assert not mgr.status("r", "t").clean


def test_config_worktree_file_refused(mgr, repo, tmp_path):
    path = _dirty(mgr, repo, tmp_path)
    gitdir = open(os.path.join(path, ".git")).read().split("gitdir: ", 1)[1].strip()
    open(os.path.join(gitdir, "config.worktree"), "w").write("[core]\n\tfsmonitor = touch pwned\n")
    with pytest.raises(WorktreeError, match="config.worktree"):
        mgr.status("r", "t")


def test_fsmonitor_not_executed_even_if_check_bypassed(mgr, repo, tmp_path, monkeypatch):
    _dirty(mgr, repo, tmp_path)
    marker = tmp_path / "fsm-ran"
    script = tmp_path / "fsm.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    run_git(repo, "config", "core.fsmonitor", str(script))
    monkeypatch.setattr(WorktreeManager, "_refuse_dangerous_config", lambda *a, **k: None)
    mgr.status("r", "t")
    assert not marker.exists()


def _gone(pid):
    try:
        return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


def test_git_deadline_is_wall_clock_and_kills_the_group(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    pidfile = tmp_path / "pids"
    (fake / "git").write_text(f"#!/bin/sh\n(sleep 600 &\n echo $! >> {pidfile})\necho $$ >> {pidfile}\nsleep 600\n")
    (fake / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    monkeypatch.setattr(wtmod, "_GIT_TIMEOUT", 1.0)
    t0 = time.monotonic()
    with pytest.raises(WorktreeError, match="timed out"):
        wtmod._git(str(tmp_path), ["status"])
    assert time.monotonic() - t0 < 4
    time.sleep(0.3)
    pids = [int(p) for p in pidfile.read_text().split()]
    assert pids and all(_gone(p) for p in pids)


def test_sleeping_filter_times_out_instead_of_wedging(mgr, repo, tmp_path, monkeypatch):
    """Review exp3. info/attributes is not covered by the attr-source pin, so with the refusal
    bypassed (TOCTOU) a planted sleeping filter runs, but the deadline still bounds git."""
    _dirty(mgr, repo, tmp_path)
    run_git(repo, "config", "filter.x.clean", "sleep 30; cat")
    open(os.path.join(repo, ".git", "info", "attributes"), "w").write("* filter=x\n")
    monkeypatch.setattr(WorktreeManager, "_refuse_dangerous_config", lambda *a, **k: None)
    monkeypatch.setattr(wtmod, "_GIT_TIMEOUT", 1.0)
    t0 = time.monotonic()
    try:
        mgr.status("r", "t")
    except WorktreeError:
        pass
    assert time.monotonic() - t0 < 8


# --- multi-repository allowlist (M6) ---------------------------------------
from synapse.infrastructure.worktrees import RepoNotAllowlistedError


def _mkrepo(path):
    path.mkdir()
    run_git(path, "init", "-q", "-b", "main")
    (path / "f.txt").write_text(path.name + "\n")
    run_git(path, "add", "f.txt")
    run_git(path, "commit", "-q", "-m", "init")
    return str(path.resolve())


@pytest.fixture
def two(tmp_path):
    a, b = _mkrepo(tmp_path / "ra"), _mkrepo(tmp_path / "rb")
    return a, b, WorktreeManager([a, b], str((tmp_path / "wt").resolve()))


def test_non_allowlisted_is_domain_error_for_create_and_check_base(two, tmp_path):
    c = _mkrepo(tmp_path / "rc")
    _, _, m = two
    with pytest.raises(RepoNotAllowlistedError):
        m.create("r", "t", c, "main")
    with pytest.raises(RepoNotAllowlistedError):
        m.check_base(c, "main")
    with pytest.raises(RepoNotAllowlistedError):
        m.create("r", "t", 5, "main")  # type: ignore[arg-type]


def test_empty_or_missing_allowlist_fails_closed(tmp_path):
    with pytest.raises(ValueError):
        WorktreeManager([], str(tmp_path / "wt"))
    with pytest.raises(ValueError):
        WorktreeManager(iter(()), str(tmp_path / "wt"))


@pytest.mark.parametrize("how", ["symlink", "dotdot", "relative", "trailing", "child", "empty"])
def test_call_time_path_escapes_are_refused(two, tmp_path, how):
    a, b, m = two
    if how == "symlink":
        (tmp_path / "ln").symlink_to(a)
        cand = str(tmp_path / "ln")
    elif how == "dotdot":
        cand = a + "/../ra"
    elif how == "relative":
        cand = os.path.relpath(a)
    elif how == "trailing":
        cand = a + "/"
    elif how == "child":
        cand = os.path.join(a, "sub")
        os.mkdir(cand)
    else:
        cand = ""
    with pytest.raises(RepoNotAllowlistedError):
        m.create("r", "t", cand, "main")
    assert not os.path.exists(os.path.join(str(tmp_path / "wt"), "r"))


def test_symlink_swapped_over_allowlisted_repo_is_refused(two, tmp_path):
    a, b, m = two
    moved = tmp_path / "moved"
    os.rename(a, moved)
    os.symlink(b, a)  # allowlisted path now points at another repo
    with pytest.raises(ValueError):
        m.create("r", "t", a, "main")
    assert not os.path.exists(os.path.join(str(tmp_path / "wt"), "r", "t"))


def test_allowlist_entry_symlink_to_other_repo_rejected(tmp_path):
    a = _mkrepo(tmp_path / "ra")
    (tmp_path / "ln").symlink_to(a)
    with pytest.raises(ValueError):
        WorktreeManager([str(tmp_path / "ln")], str(tmp_path / "wt"))


def test_nested_allowlist_entries_rejected(tmp_path):
    outer = tmp_path / "outer"
    outer_s = _mkrepo(outer)
    inner = _mkrepo(outer / "inner")
    with pytest.raises(ValueError):
        WorktreeManager([outer_s, inner], str(tmp_path / "wt"))


def test_per_repo_isolation(two):
    a, b, m = two
    pa, pb = m.create("r1", "t", a, "main"), m.create("r2", "t", b, "main")
    assert pa != pb
    assert m.worktree_repo("r1", "t") == a and m.worktree_repo("r2", "t") == b
    (open(os.path.join(pa, "x.txt"), "w")).write("x")
    assert m.status("r1", "t").changed_paths == 1
    assert m.status("r2", "t").clean
    assert "x.txt" not in run_git(b, "status", "--porcelain")
    # branches exist only in the owning repo
    assert run_git(a, "branch", "--list", "synapse/r1/t").strip()
    assert not run_git(b, "branch", "--list", "synapse/r1/t").strip()
    m.remove("r1", "t", force=True)
    assert m.worktree_repo("r2", "t") == b


def test_same_run_task_in_second_repo_is_refused_not_shared(two):
    a, b, m = two
    m.create("r", "t", a, "main")
    with pytest.raises(WorktreeError):
        m.create("r", "t", b, "main")
    assert m.worktree_repo("r", "t") == a


def test_worktree_whose_repo_left_allowlist_is_refused(two, tmp_path):
    a, b, m = two
    m.create("r", "t", a, "main")
    m2 = WorktreeManager([b], str(tmp_path / "wt"))
    with pytest.raises(WorktreeError):
        m2.status("r", "t")
    with pytest.raises(WorktreeError):
        m2.remove("r", "t", force=True)


def test_commit_lands_on_task_branch_only(mgr, repo):
    p = mgr.create("run1", "task1", repo, "main")
    main_before = run_git(repo, "rev-parse", "main").strip()
    open(os.path.join(p, "a.txt"), "w").write("changed\n")
    open(os.path.join(p, "new.txt"), "w").write("new\n")
    out = mgr.commit("run1", "task1", "fix things")
    assert out["branch"] == "synapse/run1/task1" and out["paths"] == 2 and out["head"] != out["head_before"]
    assert mgr.status("run1", "task1").clean
    assert run_git(repo, "rev-parse", "main").strip() == main_before  # owner's branch untouched
    assert open(os.path.join(repo, "a.txt")).read() == "hi\n"
    assert run_git(repo, "log", "-1", "--format=%s", out["branch"]).strip() == "fix things"
    assert run_git(repo, "log", "-1", "--format=%an", out["branch"]).strip() == "synapse"


def test_commit_refuses_clean_bad_message_and_missing(mgr, repo):
    p = mgr.create("run1", "task1", repo, "main")
    with pytest.raises(WorktreeError, match="nothing to commit"):
        mgr.commit("run1", "task1", "x")
    open(os.path.join(p, "a.txt"), "w").write("c\n")
    for bad in ("", "  ", "a" * 2001, "x\x00y", None):
        with pytest.raises(ValueError):
            mgr.commit("run1", "task1", bad)
    with pytest.raises(WorktreeError, match="does not exist"):
        mgr.commit("nope", "task1", "x")


def test_commit_ignores_hooks_and_refuses_gitlinks(mgr, repo):
    p = mgr.create("run1", "task1", repo, "main")
    hooks = os.path.join(repo, ".git", "hooks")
    marker = os.path.join(repo, "hook-ran")
    with open(os.path.join(hooks, "pre-commit"), "w") as fh:
        fh.write(f"#!/bin/sh\ntouch {marker}\n")
    os.chmod(os.path.join(hooks, "pre-commit"), 0o755)
    open(os.path.join(p, "a.txt"), "w").write("c\n")
    mgr.commit("run1", "task1", "x")
    assert not os.path.exists(marker)
    nested = os.path.join(p, "inner")
    os.mkdir(nested)
    run_git(nested, "init", "-q", "-b", "main")
    open(os.path.join(nested, "f"), "w").write("1")
    run_git(nested, "add", "f")
    run_git(nested, "commit", "-q", "-m", "i")
    head = mgr.status("run1", "task1").head
    with pytest.raises(WorktreeError, match="gitlink"):
        mgr.commit("run1", "task1", "y")
    assert mgr.status("run1", "task1").head == head  # nothing committed, index reset
    assert run_git(p, "diff", "--cached", "--name-only").strip() == ""


def _nested_repo(parent, name="inner"):
    nested = os.path.join(parent, name)
    os.mkdir(nested)
    run_git(nested, "init", "-q", "-b", "main")
    open(os.path.join(nested, "f"), "w").write("1")
    run_git(nested, "add", "f")
    run_git(nested, "commit", "-q", "-m", "i")


def _assert_nothing_committed(mgr, p, head):
    assert mgr.status("run1", "task1").head == head
    assert run_git(p, "diff", "--cached", "--name-only").strip() == ""


def test_commit_refuses_a_gitlink_hidden_by_gitmodules_ignore_all(mgr, repo):
    """Reviewer repro: the agent-written .gitmodules `ignore = all` must not hide a gitlink from the check."""
    p = mgr.create("run1", "task1", repo, "main")
    head = mgr.status("run1", "task1").head
    _nested_repo(p)
    open(os.path.join(p, ".gitmodules"), "w").write('[submodule "inner"]\n\tpath = inner\n\turl = ./x\n\tignore = all\n')
    with pytest.raises(WorktreeError):
        mgr.commit("run1", "task1", "x")
    _assert_nothing_committed(mgr, p, head)


def test_commit_refuses_a_gitmodules_change_alone(mgr, repo):
    p = mgr.create("run1", "task1", repo, "main")
    head = mgr.status("run1", "task1").head
    open(os.path.join(p, ".gitmodules"), "w").write("[submodule \"x\"]\n\tpath = x\n\turl = ./x\n")
    with pytest.raises(WorktreeError, match="gitmodules"):
        mgr.commit("run1", "task1", "x")
    _assert_nothing_committed(mgr, p, head)


def test_commit_path_cap_counts_files_not_folded_directories_and_fails_closed_on_truncation(mgr, repo):
    """Reviewer repro: one untracked dir with many long-named files plus a nested repo must be refused, not
    slip through because status folds the directory or the listing is truncated."""
    p = mgr.create("run1", "task1", repo, "main")
    head = mgr.status("run1", "task1").head
    many = os.path.join(p, "a")
    os.mkdir(many)
    for i in range(1500):
        open(os.path.join(many, ("%04d" % i) + "x" * 200), "w").close()
    _nested_repo(p, "zz")
    with pytest.raises(WorktreeError, match="too many"):
        mgr.commit("run1", "task1", "x")
    _assert_nothing_committed(mgr, p, head)


def plant_commondir(repo: str, marker) -> None:
    """Security review repro: .git/commondir points git at another dir whose config defines a filter
    and whose info/attributes applies it, while <repo>/.git/config itself stays clean."""
    import shutil
    common = os.path.join(os.path.dirname(repo), "evil-common")
    shutil.copytree(os.path.join(repo, ".git"), common)
    cmd = "touch '%s'; cat" % marker
    run_git(repo, "config", "--file", os.path.join(common, "config"), "filter.x.clean", cmd)
    run_git(repo, "config", "--file", os.path.join(common, "config"), "filter.x.smudge", cmd)
    os.makedirs(os.path.join(common, "info"), exist_ok=True)
    with open(os.path.join(common, "info", "attributes"), "w") as fh:
        fh.write("* filter=x\n")
    with open(os.path.join(repo, ".git", "commondir"), "w") as fh:
        fh.write(common + "\n")
    future = os.stat(os.path.join(repo, "a.txt")).st_mtime + 100
    os.utime(os.path.join(repo, "a.txt"), (future, future))  # stat-dirty: status re-hashes via the filter


def test_commondir_fixture_is_really_hostile_without_hardening(repo, tmp_path):
    marker = tmp_path / "marker"
    plant_commondir(repo, marker)
    subprocess.run(["git", "status"], cwd=repo, env=ENV, capture_output=True)
    assert marker.exists()


def test_commondir_redirect_is_refused_and_no_filter_runs(mgr, repo, tmp_path):
    marker = tmp_path / "marker"
    plant_commondir(repo, marker)  # planted after the daemon started
    with pytest.raises((ValueError, WorktreeError), match="commondir"):
        mgr.check_base(repo, "main")
    with pytest.raises((ValueError, WorktreeError), match="commondir"):
        mgr.create("run1", "task1", repo, "main")
    with pytest.raises(WorktreeError, match="commondir"):  # the config guard refuses it on its own too
        mgr._refuse_dangerous_config(repo)
    with pytest.raises(ValueError, match="commondir"):
        WorktreeManager([repo], str((tmp_path / "wt2").resolve()))
    assert not marker.exists()
    assert not os.path.exists(os.path.join(mgr._root, "run1", "task1"))


def add_config_hooks(repo: str, marker) -> None:
    """git 2.54+ config-defined hooks: not disabled by core.hooksPath=/dev/null."""
    for event in ("post-checkout", "reference-transaction", "post-index-change", "post-commit"):
        run_git(repo, "config", "--add", "hook.pwn.event", event)
    run_git(repo, "config", "hook.pwn.command", "touch '%s'" % marker)


def test_config_hooks_fixture_fires_under_plain_git(repo, tmp_path):
    marker = tmp_path / "marker"
    add_config_hooks(repo, marker)
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "worktree", "add", "-q", str(tmp_path / "plain")],
                   cwd=repo, env=ENV, capture_output=True)
    if not marker.exists():
        pytest.skip("this git has no config-based hooks")


def test_config_hooks_are_refused_in_create_and_force_remove(mgr, repo, tmp_path):
    marker = tmp_path / "marker"
    mgr.create("run1", "task1", repo, "main")
    add_config_hooks(repo, marker)  # planted after the worktree exists
    with pytest.raises(WorktreeError, match="hook.pwn"):
        mgr.check_base(repo, "main")
    with pytest.raises(WorktreeError, match="hook.pwn"):
        mgr.create("run2", "task2", repo, "main")
    with pytest.raises(WorktreeError, match="hook.pwn"):
        mgr.remove("run1", "task1", force=True)
    assert not marker.exists()


def test_agent_made_submodule_config_never_runs_in_host_git(mgr, repo, tmp_path):
    """A gitlink in the base commit gives an empty sub/ in the worktree; the agent turns it into a repo
    whose own config + info/attributes define a clean filter. Host status/diff/commit/remove must not
    recurse into it."""
    marker = tmp_path / "marker"
    sha = run_git(repo, "rev-parse", "HEAD").strip()
    run_git(repo, "update-index", "--add", "--cacheinfo", "160000,%s,sub" % sha)
    run_git(repo, "commit", "-q", "-m", "gitlink")
    path = mgr.create("run1", "task1", repo, "main")
    sub = os.path.join(path, "sub")
    run_git(sub, "init", "-q", "-b", "main")
    with open(os.path.join(sub, "a.txt"), "w") as fh:
        fh.write("x\n")
    run_git(sub, "add", "a.txt")
    run_git(sub, "commit", "-q", "-m", "s")
    run_git(sub, "config", "filter.pwn.clean", "touch '%s'; cat" % marker)
    with open(os.path.join(sub, ".git", "info", "attributes"), "w") as fh:
        fh.write("a.txt filter=pwn\n")
    future = os.stat(os.path.join(sub, "a.txt")).st_mtime + 100
    os.utime(os.path.join(sub, "a.txt"), (future, future))
    control = subprocess.run(["git", "status"], cwd=path, env=ENV, capture_output=True)
    assert control.returncode == 0 and marker.exists()  # the fixture really is hostile under plain git
    marker.unlink()
    mgr.status("run1", "task1")
    mgr.diff("run1", "task1")
    mgr.diff_since("run1", "task1", sha)
    try:
        mgr.commit("run1", "task1", "agent work")
    except WorktreeError:
        pass  # a gitlink change is refused anyway; what matters is that no filter ran
    mgr.remove("run1", "task1", force=True)
    assert not marker.exists()


def test_worktree_commondir_must_point_at_its_repository(mgr, repo, tmp_path):
    path = mgr.create("run1", "task1", repo, "main")
    meta = os.path.join(repo, ".git", "worktrees", "task1")
    with open(os.path.join(meta, "commondir"), "w") as fh:
        fh.write(str(tmp_path) + "\n")
    with pytest.raises(WorktreeError, match="commondir"):
        mgr.status("run1", "task1")
    assert os.path.isdir(path)


def test_check_base_keeps_value_error_for_missing_dot_git_and_idempotent_create_is_guarded(mgr, repo, tmp_path):
    marker = tmp_path / "marker"
    mgr.create("run1", "task1", repo, "main")
    add_config_hooks(repo, marker)
    with pytest.raises(WorktreeError, match="hook.pwn"):
        mgr.create("run1", "task1", repo, "main")  # same (run, task): idempotent path is guarded too
    assert not marker.exists()
    os.rename(os.path.join(repo, ".git"), os.path.join(repo, "moved-git"))
    with pytest.raises(ValueError, match="real .git directory"):
        mgr.check_base(repo, "main")
