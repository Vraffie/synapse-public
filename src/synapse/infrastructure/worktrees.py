"""Per-task git worktree manager (isolated checkouts for real agents).

Trust model: same OS user, one trust domain, but repository contents and
config are treated as untrusted input. See docs/operations/worktrees.md.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from synapse.domain.contracts import validate_id

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_REF = 256
MAX_COMMIT_MESSAGE = 2000
MAX_COMMIT_PATHS = 1000
_COMMIT_LISTING_BYTES = 4 * 1024 * 1024
_GIT_TIMEOUT = 60.0
DEFAULT_MAX_BYTES = 64 * 1024
# Repo-local config keys that make git run repo-chosen executables during
# checkout (clean/smudge filters) or via include indirection. Refused outright.
_DANGEROUS_CONFIG = re.compile(
    r"^(filter\..+\.(smudge|clean|process)|include\.path|includeif\..+\.path"
    r"|core\.(hookspath|sshcommand|gitproxy|attributesfile|askpass|editor)"
    r"|extensions\.worktreeconfig|diff\.external|diff\..+\.(command|textconv)"
    r"|merge\..+\.driver|credential\..*helper|gpg\..*program"
    # Keys that name a program git runs for transports, lazy fetch or alternates.
    r"|remote\..+\.(uploadpack|receivepack|vcs|proxy)|core\.alternaterefscommand"
    r"|uploadpack\..+|http\..*proxy)$")

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


class WorktreeError(RuntimeError):
    """Operation refused or git failed."""


class RepoNotAllowlistedError(ValueError):
    """The requested repository is not on the operator's allowlist (fail closed).

    Subclasses ``ValueError`` so existing callers that map bad input to a client error keep working.
    """


@dataclass(frozen=True, slots=True)
class WorktreeStatus:
    clean: bool
    head: str
    branch: str
    changed_paths: int
    diff_stat: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class BoundedText:
    text: str
    truncated: bool


def _safe_id(value: str, what: str) -> str:
    validate_id(value)
    if not _SAFE_ID.match(value) or ".." in value or value.endswith((".lock", ".")):
        raise ValueError(f"{what} must match [A-Za-z0-9][A-Za-z0-9._-]* and be a safe path/ref component")
    return value


def _check_dir(path: str, what: str) -> str:
    if not isinstance(path, str) or not path or "\x00" in path or not os.path.isabs(path):
        raise ValueError(f"{what} must be an absolute path")
    if os.path.normpath(path) != path:
        raise ValueError(f"{what} must be normalized")
    try:
        resolved = os.path.realpath(path, strict=True)
    except OSError as exc:
        raise ValueError(f"{what} does not exist") from exc
    if resolved != path:
        raise ValueError(f"{what} must not traverse symlinks")
    st = os.stat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise ValueError(f"{what} must be a directory owned by the current user")
    if path == os.sep:
        raise ValueError(f"{what} must not be the filesystem root")
    return path


def _git_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ASKPASS": "/bin/false",
        # Never lazily fetch missing objects of a partial clone: that would run the repo-chosen
        # remote.<name>.uploadpack (or a transport) from a read-only query such as `status`.
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_AUTHOR_NAME": "synapse", "GIT_AUTHOR_EMAIL": "synapse@localhost",
        "GIT_COMMITTER_NAME": "synapse", "GIT_COMMITTER_EMAIL": "synapse@localhost",
    }
    return env


# Every call also starts with the global option ``--no-lazy-fetch`` (same effect as GIT_NO_LAZY_FETCH,
# see ``git help git``). A git too old to know it rejects the option, so every call then fails closed
# instead of silently allowing a lazy fetch.
_HARDEN = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
           "-c", "core.pager=cat", "-c", "gc.auto=0", "-c", "protocol.ext.allow=never",
           "-c", "core.attributesFile=/dev/null", "-c", "diff.external=",
           "-c", "core.untrackedCache=false")
# Prefix for every git call that reads a working tree: attributes come from an empty tree
# (the work tree's .gitattributes is ignored). $GIT_DIR/info/attributes cannot be overridden
# and is refused by ``_refuse_unsafe_repo`` instead.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _git(cwd: str, args: Iterable[str], *, max_bytes: int = DEFAULT_MAX_BYTES,
         check: bool = True, timeout: float | None = None) -> tuple[int, str, bool]:
    """Run git (argv list, no shell, scrubbed env). Output is bounded and the whole call has a
    wall-clock deadline enforced by reader threads; on expiry the entire process group dies."""
    args = list(args)
    deadline = time.monotonic() + (_GIT_TIMEOUT if timeout is None else timeout)
    argv = ["git", "--no-lazy-fetch", *_HARDEN, *args]
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=_git_env(), shell=False,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        raise WorktreeError("cannot execute git") from exc
    out = bytearray()
    err = bytearray()
    overflow = threading.Event()

    def read_out() -> None:
        try:
            while True:
                chunk = proc.stdout.read1(65536)  # type: ignore[union-attr]
                if not chunk:
                    return
                room = max_bytes + 1 - len(out)
                out.extend(chunk[:room])
                if len(out) > max_bytes:
                    overflow.set()
                    return
        except (OSError, ValueError):
            return

    def read_err() -> None:
        try:
            while True:
                chunk = proc.stderr.read1(65536)  # type: ignore[union-attr]
                if not chunk:
                    return
                if len(err) < 4096:
                    err.extend(chunk[:4096 - len(err)])
        except (OSError, ValueError):
            return

    threads = [threading.Thread(target=read_out, daemon=True),
               threading.Thread(target=read_err, daemon=True)]
    for t in threads:
        t.start()
    timed_out = False
    truncated = False
    try:
        threads[0].join(max(0.0, deadline - time.monotonic()))
        if threads[0].is_alive():
            timed_out = True
        elif overflow.is_set():
            truncated = True
            _kill_group(proc)
        if not timed_out:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
        if timed_out:
            _kill_group(proc)
    finally:
        if proc.poll() is None:
            _kill_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for t in threads:
            t.join(2)
        for f in (proc.stdout, proc.stderr):
            if f:
                try:
                    f.close()
                except OSError:
                    pass
    if timed_out:
        raise WorktreeError("git timed out")
    data = bytes(out[:max_bytes])
    rc = proc.returncode
    if check and rc != 0 and not truncated:
        msg = bytes(err).decode("utf-8", "replace").strip().splitlines()[:1]
        raise WorktreeError(f"git {args_name(args)} failed: {msg[0] if msg else rc}")
    return rc, data.decode("utf-8", "replace"), truncated


def args_name(args: Iterable[str]) -> str:
    for a in args:
        if not a.startswith("-"):
            return a
    return "?"


def refuse_unsafe_repo_config(repo: str, cwd: str | None = None) -> None:
    """Refuse repo state that makes git run repo-chosen programs. Re-run before every git call
    that touches a work tree (linked worktrees share this config with the agent's shell).
    Shared with ``synapse.application.workspace`` so discovery applies the identical rule."""
    dotgit = os.path.join(repo, ".git")
    # .git/commondir makes git take config, info/attributes, objects and refs from ANOTHER directory,
    # so every check below would look at the wrong files. Refused outright (fail closed).
    if os.path.lexists(os.path.join(dotgit, "commondir")):
        raise WorktreeError("repository .git/commondir is not allowed")
    # The effective repository config as git itself resolves it for this git dir (explicit --git-dir:
    # no discovery walk-up; --local lists without following includes, which are refused below). A
    # listing that does not fit the cap is refused rather than checked partially.
    _, out, truncated = _git(cwd or repo, [f"--git-dir={dotgit}", "config", "--local", "--list", "-z"],
                             max_bytes=1 << 20)
    if truncated:
        raise WorktreeError("repository config is too large to check")
    for entry in out.split("\x00"):
        key, _, value = entry.partition("\n")
        key = key.lower()
        if _DANGEROUS_CONFIG.match(key):
            raise WorktreeError(f"repository config {key!r} is not allowed")
        if key == "core.fsmonitor" and value.strip().lower() not in ("false", "0", "no", "off"):
            raise WorktreeError("repository config 'core.fsmonitor' is not allowed")
        if key.startswith("alias.") and value.lstrip().startswith("!"):
            raise WorktreeError(f"repository config {key!r} is not allowed")
    if os.path.lexists(os.path.join(repo, ".git", "config.worktree")):
        raise WorktreeError("repository config.worktree is not allowed")
    info = os.path.join(repo, ".git", "info", "attributes")
    if os.path.lexists(info):
        try:
            if os.path.islink(info):
                raise WorktreeError("repository info/attributes is not allowed")
            with open(info, "rb") as fh:
                lines = fh.read(1 << 20).decode("utf-8", "replace").splitlines()
        except OSError as exc:
            raise WorktreeError("repository info/attributes is not readable") from exc
        if any(ln.strip() and not ln.lstrip().startswith("#") for ln in lines):
            raise WorktreeError("repository info/attributes is not allowed")


class WorktreeManager:
    def __init__(self, repo_allowlist: Iterable[str], root_dir: str) -> None:
        repos = list(repo_allowlist)
        if not repos:
            raise ValueError("repo_allowlist must not be empty")
        self._repos: frozenset[str] = frozenset(self._check_repo(r) for r in repos)
        ordered = sorted(self._repos)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                if Path(a) in Path(b).parents or Path(b) in Path(a).parents:
                    raise ValueError("allowlisted repositories must not be nested inside each other")
        if not os.path.isabs(root_dir) or os.path.normpath(root_dir) != root_dir:
            raise ValueError("root_dir must be absolute and normalized")
        os.makedirs(root_dir, mode=0o700, exist_ok=True)
        self._root = _check_dir(root_dir, "root_dir")
        for r in self._repos:
            p, q = Path(r), Path(self._root)
            if p == q or p in q.parents or q in p.parents:
                raise ValueError("root_dir must not overlap an allowlisted repository")
        os.chmod(self._root, 0o700)

    @staticmethod
    def _check_repo(repo: str) -> str:
        _check_dir(repo, "repo")
        dotgit = os.path.join(repo, ".git")
        if os.path.islink(dotgit) or not os.path.isdir(dotgit):
            raise ValueError("repo must contain a real .git directory")
        if os.path.lexists(os.path.join(dotgit, "commondir")):
            raise ValueError("repo .git/commondir is not allowed")
        return repo

    # --- paths ------------------------------------------------------------
    def _path(self, run_id: str, task_id: str) -> str:
        return os.path.join(self._root, _safe_id(run_id, "run_id"), _safe_id(task_id, "task_id"))

    def worktree_path(self, run_id: str, task_id: str) -> str:
        """The directory ``status``/``diff`` inspect for this (run, task)."""
        return self._path(run_id, task_id)

    @staticmethod
    def _branch(run_id: str, task_id: str) -> str:
        return f"synapse/{run_id}/{task_id}"

    def _contained(self, path: str) -> None:
        if os.path.dirname(os.path.dirname(path)) != self._root:
            raise WorktreeError("path outside root_dir")
        for p in (os.path.dirname(path), path):
            if os.path.islink(p):
                raise WorktreeError("symlink inside root_dir")

    def _lock(self, repo: str):
        with _locks_guard:
            tl = _locks.setdefault(repo, threading.Lock())
        return _RepoLock(tl, os.path.join(self._root, f".lock-{hashlib.sha256(repo.encode()).hexdigest()[:16]}"))

    def _repo_of(self, path: str) -> str:
        """Resolve owning allowlisted repo from the worktree's .git file."""
        dotgit = os.path.join(path, ".git")
        if os.path.islink(dotgit) or not os.path.isfile(dotgit):
            raise WorktreeError("not a linked worktree")
        with open(dotgit, "rb") as fh:
            line = fh.read(4096).decode("utf-8", "replace").strip()
        if not line.startswith("gitdir: "):
            raise WorktreeError("malformed .git file")
        gitdir = os.path.normpath(line[len("gitdir: "):])
        wt_meta = os.path.dirname(gitdir)
        if os.path.basename(wt_meta) != "worktrees":
            raise WorktreeError("worktree does not belong to an allowlisted repository")
        repo = os.path.dirname(os.path.dirname(wt_meta))
        if repo not in self._repos:
            raise WorktreeError("worktree does not belong to an allowlisted repository")
        try:
            with open(os.path.join(gitdir, "gitdir"), "rb") as fh:
                back = fh.read(4096).decode("utf-8", "replace").strip()
        except OSError as exc:
            raise WorktreeError("worktree metadata missing") from exc
        if back != dotgit:
            raise WorktreeError("worktree backlink mismatch")
        return repo

    def _refuse_dangerous_config(self, repo: str, cwd: str | None = None) -> None:
        refuse_unsafe_repo_config(repo, cwd)

    def _tree_git(self, repo: str, path: str, args: Iterable[str], *, max_bytes: int = DEFAULT_MAX_BYTES,
                  check: bool = True) -> tuple[int, str, bool]:
        """git against a work tree: unsafe-config refusal first, then attributes pinned to an
        empty tree so no .gitattributes filter/diff driver can apply."""
        self._refuse_dangerous_config(repo, path)
        wt_meta = self._gitdir_of(path)
        if os.path.lexists(os.path.join(wt_meta, "config.worktree")):
            raise WorktreeError("repository config.worktree is not allowed")
        return _git(path, [f"--attr-source={_EMPTY_TREE}", *args], max_bytes=max_bytes, check=check)

    @staticmethod
    def _gitdir_of(path: str) -> str:
        with open(os.path.join(path, ".git"), "rb") as fh:
            line = fh.read(4096).decode("utf-8", "replace").strip()
        return os.path.normpath(line[len("gitdir: "):])

    # --- operations -------------------------------------------------------
    def _resolve_base(self, repo: str, base_ref: str) -> str:
        """Read-only: validate the repo/ref and return the commit SHA (raises ValueError)."""
        if not isinstance(repo, str) or repo not in self._repos:
            raise RepoNotAllowlistedError("repo is not on the operator allowlist")
        self._check_repo(repo)
        if (not isinstance(base_ref, str) or not base_ref or len(base_ref) > _MAX_REF
                or base_ref.startswith("-") or "\x00" in base_ref or "\n" in base_ref):
            raise ValueError("invalid base_ref")
        rc, out, _ = _git(repo, ["rev-parse", "--verify", "--quiet", "--end-of-options",
                                 f"{base_ref}^{{commit}}"], check=False)
        sha = out.strip()
        if rc != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ValueError("base_ref does not resolve to a commit")
        return sha

    def check_base(self, repo: str, base_ref: str) -> str:
        """Side-effect-free pre-flight for ``create``: allowlist, ref syntax, no dangerous
        repo config and the ref resolves. Returns the commit SHA; raises ValueError/WorktreeError."""
        sha = self._resolve_base(repo, base_ref)
        self._refuse_dangerous_config(repo)
        return sha

    def create(self, run_id: str, task_id: str, repo: str, base_ref: str) -> str:
        path = self._path(run_id, task_id)
        if not isinstance(repo, str) or repo not in self._repos:
            raise RepoNotAllowlistedError("repo is not on the operator allowlist")
        self._check_repo(repo)
        if (not isinstance(base_ref, str) or not base_ref or len(base_ref) > _MAX_REF
                or base_ref.startswith("-") or "\x00" in base_ref or "\n" in base_ref):
            raise ValueError("invalid base_ref")
        branch = self._branch(run_id, task_id)
        with self._lock(repo):
            if os.path.lexists(path):
                self._contained(path)
                if self._repo_of(path) != repo:
                    raise WorktreeError("worktree path exists for a different repository")
                _, cur, _ = _git(path, ["symbolic-ref", "--short", "HEAD"])
                if cur.strip() != branch:
                    raise WorktreeError("existing worktree is on an unexpected branch")
                return path  # idempotent
            self._refuse_dangerous_config(repo)
            sha = self._resolve_base(repo, base_ref)
            parent = os.path.dirname(path)
            os.makedirs(parent, mode=0o700, exist_ok=True)
            self._contained(path)
            _git(repo, ["worktree", "prune"], check=False)
            rc, _, _ = _git(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                            check=False)
            if rc == 0:  # branch left over from a removed worktree: resume it
                _git(repo, ["worktree", "add", path, branch])
            else:
                _git(repo, ["worktree", "add", "-b", branch, path, sha])
        return path

    def _existing(self, run_id: str, task_id: str) -> tuple[str, str] | None:
        path = self._path(run_id, task_id)
        if not os.path.lexists(path):
            return None
        self._contained(path)
        return path, self._repo_of(path)

    def status(self, run_id: str, task_id: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> WorktreeStatus:
        found = self._existing(run_id, task_id)
        if found is None:
            raise WorktreeError("worktree does not exist")
        path, repo = found
        _, head, _ = self._tree_git(repo, path, ["rev-parse", "HEAD"])
        rc, br, _ = self._tree_git(repo, path, ["symbolic-ref", "--short", "-q", "HEAD"], check=False)
        _, porc, t1 = self._tree_git(repo, path, ["status", "--porcelain=v1", "-z", "--untracked-files=normal"],
                           max_bytes=max_bytes)
        _, stat_out, t2 = self._tree_git(repo, path, ["diff", "--stat", "--no-ext-diff", "--no-textconv",
                                      "--no-color", "HEAD", "--"], max_bytes=max_bytes)
        n = len([e for e in porc.split("\x00") if e])
        return WorktreeStatus(not porc, head.strip(), br.strip() if rc == 0 else "",
                              n, stat_out, t1 or t2)

    def diff(self, run_id: str, task_id: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> BoundedText:
        found = self._existing(run_id, task_id)
        if found is None:
            raise WorktreeError("worktree does not exist")
        _, out, trunc = self._tree_git(found[1], found[0], ["diff", "--no-ext-diff", "--no-textconv", "--no-color",
                                        "HEAD", "--"], max_bytes=max_bytes)
        return BoundedText(out, trunc)

    def commit(self, run_id: str, task_id: str, message: str) -> dict:
        """Host-side commit of everything in the task's worktree onto the task's own branch.

        The agent cannot commit from inside its sandbox, so this is how a finished run's edits become a
        reviewable commit. It never touches the owner's checkout or any other branch and never pushes.
        Same hardening as every other tree operation (hooks off, config guard, attributes pinned) plus:
        a bounded message and path count, no gitlinks (nested repositories), no signing, no ``--no-verify``
        reliance (hooks are disabled for all host git). Returns {"head_before","head","branch","paths"}.
        """
        if (not isinstance(message, str) or not message.strip() or len(message) > MAX_COMMIT_MESSAGE
                or "\x00" in message):
            raise ValueError("invalid commit message")
        found = self._existing(run_id, task_id)
        if found is None:
            raise WorktreeError("worktree does not exist")
        path, repo = found
        with self._lock(repo):
            _, head_before, _ = self._tree_git(repo, path, ["rev-parse", "HEAD"])
            rc, branch, _ = self._tree_git(repo, path, ["symbolic-ref", "--short", "-q", "HEAD"], check=False)
            if rc != 0 or branch.strip() != self._branch(run_id, task_id):
                raise WorktreeError("worktree is not on the task branch")
            _, porc, trunc = self._tree_git(repo, path, ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
                                            max_bytes=_COMMIT_LISTING_BYTES)
            entries = [e for e in porc.split("\x00") if e]
            if not entries:
                raise WorktreeError("nothing to commit")
            if trunc or len(entries) > MAX_COMMIT_PATHS:
                raise WorktreeError("too many changed paths to commit")
            try:
                self._tree_git(repo, path, ["add", "-A", "--"])
                # Judge what is STAGED, not what status showed: status folds directories and the worktree's
                # agent-written .gitmodules can change how diff presents a gitlink, so ignore it explicitly.
                _, raw, raw_trunc = self._tree_git(repo, path, ["diff", "--cached", "--raw", "-z", "--no-renames",
                                                                "--no-ext-diff", "--no-textconv",
                                                                "--ignore-submodules=none"],
                                                   max_bytes=_COMMIT_LISTING_BYTES)
                if raw_trunc:
                    raise WorktreeError("too many changed paths to commit")
                tokens = raw.split("\x00")
                records = [(t.split(" "), tokens[i + 1] if i + 1 < len(tokens) else "")
                           for i, t in enumerate(tokens) if t.startswith(":")]
                if len(records) > MAX_COMMIT_PATHS:
                    raise WorktreeError("too many changed paths to commit")
                if any("160000" in fields[:2] for fields, _ in records):
                    raise WorktreeError("a nested repository (gitlink) cannot be committed")
                if any(os.path.basename(name) == ".gitmodules" for _, name in records):
                    raise WorktreeError("a .gitmodules change cannot be committed")
                self._tree_git(repo, path, ["-c", "commit.gpgsign=false", "-c", "user.useConfigOnly=false",
                                            "commit", "--no-verify", "--no-gpg-sign", "-q", "--allow-empty-message",
                                            "-m", message.strip()])
            except BaseException:
                self._tree_git(repo, path, ["reset", "-q", "--"], check=False)  # leave the tree as the agent left it
                raise
            _, head, _ = self._tree_git(repo, path, ["rev-parse", "HEAD"])
        return {"head_before": head_before.strip(), "head": head.strip(), "branch": branch.strip(),
                "paths": len(records)}

    # --- read-only candidate facts (used by the candidate manifest) --------
    def worktree_repo(self, run_id: str, task_id: str) -> str | None:
        """The allowlisted repository that owns this task's worktree, or None if it does not exist."""
        found = self._existing(run_id, task_id)
        return None if found is None else found[1]

    def descends_from(self, run_id: str, task_id: str, sha: str) -> bool:
        """True if commit ``sha`` is an ancestor of (or equal to) the worktree's HEAD."""
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ValueError("sha must be a full lowercase hex commit id")
        found = self._existing(run_id, task_id)
        if found is None:
            raise WorktreeError("worktree does not exist")
        rc, _, _ = self._tree_git(found[1], found[0], ["merge-base", "--is-ancestor", sha, "HEAD"], check=False)
        if rc not in (0, 1):
            raise WorktreeError("git could not compare the commits")
        return rc == 0

    def diff_since(self, run_id: str, task_id: str, sha: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> BoundedText:
        """Bounded committed diff ``sha``..HEAD (tracked content; no external diff/textconv)."""
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ValueError("sha must be a full lowercase hex commit id")
        found = self._existing(run_id, task_id)
        if found is None:
            raise WorktreeError("worktree does not exist")
        _, out, trunc = self._tree_git(found[1], found[0], ["diff", "--no-ext-diff", "--no-textconv", "--no-color",
                                                           sha, "HEAD", "--"], max_bytes=max_bytes)
        return BoundedText(out, trunc)

    def remove(self, run_id: str, task_id: str, force: bool = False) -> bool:
        """Remove the worktree. Returns False if it was already gone."""
        path = self._path(run_id, task_id)
        if not os.path.lexists(path):
            return False
        self._contained(path)
        repo = self._repo_of(path)
        branch = self._branch(run_id, task_id)
        with self._lock(repo):
            if not os.path.lexists(path):
                return False
            if not force:
                _, porc, _ = self._tree_git(repo, path, ["status", "--porcelain=v1", "-z",
                                                         "--untracked-files=normal"])
                if porc:
                    raise WorktreeError("worktree has uncommitted changes (use force)")
            args = ["worktree", "remove", path]
            if force:
                args.insert(2, "--force")
            _git(repo, args)
            if os.path.lexists(path):  # defensive; git removed it already
                shutil.rmtree(path)
            _git(repo, ["branch", "-D" if force else "-d", "--", branch], check=False)
            try:
                os.rmdir(os.path.dirname(path))
            except OSError:
                pass
        return True


class _RepoLock:
    def __init__(self, tlock: threading.Lock, lockfile: str) -> None:
        self._t, self._file, self._fd = tlock, lockfile, -1

    def __enter__(self):
        self._t.acquire()
        try:
            self._fd = os.open(self._file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except BaseException:
            if self._fd >= 0:
                os.close(self._fd)
            self._t.release()
            raise

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
        finally:
            self._t.release()
