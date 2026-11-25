"""ADR 026 slice 1: multi-repository runs (registry objects, pinned baselines, per-attempt worktrees, XML-only host
commit, protected paths, ordered locks, crash windows). Real git in tmp_path and a fake CLI only: no real claude or
codex, no network, nothing outside tmp_path is read.
"""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from synapse.daemon.agent_config import AgentConfigError, RepoEntry, load_agent_config, registry_sha256
from synapse.daemon.agent_runtime import AgentRuntime
from synapse.daemon.agent_service import AgentRunService, worktree_checker
from synapse.daemon.multi_repo import AttemptWorkspaces, RepoRegistry, resolve_commit_intents
from synapse.infrastructure import worktrees as wtmod
from synapse.infrastructure.spawn_journal import SpawnJournal
from synapse.infrastructure.worktrees import (
    AttemptCreateError, AttemptRepo, CommitRefused, WorktreeError, WorktreeManager, check_protected_patterns,
    check_staged_paths, matches_protected)
from synapse.api.agent_commands import AgentRunError
from synapse.orchestration import agent_engine
from synapse.persistence import agent_tasks as at
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.repo_bindings import RepoBinding, check_workspace_specs
from synapse.providers.claude_cli import ClaudeCliProfile, validate_workspace
from synapse.providers.sandbox_launcher import SandboxUnavailable, build_bwrap_argv

ROOT = Path(__file__).resolve().parents[1]
GIT_ENV = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
REPO_IDS = ("portfolio", "product-config", "tenant-specification")

# The fake agent: the prompt is JSON {"mode", "writes": {repo_id: {relpath: text}}, "links": {repo_id: {rel: target}}}.
# cwd is the attempt directory; each declared repository is the subdirectory named by its repo_id.
FAKE = '''#!{py}
import json, os, sys, time
spec = json.loads(sys.stdin.read())
with open({log!r}, "a") as fh:
    fh.write(json.dumps({{"cwd": os.getcwd(), "entries": sorted(os.listdir("."))}}) + "\\n")
def emit(o): sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
emit({{"type": "system", "subtype": "init"}})
for repo, files in spec.get("writes", {{}}).items():
    for rel, text in files.items():
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
for repo, links in spec.get("links", {{}}).items():
    for rel, target in links.items():
        os.symlink(target, os.path.join(repo, rel))
mode = spec.get("mode", "ok")
if mode == "hang":
    time.sleep(60)
if mode == "fail":
    sys.exit(3)
emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "done"}}]}}}})
emit({{"type": "result", "subtype": "success", "is_error": False, "usage": {{"input_tokens": 1, "output_tokens": 1}}}})
'''


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True).stdout


def make_repo(path: Path) -> str:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "config.xml").write_text("<config/>\n")
    (path / "README.md").write_text("readme\n")
    (path / ".gitignore").write_text("*.log\n")
    (path / ".github").mkdir()
    (path / ".github" / "ci.xml").write_text("<ci/>\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return str(path.resolve())


def head(repo, ref="HEAD"):
    return git(repo, "rev-parse", ref).strip()


# --------------------------------------------------------------------------------------------- pure policy checks
@pytest.mark.parametrize("path,patterns,hit", [
    (".github/workflows/x.yml", [".github"], True),
    (".github/ci.xml", [".github/**"], True),
    ("CODEOWNERS", ["**/CODEOWNERS"], True),
    ("a/b/CODEOWNERS", ["**/CODEOWNERS"], True),
    ("a/CODEOWNERS", ["CODEOWNERS"], False),  # anchored: a bare name protects the root entry only
    ("cfg/secret/a.xml", ["cfg/*/a.xml"], True),
    ("cfg/x/y/a.xml", ["cfg/*/a.xml"], False),
    ("docs/a.xml", [], False),
    ("tenants/t1/limits.xml", ["tenants/**/limits.xml"], True),
    ("tenants/limits.xml", ["tenants/**/limits.xml"], True),
])
def test_protected_matching(path, patterns, hit):
    assert matches_protected(path, check_protected_patterns(patterns)) is hit


@pytest.mark.parametrize("bad", [["/abs"], ["a/../b"], ["a//b"], [""], ["[ab]"], ["!x"], ["a**b"], ["x\\y"], "notalist",
                                 ["dup", "dup"], ["x" * 300], [" lead"]])
def test_protected_patterns_are_validated(bad):
    with pytest.raises(ValueError):
        check_protected_patterns(bad)


def rec(name, old="100644", new="100644"):
    return ([":" + old, new, "0" * 40, "1" * 40, "M"], name)


@pytest.mark.parametrize("records,reason", [
    ([rec("a/b.txt")], "path_not_editable"),
    ([rec("a.xml"), rec("Makefile")], "path_not_editable"),
    ([rec("a.XML")], "path_not_editable"),  # **/*.xml is case sensitive
    ([rec(".gitmodules")], "path_not_editable"),
    ([rec("../escape.xml")], "path_not_editable"),
    ([rec("/abs.xml")], "path_not_editable"),
    ([rec("-opt.xml")], "path_not_editable"),
    ([rec("a//b.xml")], "path_not_editable"),
    ([rec("a\tb.xml")], "path_not_editable"),
    ([rec(".git/config.xml")], "path_not_editable"),
    ([rec(".github/ci.xml")], "path_not_allowed"),
    ([rec("link.xml", "000000", "120000")], "mode_not_allowed"),
    ([rec("run.xml", "100644", "100755")], "mode_not_allowed"),
    ([rec("sub.xml", "000000", "160000")], "mode_not_allowed"),
])
def test_staged_path_policy_refuses(records, reason):
    with pytest.raises(CommitRefused) as info:
        check_staged_paths(records, (".github/**",))
    assert info.value.reason == reason and info.value.count >= 1


def test_staged_path_policy_accepts_xml_edits_adds_and_deletes():
    check_staged_paths([rec("config.xml"), rec("deep/new.xml", "000000", "100644"),
                        rec("gone.xml", "100644", "000000"), rec("old-link.xml", "120000", "000000")], (".github/**",))


@pytest.mark.parametrize("blob", [b"<a/>", b"<?xml version='1.0'?>\n<a x='1'><b>t</b><!-- c --><![CDATA[<x>]]></a>",
                                  "<?xml version='1.0' encoding='ISO-8859-1'?><a>\xe6</a>".encode("latin-1")])
def test_xml_well_formed_samples(blob):
    from synapse.application.xml_wellformed import check_xml
    check_xml(blob)


@pytest.mark.parametrize("blob,reason", [
    (b"<a>", "malformed"), (b"", "malformed"), (b"not xml", "malformed"), (b"<a></b>", "malformed"),
    (b"<!DOCTYPE a [<!ENTITY x 'y'>]><a>&x;</a>", "declaration_not_allowed"),
    (b"<!ENTITY x SYSTEM 'file:///etc/passwd'>", "declaration_not_allowed"),
    (b"<a><!ELEMENT b ANY></a>", "declaration_not_allowed"),
    (b"<a>" * 300 + b"</a>" * 300, "too_deep"),
    (b"<a>" + b"x" * (8 * 1024 * 1024) + b"</a>", "too_large"),
    ("<a/>".encode("utf-16"), "encoding_not_allowed"),
    (b"<?xml version='1.0' encoding='UTF-8'?><a>\xff\xfe</a>", "malformed"),
])
def test_xml_refusals(blob, reason):
    from synapse.application.xml_wellformed import XmlRefused, check_xml
    with pytest.raises(XmlRefused) as info:
        check_xml(blob)
    assert info.value.reason == reason


def test_ref_guard_only_allows_the_synapse_namespace():
    for bad in ("main", "refs/heads/main", "synapse/../main", ""):
        with pytest.raises(WorktreeError):
            WorktreeManager._guard_ref(bad)
    assert WorktreeManager._guard_ref("synapse/r/t/1") == "synapse/r/t/1"


# ------------------------------------------------------------------------------------------------ worktree manager
@pytest.fixture
def repos(tmp_path):
    return {rid: make_repo(tmp_path / "repos" / rid) for rid in REPO_IDS}


@pytest.fixture
def mgr(repos, tmp_path):
    return WorktreeManager(list(repos.values()), str((tmp_path / "wt").resolve()))


def attempt_repos(repos, access=None):
    access = access or {}
    return [AttemptRepo(rid, path, head(path), access.get(rid, "write")) for rid, path in repos.items()]


def test_create_attempt_layout_branches_and_read_only_detached(mgr, repos):
    wanted = attempt_repos(repos, {"tenant-specification": "read"})
    paths = mgr.create_attempt("run1", "task1", 1, list(reversed(wanted)))
    adir = mgr.attempt_dir("run1", "task1", 1)
    assert adir == os.path.join(mgr._root, "run1", "task1", "1")
    assert paths == {rid: os.path.join(adir, rid) for rid in REPO_IDS}
    for rid in ("portfolio", "product-config"):
        assert git(paths[rid], "symbolic-ref", "--short", "HEAD").strip() == "synapse/run1/task1/1"
        assert head(paths[rid]) == head(repos[rid])
    ro = paths["tenant-specification"]
    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=ro, env=GIT_ENV).returncode != 0  # detached
    assert head(ro) == head(repos["tenant-specification"])
    assert "synapse/run1/task1/1" not in git(repos["tenant-specification"], "branch", "--list")  # no ref written
    for rid in REPO_IDS:  # the owner's checkout is untouched
        assert git(repos[rid], "symbolic-ref", "--short", "HEAD").strip() == "main"


