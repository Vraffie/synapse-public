"""Workspace discovery: find the git repositories inside a project folder and describe each one.

A "workspace" is a plain folder (not itself a repository, though it may be one) that holds several
sibling repositories plus ordinary folders. A Synapse run works on ONE repository through a worktree cut
from a committed ref, so the operator needs to see which repositories a folder contains and whether each
one can be used. Choosing and registering them stays an operator action (``scripts/synapse_up.py``): this
module never changes the daemon's repository allowlist and is not reachable from the daemon API.

Everything found is untrusted input:

* Discovery (``discover_repos``) lists directory NAMES with ``os.scandir`` and ``lstat``-style checks only.
  It never reads file contents, never follows a symlink (a symlinked directory is reported as skipped),
  never descends into a repository, ``.git``, a hidden directory or ``node_modules``-style trees, and is
  bounded by ``max_depth``/``max_entries``/``max_repos`` with explicit flags when a bound was hit.
* Inspection (``inspect_repo``) runs read-only git queries through the hardened runner shared with
  ``synapse.infrastructure.worktrees`` (same ``-c`` overrides, scrubbed environment, timeouts, output caps,
  argv lists, no shell). Before any work-tree query it applies the worktree manager's own unsafe-config
  refusal (``refuse_unsafe_repo_config``): a repository that the daemon would refuse at run time is reported
  ``usable=False`` with the reason, and ``git status`` is never run against it.

See docs/operations/workspace.md.
"""
from __future__ import annotations

import errno
import os
import stat
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace

from synapse.infrastructure.worktrees import (
    _EMPTY_TREE,
    WorktreeError,
    _git,
    refuse_unsafe_repo_config,
)

MAX_BRANCHES = 50
COMMIT_COUNT_CAP = 10000
DIRTY_COUNT_CAP = 10000
_QUERY_TIMEOUT = 20.0
_STATUS_TIMEOUT = 30.0
_STATUS_MAX_BYTES = 1 << 20
# Directories never descended into (besides every hidden directory, which covers .git, .venv, .cache ...).
IGNORED_DIRS = frozenset({"node_modules", "__pycache__", "site-packages", "venv"})


@dataclass(frozen=True, slots=True)
class RepoInfo:
    path: str                      # absolute, resolved
    name: str                      # basename
    rel: str                       # path relative to the scanned folder ("." when the folder is the repo)
    kind: str                      # "repo" | "linked" (worktree/submodule: .git is a file) | "bare" | "unsafe-gitdir"
    usable: bool
    reason: str                    # why not usable ("" when usable)
    branch: str | None = None      # current branch (None when detached or unknown)
    detached: bool = False
    head: str | None = None        # full commit sha of HEAD (None: no commits / unknown)
    branches: tuple[str, ...] = ()  # local branches, current first, then sorted; capped at MAX_BRANCHES
    branches_truncated: bool = False
    commit_count: int | None = None  # capped at COMMIT_COUNT_CAP + 1
    dirty_count: int | None = None   # porcelain entries (tracked changes + untracked), capped; None if not checked
    dirty_truncated: bool = False
    shallow: bool = False

    @property
    def short_head(self) -> str:
        return self.head[:12] if self.head else "-"

    @property
    def commit_count_display(self) -> str:
        if self.commit_count is None:
            return "?"
        return f"{COMMIT_COUNT_CAP}+" if self.commit_count > COMMIT_COUNT_CAP else str(self.commit_count)

    @property
    def has_uncommitted_changes(self) -> bool | None:
        return None if self.dirty_count is None else self.dirty_count > 0


@dataclass(frozen=True, slots=True)
class Skipped:
    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class WorkspaceScan:
    root: str
    root_is_repo: bool
    repos: tuple[RepoInfo, ...]
    skipped: tuple[Skipped, ...]
    entries_seen: int
    truncated: bool         # max_entries, max_repos or the time limit was hit: the result is incomplete
    depth_limited: bool     # some non-repository folder at max_depth was not looked into
    ignored_dirs: int       # hidden / node_modules-style directories not descended into


def has_control_chars(value: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)


def check_folder(path: str | os.PathLike[str], what: str = "folder") -> str:
    """Validate a caller-supplied path: str, no control characters, absolute, already resolved, a directory."""
    value = os.fspath(path)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty path")
    if has_control_chars(value):
        raise ValueError(f"{what} must not contain control characters")
    if not os.path.isabs(value):
        raise ValueError(f"{what} must be an absolute path")
    if os.path.normpath(value) != value:
        raise ValueError(f"{what} must be normalized")
    try:
        resolved = os.path.realpath(value, strict=True)
    except OSError as exc:
        raise ValueError(f"{what} does not exist") from exc
    if resolved != value:
        raise ValueError(f"{what} must be a resolved path (no symlinks)")
    if not stat.S_ISDIR(os.lstat(value).st_mode):
        raise ValueError(f"{what} must be a directory")
    return value


def _lstat_mode(path: str) -> int | None:
    try:
        return os.lstat(path).st_mode
    except OSError:
        return None


def _repo_marker(path: str) -> str | None:
    """Classify by NAMES only (lstat, never open): 'gitdir' | 'gitfile' | 'gitlink' | 'bare' | None."""
    mode = _lstat_mode(os.path.join(path, ".git"))
    if mode is not None:
        if stat.S_ISLNK(mode):
            return "gitlink"
        return "gitdir" if stat.S_ISDIR(mode) else "gitfile"
    head = _lstat_mode(os.path.join(path, "HEAD"))
    objects = _lstat_mode(os.path.join(path, "objects"))
    refs = _lstat_mode(os.path.join(path, "refs"))
    if head is not None and stat.S_ISREG(head) and objects is not None and stat.S_ISDIR(objects) \
            and refs is not None and stat.S_ISDIR(refs):
        return "bare"
    return None


def _query(path: str, args: Iterable[str], *, timeout: float = _QUERY_TIMEOUT,
           max_bytes: int = 64 * 1024, stop_at: float | None = None) -> tuple[int, str, bool]:
    """One read-only git query against the repository at PATH. The git dir and work tree are passed
    explicitly so git never walks up into a parent repository. STOP_AT (time.monotonic) bounds the
    whole scan: the call never runs past it."""
    if stop_at is not None:
        remaining = stop_at - time.monotonic()
        if remaining <= 0:
            raise WorktreeError("scan time limit reached")
        timeout = min(timeout, remaining)
    return _git(path, [f"--git-dir={os.path.join(path, '.git')}", f"--work-tree={path}", *args],
                check=False, timeout=timeout, max_bytes=max_bytes)


def inspect_repo(path: str | os.PathLike[str], *, stop_at: float | None = None) -> RepoInfo:
    """Describe one repository. Raises ValueError for bad input or a folder that is not a repository.
    STOP_AT (a time.monotonic value) is an overall deadline checked before and bounding every git call."""
    path = check_folder(path, "repository path")
    name = os.path.basename(path) or path
    marker = _repo_marker(path)
    base = RepoInfo(path=path, name=name, rel=name, kind="repo", usable=False, reason="")
    if marker is None:
        raise ValueError("not a git repository")
    if marker == "gitlink":
        return replace(base, kind="unsafe-gitdir", reason=".git is a symlink")
    if marker == "gitfile":
        return replace(base, kind="linked",
                       reason=".git is a file (linked worktree or submodule): register its main repository")
    if marker == "bare":
        return replace(base, kind="bare", reason="bare repository (no working tree)")
    if os.path.lexists(os.path.join(path, ".git", "commondir")):
        # git would read config, attributes, objects and refs from another directory.
        return replace(base, kind="unsafe-gitdir", reason="unsafe: .git/commondir redirects the repository")
    for p in (path, os.path.join(path, ".git")):
        if os.lstat(p).st_uid != os.getuid():
            return replace(base, reason="not owned by the current user")
    info = replace(base, shallow=os.path.lexists(os.path.join(path, ".git", "shallow")))
    if stop_at is not None and time.monotonic() >= stop_at:
        return replace(info, reason="not inspected: scan time limit reached")
    try:
        refuse_unsafe_repo_config(path)
    except WorktreeError as exc:
        # No further git call of any kind against a repository the daemon would refuse.
        return replace(info, reason=f"unsafe config: {exc}")
    try:
        return _inspect_gitdir(info, stop_at)
    except WorktreeError as exc:  # git timed out, could not be executed, or the scan ran out of time
        return replace(info, reason=f"git query failed: {exc}")