def test_create_attempt_is_all_or_none(mgr, repos, tmp_path):
    wanted = attempt_repos(repos)
    # product-config's baseline does not exist in that repository: the second create fails
    bad = [r if r.repo_id != "product-config" else AttemptRepo(r.repo_id, r.repo_path, "f" * 40, "write") for r in wanted]
    with pytest.raises(AttemptCreateError) as info:
        mgr.create_attempt("run1", "task1", 1, bad)
    assert info.value.repo_id == "product-config"
    assert not os.path.exists(mgr.attempt_dir("run1", "task1", 1))
    for path in repos.values():
        assert "synapse/" not in git(path, "branch", "--list")
        assert git(path, "worktree", "list").count("\n") == 1  # only the main checkout


def test_create_attempt_never_reuses_a_leftover_branch(mgr, repos):
    git(repos["portfolio"], "branch", "synapse/run1/task1/1")  # e.g. left by a crash: never adopted
    with pytest.raises(AttemptCreateError) as info:
        mgr.create_attempt("run1", "task1", 1, attempt_repos(repos))
    assert info.value.repo_id == "portfolio"
    assert not os.path.exists(mgr.attempt_dir("run1", "task1", 1))
    # the pre-existing branch is not deleted by the rollback? It is in the Synapse namespace and the rollback only
    # deletes branches of repositories it created a worktree in; portfolio failed before its worktree existed.
    assert "synapse/run1/task1/1" in git(repos["portfolio"], "branch", "--list")