def _inspect_gitdir(info: RepoInfo, stop_at: float | None) -> RepoInfo:
    path = info.path
    rc, out, _ = _query(path, ["symbolic-ref", "-q", "--short", "HEAD"], stop_at=stop_at)
    branch = out.strip() if rc == 0 and out.strip() else None
    detached = rc == 1
    rc, out, _ = _query(path, ["rev-parse", "--verify", "-q", "--end-of-options", "HEAD^{commit}"],
                        stop_at=stop_at)
    head = out.strip() if rc == 0 and out.strip() else None
    rc, out, _ = _query(path, ["for-each-ref", f"--count={MAX_BRANCHES + 1}",
                               "--format=%(refname:strip=2)", "refs/heads/"], stop_at=stop_at)
    names = [n for n in out.split("\n") if n] if rc == 0 else []
    truncated_branches = len(names) > MAX_BRANCHES
    names = sorted(names[:MAX_BRANCHES])
    if branch is not None:
        names = [branch, *[n for n in names if n != branch]][:MAX_BRANCHES]
    count = None
    if head:
        rc, out, _ = _query(path, ["rev-list", "--count", f"--max-count={COMMIT_COUNT_CAP + 1}", head],
                            stop_at=stop_at)
        count = int(out.strip()) if rc == 0 and out.strip().isdigit() else None
    info = replace(info, branch=branch, detached=detached, head=head, branches=tuple(names),
                   branches_truncated=truncated_branches, commit_count=count)
    rc, out, out_truncated = _query(
        path, [f"--attr-source={_EMPTY_TREE}", "status", "--porcelain=v1", "-z", "--no-renames",
               "--ignore-submodules=all", "--untracked-files=normal"],
        timeout=_STATUS_TIMEOUT, max_bytes=_STATUS_MAX_BYTES, stop_at=stop_at)
    if rc == 0 or out_truncated:
        entries = sum(1 for e in out.split("\x00") if e)
        info = replace(info, dirty_count=min(entries, DIRTY_COUNT_CAP),
                       dirty_truncated=out_truncated or entries > DIRTY_COUNT_CAP)
    if head is None:
        return replace(info, reason="no commits yet (a worktree needs a committed base ref)")
    if info.dirty_count is None:
        return replace(info, reason="git status failed")
    return replace(info, usable=True)


@dataclass(frozen=True, slots=True)
class _Walk:
    root: str
    root_is_repo: bool
    candidates: tuple[str, ...]
    skipped: tuple[Skipped, ...]
    entries_seen: int
    truncated: bool
    depth_limited: bool
    ignored_dirs: int


def _walk(folder: str | os.PathLike[str], max_depth: int, max_entries: int, max_repos: int) -> _Walk:
    """The names-only part of discovery: no git call, no file opened."""
    root = check_folder(folder)
    if max_depth < 1 or max_entries < 1 or max_repos < 1:
        raise ValueError("max_depth, max_entries and max_repos must be >= 1")
    if _repo_marker(root) is not None:
        return _Walk(root, True, (root,), (), 0, False, False, 0)
    candidates: list[str] = []
    skipped: list[Skipped] = []
    seen = 0
    ignored = 0
    truncated = False
    depth_limited = False
    stack: list[tuple[str, int]] = [(root, 0)]
    while stack and not truncated:
        directory, depth = stack.pop()
        try:
            listing: list[os.DirEntry[str]] = []
            with os.scandir(directory) as it:
                for entry in it:
                    if seen >= max_entries:
                        truncated = True
                        break
                    seen += 1
                    listing.append(entry)
        except OSError:
            skipped.append(Skipped(directory, "unreadable"))
            continue
        subdirs: list[str] = []
        for entry in sorted(listing, key=lambda e: e.name):
            child = os.path.join(directory, entry.name)
            try:
                if entry.is_symlink():
                    reason = _symlink_reason(child)
                    if reason:
                        skipped.append(Skipped(child, reason))
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                skipped.append(Skipped(child, "unreadable"))
                continue
            if entry.name.startswith(".") or entry.name in IGNORED_DIRS:
                ignored += 1
                continue
            if has_control_chars(entry.name):
                skipped.append(Skipped(child, "name contains control characters"))
                continue
            if _repo_marker(child) is not None:
                if len(candidates) >= max_repos:
                    truncated = True
                    break
                candidates.append(child)  # never descend into a repository
            elif depth + 1 < max_depth:
                subdirs.append(child)
            else:
                depth_limited = True
        stack.extend((d, depth + 1) for d in reversed(subdirs))
    return _Walk(root, False, tuple(sorted(candidates)), tuple(skipped), seen, truncated, depth_limited, ignored)


def _scan(walk: _Walk, wanted, deadline_seconds: float) -> WorkspaceScan:
    stop_at = time.monotonic() + deadline_seconds
    repos: list[RepoInfo] = []
    skipped = list(walk.skipped)
    truncated = walk.truncated
    for c in walk.candidates:
        rel = "." if walk.root_is_repo else os.path.relpath(c, walk.root)
        if not wanted(rel, c):
            continue
        try:
            info = replace(inspect_repo(c, stop_at=stop_at), rel=rel)
        except ValueError as exc:
            skipped.append(Skipped(c, str(exc)))
            continue
        truncated = truncated or info.reason.endswith("scan time limit reached")
        repos.append(info)
    return WorkspaceScan(root=walk.root, root_is_repo=walk.root_is_repo, repos=tuple(repos),
                         skipped=tuple(skipped), entries_seen=walk.entries_seen, truncated=truncated,
                         depth_limited=walk.depth_limited, ignored_dirs=walk.ignored_dirs)


def discover_repos(folder: str | os.PathLike[str], *, max_depth: int = 2, max_entries: int = 2000,
                   max_repos: int = 64, deadline_seconds: float = 300.0) -> WorkspaceScan:
    """Find and inspect the repositories under FOLDER (see the module docstring for what is touched).
    DEADLINE_SECONDS bounds all git queries together: every call is cut to the remaining time, except the
    unsafe-config check, which keeps its own 60 s timeout; repositories not reached are reported."""
    return _scan(_walk(folder, max_depth, max_entries, max_repos), lambda rel, path: True, deadline_seconds)


def pick_repos(folder: str | os.PathLike[str], names: Iterable[str], *, max_depth: int = 2,
               max_entries: int = 2000, max_repos: int = 64, deadline_seconds: float = 300.0) -> list[RepoInfo]:
    """Registration path: walk FOLDER by names only, run git queries ONLY on the repositories named, then
    apply ``select_repos`` (refuses unknown, outside-the-folder and unusable entries)."""
    names = list(names)
    walk = _walk(folder, max_depth, max_entries, max_repos)
    keys = set()
    for raw in names:
        if isinstance(raw, str) and raw and not has_control_chars(raw):
            keys.add(os.path.normpath(raw))
    return select_repos(_scan(walk, lambda rel, path: rel in keys or path in keys, deadline_seconds), names)


def _symlink_reason(path: str) -> str | None:
    """Report symlinks that lead to a directory (or loop); symlinks to files are not interesting."""
    try:
        target = os.stat(path)  # stat only: the link target is never listed or opened
    except OSError as exc:
        return "symlink loop (not followed)" if exc.errno == errno.ELOOP else None
    return "symlink to a directory (not followed)" if stat.S_ISDIR(target.st_mode) else None


def select_repos(scan: WorkspaceScan, names: Iterable[str]) -> list[RepoInfo]:
    """Pick repositories from SCAN by their relative name (as shown in the table) or absolute path.

    Refuses anything not found by the scan (including paths outside the scanned folder) and any repository
    that is not usable. Order follows NAMES; duplicates are dropped."""
    by_rel = {r.rel: r for r in scan.repos}
    by_path = {r.path: r for r in scan.repos}
    chosen: list[RepoInfo] = []
    for raw in names:
        if not isinstance(raw, str) or not raw or has_control_chars(raw):
            raise ValueError("repository names must be non-empty and free of control characters")
        if os.path.isabs(raw):
            info = by_path.get(os.path.normpath(raw))
        else:
            norm = os.path.normpath(raw)
            if norm == ".." or norm.startswith(".." + os.sep):
                raise ValueError(f"{raw!r} is outside the scanned folder {scan.root}")
            info = by_rel.get(norm)
        if info is None:
            raise ValueError(f"{raw!r} is not a repository found in {scan.root}")
        if os.path.commonpath([scan.root, info.path]) != scan.root:
            raise ValueError(f"{raw!r} is outside the scanned folder {scan.root}")
        if not info.usable:
            raise ValueError(f"{info.rel!r} cannot be registered: {info.reason}")
        if info not in chosen:
            chosen.append(info)
    if not chosen:
        raise ValueError("no repository selected")
    return chosen