def test_create_attempt_refuses_a_symlinked_task_directory(mgr, repos, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.makedirs(os.path.join(mgr._root, "run1"), mode=0o700)
    os.symlink(outside, os.path.join(mgr._root, "run1", "task1"))
    with pytest.raises(AttemptCreateError):
        mgr.create_attempt("run1", "task1", 1, attempt_repos(repos))
    assert list(outside.iterdir()) == []


def test_create_attempt_refuses_a_repo_off_the_allowlist(mgr, repos, tmp_path):
    other = make_repo(tmp_path / "other")
    with pytest.raises(ValueError):
        mgr.create_attempt("run1", "task1", 1, [AttemptRepo("other", other, head(other), "write")])


def test_locks_are_taken_in_repo_id_order(mgr, repos, monkeypatch):
    order = []
    real = WorktreeManager._lock

    def spy(self, repo):
        order.append(repo)
        return real(self, repo)
    monkeypatch.setattr(WorktreeManager, "_lock", spy)
    mgr.create_attempt("run1", "task1", 1, list(reversed(attempt_repos(repos))))
    assert order == [repos[rid] for rid in sorted(REPO_IDS)]


def test_concurrent_attempts_over_overlapping_repos_do_not_deadlock(mgr, repos):
    errors = []

    def go(task, ids):
        try:
            mgr.create_attempt("run1", task, 1, [r for r in attempt_repos(repos) if r.repo_id in ids][::-1])
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)
    threads = [threading.Thread(target=go, args=("t%d" % i, ids))
               for i, ids in enumerate([REPO_IDS, REPO_IDS[::-1], REPO_IDS[1:], REPO_IDS[:2]] * 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads) and errors == []


def test_commit_xml_only_and_protected_paths(mgr, repos):
    wanted = attempt_repos(repos)
    paths = mgr.create_attempt("r", "t", 1, wanted)
    base = {r.repo_id: r.baseline_sha for r in wanted}
    p = paths["portfolio"]
    Path(p, "config.xml").write_text("<config changed='1'/>\n")
    os.makedirs(os.path.join(p, "new"), exist_ok=True)
    Path(p, "new", "item.xml").write_text("<item/>\n")
    out = mgr.commit_attempt("r", "t", 1, "portfolio", repos["portfolio"], "msg", expected_head=base["portfolio"],
                             protected=(".github/**",))
    assert out["committed"] and out["paths"] == 2 and out["head_before"] == base["portfolio"]
    assert git(repos["portfolio"], "rev-parse", "synapse/r/t/1").strip() == out["head"]
    assert git(repos["portfolio"], "rev-parse", out["head"] + "^").strip() == base["portfolio"]
    assert head(repos["portfolio"]) == base["portfolio"]  # main did not move, nothing pushed (no remote at all)

    # A non-XML file anywhere refuses the whole commit; the tree is left exactly as the agent left it.
    q = paths["product-config"]
    Path(q, "config.xml").write_text("<ok/>\n")
    Path(q, "run.sh").write_text("echo hi\n")
    with pytest.raises(CommitRefused) as info:
        mgr.commit_attempt("r", "t", 1, "product-config", repos["product-config"], "m",
                           expected_head=base["product-config"])
    assert info.value.reason == "path_not_editable" and info.value.count == 1
    assert Path(q, "run.sh").exists() and git(q, "diff", "--cached", "--name-only") == ""
    assert head(q) == base["product-config"]

    # A protected path (even an XML one) refuses the commit.
    s = paths["tenant-specification"]
    Path(s, ".github", "ci.xml").write_text("<ci evil='1'/>\n")
    with pytest.raises(CommitRefused) as info:
        mgr.commit_attempt("r", "t", 1, "tenant-specification", repos["tenant-specification"], "m",
                           expected_head=base["tenant-specification"], protected=(".github/**",))
    assert info.value.reason == "path_not_allowed"
    assert head(s) == base["tenant-specification"]


def test_commit_refuses_ignored_files_malformed_xml_and_executables(mgr, repos):
    wanted = attempt_repos(repos)
    paths = mgr.create_attempt("r", "t", 1, wanted)
    base = {r.repo_id: r.baseline_sha for r in wanted}
    p = paths["portfolio"]
    Path(p, "config.xml").write_text("<ok/>")
    Path(p, "hidden.log").write_text("a .gitignore must never hide a file from the rule")
    with pytest.raises(CommitRefused) as info:
        mgr.commit_attempt("r", "t", 1, "portfolio", repos["portfolio"], "m", expected_head=base["portfolio"])
    assert info.value.reason == "ignored_path_present"
    os.unlink(os.path.join(p, "hidden.log"))
    Path(p, "config.xml").write_text("<broken>")
    with pytest.raises(CommitRefused) as info:
        mgr.commit_attempt("r", "t", 1, "portfolio", repos["portfolio"], "m", expected_head=base["portfolio"])
    assert info.value.reason == "xml_not_well_formed"
    Path(p, "config.xml").write_text("<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/hostname'>]><x>&e;</x>")
    with pytest.raises(CommitRefused) as info:
        mgr.commit_attempt("r", "t", 1, "portfolio", repos["portfolio"], "m", expected_head=base["portfolio"])
    assert info.value.reason == "xml_not_well_formed"
    Path(p, "config.xml").write_text("<ok/>")
    os.chmod(os.path.join(p, "config.xml"), 0o755)
    with pytest.raises(CommitRefused) as info:
        mgr.commit_attempt("r", "t", 1, "portfolio", repos["portfolio"], "m", expected_head=base["portfolio"])
    assert info.value.reason == "mode_not_allowed"
    assert head(p) == base["portfolio"]


def test_commit_refuses_symlinks_even_named_xml(mgr, repos, tmp_path):
    wanted = attempt_repos(repos)
    paths = mgr.create_attempt("r", "t", 1, wanted)
    p = paths["portfolio"]
    os.symlink("/etc/hostname", os.path.join(p, "evil.xml"))
    with pytest.raises(CommitRefused) as info:
        mgr.commit_attempt("r", "t", 1, "portfolio", repos["portfolio"], "m", expected_head=wanted[0].baseline_sha)
    assert info.value.reason == "mode_not_allowed"
    # a symlinked directory is staged as the link itself (git never follows it): refused as well
    os.unlink(os.path.join(p, "evil.xml"))
    os.symlink(str(tmp_path), os.path.join(p, "dir.xml"))
    with pytest.raises(CommitRefused) as info:
        mgr.commit_attempt("r", "t", 1, "portfolio", repos["portfolio"], "m", expected_head=wanted[0].baseline_sha)
    assert info.value.reason == "mode_not_allowed"


def test_commit_with_no_change_keeps_the_baseline_and_moved_branch_is_refused(mgr, repos):
    wanted = attempt_repos(repos)
    paths = mgr.create_attempt("r", "t", 1, wanted)
    base = wanted[0].baseline_sha
    out = mgr.commit_attempt("r", "t", 1, "portfolio", repos["portfolio"], "m", expected_head=base)
    assert out == {"head_before": base, "head": base, "paths": 0, "committed": False}
    Path(paths["portfolio"], "config.xml").write_text("<x/>\n")
    with pytest.raises(CommitRefused) as info:
        mgr.commit_attempt("r", "t", 1, "portfolio", repos["portfolio"], "m", expected_head="e" * 40)
    assert info.value.reason == "branch_moved"


def test_commit_refuses_a_worktree_of_another_repository(mgr, repos):
    wanted = attempt_repos(repos)
    mgr.create_attempt("r", "t", 1, wanted)
    with pytest.raises(WorktreeError):
        mgr.commit_attempt("r", "t", 1, "portfolio", repos["product-config"], "m", expected_head=wanted[0].baseline_sha)


# ------------------------------------------------------------------------------------------------ sandbox mounts
def test_bwrap_mounts_only_declared_worktrees_and_read_access_read_only(tmp_path):
    adir = str(tmp_path / "wt" / "r" / "t" / "1")
    w, r = adir + "/portfolio", adir + "/tenant-specification"
    argv = build_bwrap_argv("/usr/bin/bwrap", worktree=adir, scratch=str(tmp_path / "s"), executable="/usr/bin/true",
                            inner=["/usr/bin/true"], worktree_binds=[(w, True), (r, False)])
    pairs = [(argv[i], argv[i + 1], argv[i + 2]) for i in range(len(argv) - 2)]
    assert ("--bind", w, w) in pairs and ("--ro-bind", r, r) in pairs
    assert ("--bind", r, r) not in pairs
    assert ("--bind", adir, adir) not in pairs and ("--ro-bind", adir, adir) not in pairs  # the attempt dir itself
    assert argv[argv.index("--chdir") + 1] == adir
    for bad in ([(adir, True)], [(str(tmp_path / "elsewhere"), True)], [(w, True), (w, False)]):
        with pytest.raises(SandboxUnavailable):
            build_bwrap_argv("/usr/bin/bwrap", worktree=adir, scratch=str(tmp_path / "s"),
                             executable="/usr/bin/true", inner=["/usr/bin/true"], worktree_binds=bad)
    with pytest.raises(SandboxUnavailable):  # a declared worktree may not expose a forbidden path
        build_bwrap_argv("/usr/bin/bwrap", worktree=adir, scratch=str(tmp_path / "s"), executable="/usr/bin/true",
                         inner=["/usr/bin/true"], worktree_binds=[(w, True)], forbidden=[w + "/secret"])


def test_validate_workspace_requires_direct_child_worktrees(mgr, repos, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    paths = mgr.create_attempt("r", "t", 1, attempt_repos(repos, {"tenant-specification": "read"}))
    adir = mgr.attempt_dir("r", "t", 1)
    entries = [{"path": paths[rid], "access": "read" if rid == "tenant-specification" else "write"} for rid in REPO_IDS]
    root, binds = validate_workspace(adir, entries, str(state))
    assert root == adir and dict(binds) == {paths["portfolio"]: True, paths["product-config"]: True,
                                            paths["tenant-specification"]: False}
    for bad in ([{"path": repos["portfolio"], "access": "write"}], [{"path": paths["portfolio"], "access": "admin"}],
                [], [{"path": paths["portfolio"], "access": "write", "x": 1}]):
        with pytest.raises(ValueError):
            validate_workspace(adir, bad, str(state))
    with pytest.raises(ValueError):
        validate_workspace(adir + "/", entries, str(state))


# ------------------------------------------------------------------------------------------------ operator registry
@pytest.fixture
def cfg_world(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    a, b = tmp_path / "a", tmp_path / "b"
    for r in (a, b):
        (r / ".git").mkdir(parents=True)
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)

    def load(repos):
        path = tmp_path / "cfg.json"
        path.write_text(json.dumps({"executable": str(exe), "allowed_tools": ["Read"], "enabled": True,
                                    "repos": repos, "worktree_root": str((tmp_path / "wt").resolve())}))
        path.chmod(0o600)
        return load_agent_config(path, state)
    return load, str(a.resolve()), str(b.resolve())


def test_registry_accepts_strings_and_objects(cfg_world):
    load, a, b = cfg_world
    cfg = load([a, {"id": "product-config", "path": b, "baseline_refs": ["refs/heads/main", "refs/heads/release"],
                    "default_baseline_ref": "refs/heads/main", "protected_paths": [".github/**", "CODEOWNERS"]}])
    assert cfg.repos == (a, b)
    (entry,) = cfg.registry
    assert entry == RepoEntry("product-config", b, ("refs/heads/main", "refs/heads/release"), "refs/heads/main",
                              (".github/**", "CODEOWNERS"))
    assert len(registry_sha256(cfg.registry)) == 64 and len(entry.entry_sha256) == 64
    only_default = load([{"id": "x", "path": a, "default_baseline_ref": "refs/heads/main"}]).registry[0]
    assert only_default.baseline_refs == ("refs/heads/main",)
    assert load([a]).registry == ()  # plain strings: unchanged, legacy single-repository entries only


@pytest.mark.parametrize("entry", [
    {"id": "../x", "path": "A"}, {"id": "x"}, {"path": "A"}, {"id": "x", "path": "A", "retention": {}},
    {"id": "x", "path": "A", "baseline_refs": ["main"]}, {"id": "x", "path": "A", "baseline_refs": ["refs/remotes/o/m"]},
    {"id": "x", "path": "A", "baseline_refs": ["refs/heads/a"], "default_baseline_ref": "refs/heads/b"},
    {"id": "x", "path": "A", "baseline_refs": []}, {"id": "x", "path": "A", "protected_paths": ["/etc"]},
    {"id": "x", "path": "A", "baseline_refs": ["refs/heads/a", "refs/heads/a"]},
    {"id": "x", "path": "A", "baseline_refs": ["refs/heads/a..b"]},
])
def test_registry_object_entries_are_validated(cfg_world, entry):
    load, a, _ = cfg_world
    entry = {k: (a if v == "A" else v) for k, v in entry.items()}
    with pytest.raises(AgentConfigError):
        load([entry])


def test_registry_ids_unique_and_paths_unique(cfg_world):
    load, a, b = cfg_world
    with pytest.raises(AgentConfigError):
        load([{"id": "x", "path": a}, {"id": "x", "path": b}])
    with pytest.raises(AgentConfigError):
        load([a, {"id": "x", "path": a}])


# ------------------------------------------------------------------------------------------------ migration 0018
def test_migration_0018_upgrade_keeps_data_and_tables_are_insert_only(tmp_path):
    db = open_database(tmp_path / "u.db")
    try:
        applied = apply_migrations(db, ROOT / "migrations", limit=17)
        assert applied[-1] == 17
        db.execute("INSERT INTO audit_entries(principal, action, resource, decision, details_json, created_at) "
                   "VALUES ('op','read','run','allowed','{}','now')")
        assert apply_migrations(db, ROOT / "migrations", limit=18) == [18]
        assert db.execute("SELECT principal FROM audit_entries").fetchall() == [("op",)]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"run_repo_bindings", "attempt_worktrees", "attempt_repo_commits"} <= tables
        cols = {r[1] for r in db.execute("PRAGMA table_info(task_attempts)")}
        assert "repo_id" not in cols  # hot tables are not altered
    finally:
        db.close()


@pytest.fixture
def attempt_db(tmp_path):
    from synapse.persistence.workflow_store import create_run, publish_workflow
    db = open_database(tmp_path / "d.db")
    apply_migrations(db, ROOT / "migrations")
    publish_workflow(db, workflow_id="wf", version="1", nodes=[{"id": "n", "dependencies": []}], principal="op",
                     idempotency_key="p")
    run = create_run(db, workflow_id="wf", version="1", principal="op", idempotency_key="c").response
    task_id = dict(run["tasks"])["n"]
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state,started_at) "
               "VALUES('att1',?,1,'p','running','now')", (task_id,))
    db.execute("PRAGMA foreign_keys = ON")
    yield db, run["run_id"]
    db.close()


def test_insert_only_and_append_only_triggers(attempt_db):
    from synapse.persistence import repo_bindings as rb
    db, run_id = attempt_db
    sha = "a" * 40
    db.execute("BEGIN")
    rb.bind_run_repos(db, run_id, [RepoBinding("portfolio", "/srv/p", "refs/heads/main", sha, (".github/**",), "e" * 64)])
    rb.record_attempt_worktrees(db, "att1", [rb.AttemptWorktree("portfolio", "write", "/w/p", "synapse/r/t/1", sha)])
    rb.commit_intent(db, "att1", "portfolio", sha)
    db.execute("COMMIT")
    for stmt in ("UPDATE run_repo_bindings SET baseline_sha='%s'" % ("b" * 40), "DELETE FROM run_repo_bindings",
                 "INSERT OR REPLACE INTO run_repo_bindings SELECT * FROM run_repo_bindings",
                 "UPDATE attempt_worktrees SET path='/x'", "DELETE FROM attempt_worktrees",
                 "INSERT OR REPLACE INTO attempt_worktrees SELECT * FROM attempt_worktrees",
                 "UPDATE attempt_repo_commits SET state='done'", "DELETE FROM attempt_repo_commits"):
        with pytest.raises(sqlite3.DatabaseError):
            db.execute(stmt)
    db.execute("BEGIN")
    with pytest.raises(sqlite3.DatabaseError):  # a second intent for the same (attempt, repo)
        rb.commit_intent(db, "att1", "portfolio", sha)
    with pytest.raises(sqlite3.DatabaseError):  # a result for an intent that does not exist
        rb.commit_result(db, "att1", "other", sha, state="done", candidate_sha="c" * 40)
    with pytest.raises(sqlite3.DatabaseError):  # a result whose head_before differs from the intent
        rb.commit_result(db, "att1", "portfolio", "d" * 40, state="done", candidate_sha="c" * 40)
    rb.commit_result(db, "att1", "portfolio", sha, state="done", candidate_sha="c" * 40, paths=2)
    with pytest.raises(sqlite3.DatabaseError):  # exactly one result
        rb.commit_result(db, "att1", "portfolio", sha, state="failed", reason="git_error")
    with pytest.raises(ValueError):  # reasons are fixed codes, never paths or git output
        rb.commit_result(db, "att1", "x", sha, state="failed", reason="fatal: /etc/passwd")
    db.execute("COMMIT")
    assert rb.commit_states(db, "att1") == {"portfolio": "done"}


def test_workspace_specs_cross_check():
    b = [RepoBinding("a", "/a", "refs/heads/main", "1" * 40, (), "e" * 64, "T1"),
         RepoBinding("b", "/b", "refs/heads/main", "2" * 40, (), "e" * 64, "T2")]

    def spec(*repos):
        return {"repos": [{"repo_id": r, "access": acc} for r, acc in repos]}
    ids = {"x": "T1", "y": "T2"}
    check_workspace_specs({"x": spec(("a", "write"), ("b", "read")), "y": spec(("b", "write"))}, b, ids)
    with pytest.raises(ValueError):  # two writers on one repository
        check_workspace_specs({"x": spec(("a", "write")), "y": spec(("a", "write"))}, b)
    with pytest.raises(ValueError):  # a multi-repository task without bindings
        check_workspace_specs({"x": spec(("a", "write"))}, None)
    with pytest.raises(ValueError):  # a legacy task inside a multi-repository run
        check_workspace_specs({"x": spec(("a", "write")), "y": {}}, b)
    with pytest.raises(ValueError):  # an unbound repository
        check_workspace_specs({"x": spec(("c", "write"))}, b)
    with pytest.raises(ValueError):  # the binding names another writer
        check_workspace_specs({"x": spec(("a", "write"), ("b", "write")), "y": spec(("b", "read"))}, b, ids)
    check_workspace_specs({"x": {}}, None)  # legacy run: nothing to check


def test_bindings_digest_covers_baseline_and_writer():
    from synapse.persistence.repo_bindings import bindings_digest
    a = RepoBinding("a", "/a", "refs/heads/main", "1" * 40, (), "e" * 64, "T1")
    assert bindings_digest([a]) != bindings_digest([RepoBinding("a", "/a", "refs/heads/main", "2" * 40, (), "e" * 64, "T1")])
    assert bindings_digest([a]) != bindings_digest([RepoBinding("a", "/a", "refs/heads/main", "1" * 40, (), "e" * 64, None)])
    assert bindings_digest([a]) == bindings_digest([RepoBinding("a", "/elsewhere", "refs/heads/main", "1" * 40, ("x",),
                                                                "e" * 64, "T1")])  # paths are not in it


# ------------------------------------------------------------------------------------------------ end to end
class World:
    """Three registered repositories, a worktree manager, a journalled runtime with the fake CLI, the service."""

    def __init__(self, tmp_path, *, workspaces_factory=AttemptWorkspaces):
        self.tmp = tmp_path
        self.state = tmp_path / "state"
        self.state.mkdir(mode=0o700)
        self.repos = {rid: make_repo(tmp_path / "repos" / rid) for rid in REPO_IDS}
        self.root = str((tmp_path / "wt").resolve())
        self.manager = WorktreeManager(list(self.repos.values()), self.root)
        self.entries = tuple(RepoEntry(rid, path, ("refs/heads/main", "refs/heads/release"), "refs/heads/main",
                                       (".github/**",)) for rid, path in self.repos.items())
        for path in self.repos.values():
            git(path, "branch", "release")
        self.path = tmp_path / "db"
        self.db = open_database(self.path)
        apply_migrations(self.db, ROOT / "migrations")
        self.blobs = tmp_path / "blobs"
        self.blobs.mkdir(mode=0o700)
        self.log = tmp_path / "spawns.jsonl"
        exe = tmp_path / "bin" / "claude"
        exe.parent.mkdir()
        exe.write_text(FAKE.format(py=sys.executable, log=str(self.log)))
        exe.chmod(0o755)
        self.exe = exe
        self.profile = ClaudeCliProfile(executable=str(exe), allowed_tools=("Read", "Edit"), state_dir=str(self.state),
                                        enabled=True, term_grace_seconds=0.3, permission_mode="plan",
                                        git_repos=tuple(self.repos.values()))
        self.journal = self.state / "spawn-journal"
        self.workspaces = workspaces_factory(self.manager)
        self.workspaces.allow_unsandboxed = True  # the fake CLI runs without bwrap here; see the sandbox test
        self.runtime = self.new_runtime(self.db)
        self.service = AgentRunService(self.db, self.runtime, self.manager, None, RepoRegistry(self.entries))

    def new_runtime(self, db, workspaces=None):
        rt = AgentRuntime(db, self.blobs, profile=self.profile, spawn_journal=SpawnJournal(self.journal),
                          worktree_check=worktree_checker(self.manager), workspaces=workspaces or self.workspaces,
                          commit_recovery=lambda d: resolve_commit_intents(d, self.manager))
        rt._park_poll, rt._cancel_timeout = 0.01, 10
        return rt

    def request(self, tasks, repos=None):
        return {"repos": repos or [{"repo_id": rid} for rid in REPO_IDS], "tasks": tasks}

    @staticmethod
    def task(node="edit", repos=None, prompt=None, **over):
        t = {"id": node, "prompt": json.dumps(prompt or {"mode": "ok"}), "allowed_tools": ["Read", "Edit"],
             "timeout_seconds": 20, "repos": repos or [{"repo_id": "portfolio", "access": "write"},
                                                       {"repo_id": "product-config", "access": "write"},
                                                       {"repo_id": "tenant-specification", "access": "read"}]}
        t.update(over)
        return t

    def start(self, request, key="k1"):
        async def go():
            result = await self.service.start(key, request)
            if self.runtime._active is not None:
                await asyncio.wait_for(self.runtime._active, 60)
            return result
        return asyncio.run(go())

    def spawns(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def states(self, db=None):
        db = db or self.db
        return ([r[0] for r in db.execute("SELECT state FROM runs")],
                dict(db.execute("SELECT node_id,state FROM tasks")))

    def attempts(self, db=None):
        return [tuple(r) for r in (db or self.db).execute("SELECT attempt_no,state FROM task_attempts ORDER BY rowid")]

    def summary_detail(self):
        rows = self.db.execute("SELECT sha256 FROM artifacts WHERE provenance_json LIKE '%agent_summary%'").fetchall()
        return [json.loads(self.runtime.blobs.read(r[0]))["detail"] for r in rows]

    def commits(self, db=None):
        return [tuple(r) for r in (db or self.db).execute(
            "SELECT repo_id,state,reason FROM attempt_repo_commits ORDER BY seq")]

    def close(self):
        subprocess.run(["pkill", "-9", "-f", str(self.exe)], check=False)
        self.db.close()


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def test_multi_repo_run_pins_baselines_spawns_once_and_commits_xml_to_local_branches(world):
    base = {rid: head(p) for rid, p in world.repos.items()}
    prompt = {"mode": "ok", "writes": {"portfolio": {"config.xml": "<p/>\n"},
                                       "product-config": {"deep/x.xml": "<x/>\n"}}}
    result = world.start(world.request([world.task(prompt=prompt)],
                                       repos=[{"repo_id": "portfolio", "baseline_ref": "refs/heads/release"},
                                              {"repo_id": "product-config"}, {"repo_id": "tenant-specification"}]))
    assert world.states() == (["succeeded"], {"edit": "completed"})
    run_id = result.response["run_id"]
    task_id = result.response["tasks"][0]["task_id"]
    rows = world.db.execute("SELECT repo_id,baseline_ref,baseline_sha FROM run_repo_bindings WHERE run_id=? "
                            "ORDER BY repo_id", (run_id,)).fetchall()
    assert rows == [("portfolio", "refs/heads/release", base["portfolio"]),
                    ("product-config", "refs/heads/main", base["product-config"]),
                    ("tenant-specification", "refs/heads/main", base["tenant-specification"])]
    # one spawn for the whole attempt, cwd = attempt dir, which holds exactly the declared worktrees
    (spawn,) = world.spawns()
    assert spawn["cwd"] == world.manager.attempt_dir(run_id, task_id, 1)
    assert spawn["entries"] == sorted(REPO_IDS)
    assert world.db.execute("SELECT count(*) FROM attempt_spawn_marks").fetchone() == (1,)
    trees = dict(world.db.execute("SELECT repo_id,access FROM attempt_worktrees"))
    assert trees == {"portfolio": "write", "product-config": "write", "tenant-specification": "read"}
    assert world.commits() == [("portfolio", "intent", None), ("portfolio", "done", None),
                               ("product-config", "intent", None), ("product-config", "done", None)]
    writers = dict(world.db.execute("SELECT repo_id,writer_task_id FROM run_repo_bindings"))
    assert writers == {"portfolio": task_id, "product-config": task_id, "tenant-specification": None}
    branch = "synapse/%s/%s/1" % (run_id, task_id)
    for rid in ("portfolio", "product-config"):
        tip = git(world.repos[rid], "rev-parse", branch).strip()
        assert git(world.repos[rid], "rev-parse", tip + "^").strip() == base[rid]
        assert head(world.repos[rid], "main") == base[rid]  # the owner's branches never move
        assert git(world.repos[rid], "remote") == ""  # and there is nothing to push to; nothing was pushed
    assert branch not in git(world.repos["tenant-specification"], "branch", "--list")
    spec = at.load_task_specs(world.db, run_id)["edit"]
    assert not {"worktree", "worktree_head", "repo", "base_ref"} & set(spec)  # ADR 026 D5 spec shape
    assert [r["access"] for r in spec["repos"]] == ["write", "write", "read"]
    # no repository path or file name reaches the event stream
    payloads = " ".join(r[0] for r in world.db.execute("SELECT payload_json FROM events"))
    assert "deep/x.xml" not in payloads and world.repos["portfolio"] not in payloads


def test_non_xml_edit_fails_the_attempt_and_partial_commits_stay_as_evidence(world):
    prompt = {"writes": {"portfolio": {"config.xml": "<p/>\n"}, "product-config": {"build.sh": "echo\n"}}}
    world.start(world.request([world.task(prompt=prompt)]))
    assert world.states() == (["failed"], {"edit": "failed"})
    assert world.attempts() == [(1, "failed")]
    assert world.summary_detail() == ["commit_failed:product-config:path_not_editable"]
    assert world.commits() == [("portfolio", "intent", None), ("portfolio", "done", None),
                               ("product-config", "intent", None), ("product-config", "failed", "path_not_editable")]
    branches = git(world.repos["portfolio"], "branch", "--list")
    assert "synapse/" in branches  # repository 1's commit is kept (not undone)
    tip = git(world.repos["product-config"], "for-each-ref", "--format=%(objectname)", "refs/heads/synapse/").strip()
    assert tip == head(world.repos["product-config"], "main")  # nothing committed in repository 2


def test_protected_path_and_symlink_are_refused_at_commit(world):
    prompt = {"writes": {"portfolio": {".github/ci.xml": "<ci evil='1'/>\n"}}}
    world.start(world.request([world.task(prompt=prompt)]), key="p")
    assert world.summary_detail() == ["commit_failed:portfolio:path_not_allowed"]


def test_symlink_named_xml_is_refused(world):
    prompt = {"links": {"portfolio": {"hostname.xml": "/etc/hostname"}}}
    world.start(world.request([world.task(prompt=prompt)]), key="s")
    assert world.summary_detail() == ["commit_failed:portfolio:mode_not_allowed"]


def test_unchanged_write_repo_is_recorded_unchanged(world):
    world.start(world.request([world.task(prompt={"writes": {"portfolio": {"config.xml": "<p/>"}}})]))
    assert world.states() == (["succeeded"], {"edit": "completed"})
    assert world.commits() == [("portfolio", "intent", None), ("portfolio", "done", None),
                               ("product-config", "intent", None), ("product-config", "unchanged", None)]
    row = world.db.execute("SELECT candidate_sha,head_before FROM attempt_repo_commits WHERE state='unchanged'").fetchone()
    assert row[0] == row[1] == head(world.repos["product-config"])


def test_failed_agent_is_never_committed(world):
    world.start(world.request([world.task(prompt={"mode": "fail", "writes": {"portfolio": {"config.xml": "<x/>"}}})]))
    assert world.states() == (["failed"], {"edit": "failed"}) and world.commits() == []


def test_read_only_reviewer_task_needs_no_commit(world):
    task = world.task(repos=[{"repo_id": "portfolio", "access": "read"}])
    world.start(world.request([task], repos=[{"repo_id": "portfolio"}]))
    assert world.states() == (["succeeded"], {"edit": "completed"}) and world.commits() == []


def test_two_tasks_writing_different_repos_and_one_task_writing_several(world):
    a = world.task("a", repos=[{"repo_id": "portfolio", "access": "write"},
                               {"repo_id": "product-config", "access": "write"}],
                   prompt={"writes": {"portfolio": {"a.xml": "<a/>"}, "product-config": {"b.xml": "<b/>"}}})
    b = world.task("b", repos=[{"repo_id": "tenant-specification", "access": "write"},
                               {"repo_id": "portfolio", "access": "read"}], depends_on=["a"],
                   prompt={"writes": {"tenant-specification": {"t.xml": "<t/>"}}})
    world.start(world.request([a, b]))
    assert world.states() == (["succeeded"], {"a": "completed", "b": "completed"})
    assert [c for c in world.commits() if c[1] == "done"] == [("portfolio", "done", None),
                                                              ("product-config", "done", None),
                                                              ("tenant-specification", "done", None)]


@pytest.mark.parametrize("body,needle", [
    ({"repos": [{"repo_id": "nope"}]}, "repo_not_registered"),
    ({"repos": [{"repo_id": "portfolio", "baseline_ref": "refs/heads/other"}]}, "baseline_ref_not_allowed"),
    ({"repos": [{"repo_id": "portfolio", "baseline_ref": "main"}]}, "refs/heads"),
    ({"repos": [{"repo_id": "portfolio"}], "repo": "/x", "base_ref": "main"}, "not both"),
    ({"repos": [{"repo_id": "portfolio"}, {"repo_id": "portfolio"}]}, "repeat"),
    ({"repos": []}, "1 to 16"),
])
def test_bad_multi_repo_requests_change_nothing(world, body, needle):
    task = world.task(repos=[{"repo_id": body["repos"][0]["repo_id"] if body["repos"] else "portfolio",
                              "access": "write"}])
    with pytest.raises(AgentRunError) as info:
        world.start({"tasks": [task], **body})
    assert info.value.status == 422 and needle in str(info.value)
    assert world.db.execute("SELECT count(*) FROM runs").fetchone() == (0,)
    assert world.spawns() == [] and os.listdir(world.root) == [] or all(n.startswith(".lock") for n in os.listdir(world.root))


def test_two_writers_on_one_repo_and_undeclared_repo_are_refused(world):
    a = world.task("a", repos=[{"repo_id": "portfolio", "access": "write"}])
    b = world.task("b", repos=[{"repo_id": "portfolio", "access": "write"}])
    with pytest.raises(AgentRunError, match="at most one task"):
        world.start(world.request([a, b]))
    c = world.task("c", repos=[{"repo_id": "tenant-specification", "access": "write"}])
    with pytest.raises(AgentRunError, match="naming the run's repos"):
        world.start(world.request([c], repos=[{"repo_id": "portfolio"}]))
    assert world.db.execute("SELECT count(*) FROM run_repo_bindings").fetchone() == (0,)


def test_unresolvable_baseline_writes_nothing(world):
    for path in world.repos.values():
        git(path, "branch", "-D", "release")
    with pytest.raises(AgentRunError, match="baseline_ref_unresolved"):
        world.start(world.request([world.task(repos=[{"repo_id": "portfolio", "access": "write"}])],
                                  repos=[{"repo_id": "portfolio", "baseline_ref": "refs/heads/release"}]))
    assert world.db.execute("SELECT count(*) FROM runs").fetchone() == (0,)


class FailingCreate(AttemptWorkspaces):
    def prepare(self, run_id, task_id, attempt_no, repos, bindings):
        bindings = dict(bindings)
        b = bindings["product-config"]
        bindings["product-config"] = RepoBinding(b.repo_id, b.path, b.baseline_ref, "f" * 40, b.protected_paths,
                                                 b.entry_sha256)
        return super().prepare(run_id, task_id, attempt_no, repos, bindings)


def test_worktree_creation_failure_spawns_nothing_and_leaves_no_worktree(tmp_path):
    w = World(tmp_path, workspaces_factory=FailingCreate)
    try:
        w.start(w.request([w.task()]))
        assert w.states() == (["failed"], {"edit": "failed"})
        assert w.summary_detail() == ["worktree_create_failed:product-config"]
        assert w.spawns() == [] and w.db.execute("SELECT count(*) FROM attempt_spawn_marks").fetchone() == (0,)
        assert w.db.execute("SELECT count(*) FROM attempt_worktrees").fetchone() == (0,)
        for path in w.repos.values():
            assert "synapse/" not in git(path, "branch", "--list")
    finally:
        w.close()


def test_unrecorded_worktrees_are_discarded_and_never_spawned(world, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(agent_engine, "record_attempt_worktrees", boom)
    world.start(world.request([world.task()]))
    assert world.summary_detail() == ["worktree_not_recorded"] and world.spawns() == []
    for path in world.repos.values():
        assert "synapse/" not in git(path, "branch", "--list")
        assert git(path, "worktree", "list").count("\n") == 1


def test_workspace_task_without_hooks_is_refused_before_spawn(world, monkeypatch):
    monkeypatch.setattr(world.service.runtime, "_workspaces", None)
    world.service.runtime.profile = world.profile.__class__(**{**{f: getattr(world.profile, f) for f in (
        "executable", "allowed_tools", "state_dir", "enabled", "term_grace_seconds", "permission_mode", "git_repos")},
        "sandbox": False})
    hooks = AttemptWorkspaces(world.manager, allow_unsandboxed=True)
    monkeypatch.setattr(world.service, "runtime", world.runtime)
    # the service passes (hooks present at validation), the engine then finds none: refused before any spawn
    world.runtime._workspaces = hooks

    async def go():
        result = await world.service.start("k", world.request([world.task()]))
        return result
    real_launch = world.runtime._launch

    def launch(run_id):
        world.runtime._workspaces = None
        real_launch(run_id)
    monkeypatch.setattr(world.runtime, "_launch", launch)

    async def run():
        await go()
        await asyncio.wait_for(world.runtime._active, 60)
    asyncio.run(run())
    assert world.summary_detail() == ["multi_repo_unavailable"] and world.spawns() == []


def test_multi_repo_runs_need_the_sandbox_unless_the_operator_waives_it(world):
    world.workspaces.allow_unsandboxed = False  # profile sandbox is off in this world
    with pytest.raises(AgentRunError, match="multi_repo_requires_sandbox"):
        world.start(world.request([world.task()]))
    assert world.db.execute("SELECT count(*) FROM runs").fetchone() == (0,)
    # the engine refuses on its own too (a run created while waived, waiver withdrawn before the attempt)
    world.workspaces.allow_unsandboxed = True
    real_launch = world.runtime._launch

    def launch(run_id):
        world.workspaces.allow_unsandboxed = False
        real_launch(run_id)
    world.runtime._launch = launch
    world.start(world.request([world.task()]), key="k2")
    assert world.summary_detail() == ["multi_repo_requires_sandbox"] and world.spawns() == []


def test_config_waiver_is_a_strict_boolean(cfg_world, tmp_path):
    load, a, _ = cfg_world
    path = tmp_path / "cfg.json"
    load([a])
    data = json.loads(path.read_text())
    for value, ok in ((True, True), (False, True), ("yes", False)):
        path.write_text(json.dumps({**data, "allow_multi_repo_unsandboxed": value}))
        path.chmod(0o600)
        if ok:
            assert load_agent_config(path, tmp_path / "state").allow_multi_repo_unsandboxed is value
        else:
            with pytest.raises(AgentConfigError):
                load_agent_config(path, tmp_path / "state")


def test_lock_order_is_by_repository_path_not_repo_id(tmp_path):
    """Bindings outlive registry edits: run 1 binds A->p1, B->p2 and run 2 binds A->p2, B->p1. Locking by repo_id
    would take p1 then p2 in run 1 and p2 then p1 in run 2 (deadlock); locking by path is one global order."""
    p1, p2 = make_repo(tmp_path / "p1"), make_repo(tmp_path / "p2")
    mgr = WorktreeManager([p1, p2], str((tmp_path / "wt").resolve()))
    order = []
    real = WorktreeManager._lock

    def spy(self, repo):
        order.append(repo)
        return real(self, repo)
    try:
        WorktreeManager._lock = spy
        mgr.create_attempt("run1", "t", 1, [AttemptRepo("A", p1, head(p1), "write"), AttemptRepo("B", p2, head(p2), "write")])
        first, order[:] = list(order), []
        mgr.create_attempt("run2", "t", 1, [AttemptRepo("A", p2, head(p2), "write"), AttemptRepo("B", p1, head(p1), "write")])
        assert first == order == sorted([p1, p2])
    finally:
        WorktreeManager._lock = real
    errors = []

    def go(run, pairs):
        try:
            for n in range(2, 6):
                mgr.create_attempt(run, "t", n, [AttemptRepo(rid, path, head(path), "write") for rid, path in pairs])
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)
    threads = [threading.Thread(target=go, args=("run1", [("A", p1), ("B", p2)])),
               threading.Thread(target=go, args=("run2", [("A", p2), ("B", p1)]))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads) and errors == []


def test_recovery_never_calls_a_landed_but_rule_breaking_commit_done(tmp_path):
    w, image = crash_world(tmp_path, "commit_before_git")
    try:
        result = w.start(w.request([w.task(prompt={"writes": {"portfolio": {"config.xml": "<p/>"}}})]))
        run_id, task_id = result.response["run_id"], result.response["tasks"][0]["task_id"]
        tree = os.path.join(w.root, run_id, task_id, "1", "portfolio")
        branch = "synapse/%s/%s/1" % (run_id, task_id)
        base = head(w.repos["portfolio"], "main")
        # what a same-uid agent could leave: exactly one commit on head_before, index == that tree, adding a script
        git(w.repos["portfolio"], "update-ref", "refs/heads/" + branch, base)
        git(tree, "reset", "-q", "--hard", base)
        Path(tree, "evil.sh").write_text("#!/bin/sh\nid\n")
        os.chmod(os.path.join(tree, "evil.sh"), 0o755)
        git(tree, "add", "-A")
        git(tree, "commit", "-q", "-m", "x")
        db = open_database(image)
        try:
            w.new_runtime(db).recover_interrupted()
            assert w.commits(db)[-1] == ("portfolio", "unknown", "unverifiable")
        finally:
            db.close()
    finally:
        w.close()


def test_a_failure_after_the_branch_moved_is_never_recorded_failed(world, monkeypatch):
    real = WorktreeManager.commit_attempt

    def landed_then_raise(self, *a, **k):
        out = real(self, *a, **k)
        if out["committed"]:  # the branch moved, then a later step failed
            raise WorktreeError("post-commit step failed")
        return out
    monkeypatch.setattr(WorktreeManager, "commit_attempt", landed_then_raise)
    world.start(world.request([world.task(prompt={"writes": {"portfolio": {"config.xml": "<p/>"}}})]))
    assert world.commits() == [("portfolio", "intent", None), ("portfolio", "done", None),
                               ("product-config", "intent", None), ("product-config", "unchanged", None)]
    assert world.states() == (["succeeded"], {"edit": "completed"})


def test_runtime_start_refuses_workspace_specs_without_bindings(world):
    from synapse.persistence.workflow_store import create_run, publish_workflow
    publish_workflow(world.db, workflow_id="wf", version="1", nodes=[{"id": "n", "dependencies": []}],
                     principal="op", idempotency_key="p")
    run_id = create_run(world.db, workflow_id="wf", version="1", principal="op", idempotency_key="c").response["run_id"]
    spec = {"prompt": "{}", "allowed_tools": ["Read"], "permission_mode": "acceptEdits", "timeout_seconds": 20,
            "repos": [{"repo_id": "portfolio", "access": "write"}]}
    with pytest.raises(at.AgentSpecError):
        world.runtime.start(run_id, "k", {"n": spec})
    binding = RepoBinding("portfolio", world.repos["portfolio"], "refs/heads/main", head(world.repos["portfolio"]),
                          (), "e" * 64, None)
    for bad in ({**spec, "worktree": world.root + "/x"}, {**spec, "repo": "/srv/x"}, {**spec, "worktree_head": "a" * 40}):
        with pytest.raises(at.AgentSpecError):  # a multi-repository spec carries no path, HEAD or legacy identity
            world.runtime.start(run_id, "k", {"n": bad}, repo_bindings=[binding])
    with pytest.raises(at.AgentSpecError):  # the binding must name this task as the writer
        world.runtime.start(run_id, "k", {"n": spec}, repo_bindings=[binding])
    assert world.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone() == ("queued",)
    assert world.db.execute("SELECT count(*) FROM run_repo_bindings").fetchone() == (0,)


def test_bindings_roll_back_with_a_failed_start_transaction(world, monkeypatch):
    from synapse.daemon import agent_runtime as ar

    def refuse(*a, **k):
        raise ar.StateConflict("injected")
    monkeypatch.setattr(ar, "write_run_transition", refuse)
    with pytest.raises(AgentRunError):
        world.start(world.request([world.task()]))
    assert world.db.execute("SELECT count(*) FROM run_repo_bindings").fetchone() == (0,)


# ------------------------------------------------------------------------------------------------ crash windows
def snapshot(path, image):
    src = sqlite3.connect(path)
    dst = sqlite3.connect(image)
    src.backup(dst)
    dst.close()
    src.close()


class CrashAt(AttemptWorkspaces):
    """Copies the database (what a SIGKILLed daemon leaves) at one point of the per-attempt lifecycle."""
    point = None
    image = None
    db_path = None

    def prepare(self, run_id, task_id, attempt_no, repos, bindings):
        if self.point == "before_create":
            snapshot(self.db_path, self.image)
        out = super().prepare(run_id, task_id, attempt_no, repos, bindings)
        if self.point == "after_create":
            snapshot(self.db_path, self.image)
        return out

    def commit(self, run_id, task_id, attempt_no, repo_id, binding, message, expected_head):
        if self.point == "commit_before_git" and repo_id == "portfolio":
            snapshot(self.db_path, self.image)
        out = super().commit(run_id, task_id, attempt_no, repo_id, binding, message, expected_head)
        if self.point == "commit_after_git" and repo_id == "portfolio":
            snapshot(self.db_path, self.image)
        return out


def crash_world(tmp_path, point):
    class Hooks(CrashAt):
        pass
    Hooks.point, Hooks.image, Hooks.db_path = point, tmp_path / "image.db", tmp_path / "db"
    return World(tmp_path, workspaces_factory=Hooks), Hooks.image


@pytest.mark.parametrize("point", ["before_create", "after_create"])
def test_crash_before_spawn_requeues_a_new_attempt_with_fresh_worktrees(tmp_path, point):
    w, image = crash_world(tmp_path, point)
    try:
        prompt = {"writes": {"portfolio": {"config.xml": "<p/>"}}}
        result = w.start(w.request([w.task(prompt=prompt)]))
        run_id, task_id = result.response["run_id"], result.response["tasks"][0]["task_id"]
        w.log.unlink()
        db = open_database(image)
        try:
            assert db.execute("SELECT count(*) FROM attempt_spawn_marks").fetchone() == (0,)
            assert db.execute("SELECT count(*) FROM attempt_worktrees").fetchone() == (0,)
            # remove what the finished first run left so the re-driven attempt is observable on its own
            for rid, path in w.repos.items():
                git(path, "worktree", "remove", "--force", os.path.join(w.root, run_id, task_id, "1", rid))
                for b in git(path, "for-each-ref", "--format=%(refname:short)", "refs/heads/synapse/").split():
                    git(path, "branch", "-D", b)
            if point == "after_create":  # the crash left attempt 1's worktrees on disk, unrecorded
                w.manager.create_attempt(run_id, task_id, 1, [
                    AttemptRepo(rid, w.repos[rid], head(w.repos[rid]), "write" if rid != "tenant-specification" else "read")
                    for rid in REPO_IDS])
            rt = w.new_runtime(db, workspaces=AttemptWorkspaces(w.manager, allow_unsandboxed=True))
            rt.recover_interrupted()
            assert rt._pending_redrive == run_id  # ADR 024: provably never spawned

            async def drive():
                assert rt.resume_parked()
                await asyncio.wait_for(rt._active, 60)
            asyncio.run(drive())
            assert w.states(db) == (["succeeded"], {"edit": "completed"})
            assert w.attempts(db) == [(1, "cancelled"), (2, "completed")]
            (spawn,) = w.spawns()
            assert spawn["cwd"].endswith("/%s/%s/2" % (run_id, task_id))  # a new attempt dir, from the pinned baseline
            assert db.execute("SELECT count(*) FROM attempt_worktrees").fetchone() == (3,)
            # ADR 026 D4 step 8: the never-spawned attempt's trees were removed (not forced) and recorded
            removals = db.execute("SELECT repo_id,reason,forced FROM attempt_worktree_removals ORDER BY repo_id").fetchall()
            assert removals == ([(rid, "never_spawned", 0) for rid in sorted(REPO_IDS)] if point == "after_create" else [])
            assert not os.path.exists(os.path.join(w.root, run_id, task_id, "1"))
        finally:
            db.close()
    finally:
        w.close()


@pytest.mark.parametrize("point,expected", [("commit_before_git", "not_done"), ("commit_after_git", "done")])
def test_crash_during_commit_is_resolved_from_git_never_replayed(tmp_path, point, expected):
    w, image = crash_world(tmp_path, point)
    try:
        prompt = {"writes": {"portfolio": {"config.xml": "<p/>"}, "product-config": {"c.xml": "<c/>"}}}
        result = w.start(w.request([w.task(prompt=prompt)]))
        run_id, task_id = result.response["run_id"], result.response["tasks"][0]["task_id"]
        branch = "synapse/%s/%s/1" % (run_id, task_id)
        landed = git(w.repos["portfolio"], "rev-parse", branch).strip()
        if point == "commit_before_git":  # rewind git to the moment of the snapshot: the commit had not happened
            git(w.repos["portfolio"], "update-ref", "refs/heads/" + branch, head(w.repos["portfolio"], "main"))
            git(os.path.join(w.root, run_id, task_id, "1", "portfolio"), "reset", "-q", "--mixed")
        db = open_database(image)
        try:
            assert w.commits(db) == [("portfolio", "intent", None)]
            rt = w.new_runtime(db)
            rt.recover_interrupted()
            assert w.commits(db) == [("portfolio", "intent", None), ("portfolio", expected, None)]
            sha = db.execute("SELECT candidate_sha FROM attempt_repo_commits WHERE state=?", (expected,)).fetchone()[0]
            assert sha == (landed if expected == "done" else None)
            # the attempt may have run: it is unknown, the task blocked, nothing re-committed or re-spawned
            assert w.attempts(db) == [(1, "unknown")]
            assert w.states(db) == (["recovering"], {"edit": "blocked"})
            assert len(w.spawns()) == 1
            rt.recover_interrupted()  # idempotent: no second result row
            assert len(w.commits(db)) == 2
        finally:
            db.close()
    finally:
        w.close()


def test_dangling_intent_with_a_foreign_branch_tip_is_unknown(tmp_path):
    w, image = crash_world(tmp_path, "commit_before_git")
    try:
        result = w.start(w.request([w.task(prompt={"writes": {"portfolio": {"config.xml": "<p/>"}}})]))
        run_id, task_id = result.response["run_id"], result.response["tasks"][0]["task_id"]
        tree = os.path.join(w.root, run_id, task_id, "1", "portfolio")
        Path(tree, "other.xml").write_text("<o/>")
        git(tree, "add", "-A")
        git(tree, "commit", "-q", "-m", "someone else")  # two commits past head_before: not ours to vouch for
        db = open_database(image)
        try:
            w.new_runtime(db).recover_interrupted()
            assert w.commits(db)[-1] == ("portfolio", "unknown", "unverifiable")
        finally:
            db.close()
    finally:
        w.close()


# ------------------------------------------------------------------------------------------------ single repo unchanged
def test_single_repo_runs_are_unchanged(world):
    from synapse.persistence.agent_tasks import spec_hash
    legacy = {"prompt_sha256": "0" * 64, "repo": "/srv/repo", "base_ref": "main", "worktree": "/w/r/t",
              "worktree_head": "a" * 40, "allowed_tools": ["Read"], "permission_mode": "plan", "timeout_seconds": 5}
    # the legacy full-spec hash is unchanged (no workspace key leaks into the v1 body)
    assert spec_hash(legacy) == spec_hash({**legacy, "workspace": None})
    req = {"repo": world.repos["portfolio"], "base_ref": "main", "prompt": json.dumps({"mode": "ok"}),
           "allowed_tools": ["Read", "Edit"], "permission_mode": "acceptEdits", "timeout_seconds": 20}
    out = world.start(req)
    assert world.states() == (["succeeded"], {"agent": "completed"})
    assert out.response["worktree_path"] == os.path.join(world.root, out.response["run_id"], out.response["task_id"])
    assert world.db.execute("SELECT count(*) FROM run_repo_bindings").fetchone() == (0,)
    assert world.db.execute("SELECT count(*) FROM attempt_worktrees").fetchone() == (0,)
    assert world.commits() == []  # legacy runs keep the operator-requested commit
    assert "workspace" not in at.load_task_specs(world.db, out.response["run_id"])["agent"]


# ------------------------------------------------------------------------------------------------ spawn-time recheck
class TamperAfterCreate(AttemptWorkspaces):
    """Changes one fresh tree between its creation and the spawn-time recheck."""
    how = None

    def prepare(self, run_id, task_id, attempt_no, repos, bindings):
        root, trees = super().prepare(run_id, task_id, attempt_no, repos, bindings)
        tree = next(t for t in trees if t.repo_id == "portfolio")
        Path(tree.path, "config.xml").write_text("<tampered/>")
        if self.how == "commit":
            git(tree.path, "commit", "-q", "-am", "moved")
        return root, trees


@pytest.mark.parametrize("how,reason", [("dirty", "worktree_dirty"), ("commit", "head_moved")])
def test_spawn_time_recheck_refuses_before_any_spawn(tmp_path, how, reason):
    class Hooks(TamperAfterCreate):
        pass
    Hooks.how = how
    w = World(tmp_path, workspaces_factory=Hooks)
    try:
        w.start(w.request([w.task()]))
        assert w.summary_detail() == [reason] and w.spawns() == []
        assert w.db.execute("SELECT count(*) FROM attempt_spawn_marks").fetchone() == (0,)
        assert w.commits() == []
    finally:
        w.close()


def test_recheck_detects_a_recorded_path_that_is_not_the_computed_one(world):
    from synapse.persistence.repo_bindings import AttemptWorktree
    p = world.repos["portfolio"]
    world.manager.create_attempt("r", "t", 1, [AttemptRepo("portfolio", p, head(p), "write")])
    good = world.manager.attempt_worktree_path("r", "t", 1, "portfolio")
    b = {"portfolio": RepoBinding("portfolio", p, "refs/heads/main", head(p), (), "e" * 64, "t")}
    assert world.workspaces.recheck("r", "t", 1, [AttemptWorktree("portfolio", "write", good, "x", head(p))], b) is None
    assert world.workspaces.recheck("r", "t", 1, [AttemptWorktree("portfolio", "write", p, "x", head(p))],
                                    b) == "worktree_mismatch"
    assert world.workspaces.recheck("r", "t", 1, [], b) == "worktree_mismatch"


def test_evidence_policy_nodes_are_refused_for_multi_repo_in_this_slice(world):
    from synapse.orchestration.agent_engine import process_outcome_verifier
    from synapse.orchestration.evidence_verifier import EVIDENCE_VERIFIER_ID

    class Evidence:
        async def __call__(self, task_id, candidate):
            return await process_outcome_verifier(task_id, candidate)

        def verifier_id_for(self, task_id):
            return EVIDENCE_VERIFIER_ID
    world.runtime._verify = Evidence()
    world.start(world.request([world.task()]))
    assert world.summary_detail() == ["evidence_multi_repo_unsupported"] and world.spawns() == []


def test_never_spawned_cleanup_keeps_a_dirty_tree(mgr, repos):
    wanted = attempt_repos(repos, {"tenant-specification": "read"})
    paths = mgr.create_attempt("r", "t", 1, wanted)
    Path(paths["portfolio"], "config.xml").write_text("<dirty/>")
    out = mgr.remove_attempt_clean("r", "t", 1, wanted)
    assert out == {"portfolio": "kept_dirty", "product-config": "removed", "tenant-specification": "removed"}
    assert os.path.exists(paths["portfolio"]) and not os.path.exists(paths["product-config"])
    assert "synapse/r/t/1" in git(repos["portfolio"], "branch", "--list")
    assert "synapse/r/t/1" not in git(repos["product-config"], "branch", "--list")
    assert mgr.remove_attempt_clean("r", "t", 1, wanted)["product-config"] == "absent"


def test_approval_subject_is_agent_run_multi_without_paths(world):
    from synapse.persistence.approval_subject import agent_subject
    gated = world.task(approval_step={"operation": "agent.run", "target": "repos"})

    async def go():
        result = await world.service.start("g", world.request([gated]))
        run_id = result.response["run_id"]
        for _ in range(500):
            if world.db.execute("SELECT 1 FROM approval_requests").fetchone():
                break
            await asyncio.sleep(0.02)
        world.runtime.cancel(run_id, "c")
        await asyncio.gather(world.runtime._active, return_exceptions=True)
        if world.runtime._cancelling is not None:
            await asyncio.gather(world.runtime._cancelling, return_exceptions=True)
        return run_id
    run_id = asyncio.run(go())
    spec = at.load_task_specs(world.db, run_id)["edit"]
    bound = spec["approval_step"]["candidate_sha256"]
    subject = agent_subject(world.db, None, run_id, bound, with_prompt=False)
    assert subject["kind"] == "agent_run_multi" and subject["spec_sha256"] == bound
    assert [r["repo_id"] for r in subject["repos"]] == sorted(REPO_IDS)
    assert subject["repos"][0]["baseline_sha"] == head(world.repos["portfolio"])
    text = json.dumps(subject)
    assert world.root not in text and world.repos["portfolio"] not in text
    assert world.spawns() == []  # parked for the approval, never spawned
    # the hash binds the pinned baselines: another bindings digest gives another spec hash
    assert at.spec_hash(dict(spec, bindings_sha256="0" * 64)) != bound


def test_an_error_while_rechecking_a_moved_branch_is_unknown_never_failed(world, monkeypatch):
    real = WorktreeManager.commit_attempt

    def landed_then_raise(self, *a, **k):
        out = real(self, *a, **k)
        if out["committed"]:
            raise WorktreeError("post-commit reset refused")
        return out

    def recheck_raises(self, *a, **k):
        raise WorktreeError("repository config.worktree is not allowed")
    monkeypatch.setattr(WorktreeManager, "commit_attempt", landed_then_raise)
    monkeypatch.setattr(WorktreeManager, "attempt_commit_landed", recheck_raises)
    world.start(world.request([world.task(prompt={"writes": {"portfolio": {"config.xml": "<p/>"}}})]))
    assert world.commits()[:2] == [("portfolio", "intent", None), ("portfolio", "unknown", "commit_state_unknown")]
    assert not any(c[1] == "failed" for c in world.commits())
    assert world.summary_detail() == ["commit_failed:portfolio:commit_state_unknown"]


def test_replace_refs_and_grafts_cannot_make_a_bad_commit_look_clean(mgr, repos):
    repo = repos["portfolio"]
    base = head(repo)
    wanted = attempt_repos({"portfolio": repo})
    paths = mgr.create_attempt("r", "t", 1, wanted)
    tree = paths["portfolio"]
    # a bad commit (a non-XML file) on the attempt branch, and a clean look-alike commit
    Path(tree, "evil.sh").write_text("id\n")
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "-m", "bad")
    bad = head(tree)
    git(tree, "reset", "-q", "--hard", base)
    Path(tree, "config.xml").write_text("<clean/>")
    git(tree, "commit", "-q", "-am", "clean")
    clean = head(tree)
    git(repo, "replace", bad, clean)  # refs/replace/<bad> -> clean
    (Path(repo) / ".git" / "info").mkdir(exist_ok=True)
    (Path(repo) / ".git" / "info" / "grafts").write_text("%s\n" % bad)  # graft: bad has no parent
    with pytest.raises(CommitRefused) as info:
        mgr.check_commit_range(repo, base, bad)
    assert info.value.reason == "path_not_editable"  # the stored object is checked, not the replacement
    assert mgr.check_commit_range(repo, base, clean) == 1


def test_one_repository_is_one_lock_and_duplicate_realpaths_are_refused(cfg_world, tmp_path, mgr, repos):
    load, a, b = cfg_world
    link = tmp_path / "alias"
    os.symlink(a, link)
    with pytest.raises(AgentConfigError):
        load([{"id": "x", "path": a}, {"id": "y", "path": str(link)}])
    taken = []
    real = WorktreeManager._lock

    def spy(self, repo):
        taken.append(repo)
        return real(self, repo)
    try:
        WorktreeManager._lock = spy
        from contextlib import ExitStack
        with ExitStack() as stack:
            p = repos["portfolio"]
            mgr._lock_all(stack, [("b-id", p), ("a-id", p), ("c", repos["product-config"])])
        assert taken == sorted([p, repos["product-config"]])  # one lock per resolved path, in path order
    finally:
        WorktreeManager._lock = real


def test_workspace_start_scans_each_worktree_once_not_the_attempt_directory(mgr, repos, tmp_path, monkeypatch):
    """The scan limit counts every file: scanning the attempt directory too would double-count all worktrees."""
    import synapse.providers.claude_cli as cc
    from synapse.providers.claude_cli import ClaudeCliProvider
    from synapse.providers.contracts import ExecutionRequest
    state = tmp_path / "state"
    state.mkdir()
    paths = mgr.create_attempt("r", "t", 1, attempt_repos(repos))
    adir = mgr.attempt_dir("r", "t", 1)
    scanned = []
    monkeypatch.setattr(cc, "check_repo_config", lambda cwd, adapter, allow=False: scanned.append(cwd))
    profile = ClaudeCliProfile(executable="/bin/true", allowed_tools=("Read",), state_dir=str(state), enabled=True,
                               permission_mode="plan", git_repos=tuple(repos.values()))
    provider = ClaudeCliProvider(profile)
    request = ExecutionRequest("p", "task", {"prompt": "x", "worktree": adir, "timeout_seconds": 5,
                                             "workspace": [{"path": p, "access": "write"} for p in paths.values()]})
    try:
        provider._plan(request)
    except Exception:
        pass  # only the scan targets matter here (the credential source may refuse in this fixture)
    assert sorted(scanned) == sorted(paths.values()) and adir not in scanned
