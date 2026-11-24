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
from collections.abc import Iterable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from synapse.application.xml_wellformed import MAX_XML_BYTES, XmlRefused, check_xml
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
    r"|uploadpack\..+|http\..*proxy"
    # Config-defined hooks (git 2.54+: hook.<name>.command/event) bypass core.hooksPath=/dev/null.
    r"|hook\..+|gpg\.ssh\.defaultkeycommand)$")

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


class WorktreeError(RuntimeError):
    """Operation refused or git failed."""


class CommitRefused(WorktreeError):
    """The host-side commit refused the staged change (fixed ``reason`` code; ``count`` paths, never their names)."""

    def __init__(self, reason: str, count: int = 0) -> None:
        super().__init__("commit refused: %s" % reason)
        self.reason, self.count = reason, count


class AttemptCreateError(WorktreeError):
    """Creating an attempt's worktrees failed for ``repo_id``; every worktree made for the attempt was removed."""

    def __init__(self, repo_id: str) -> None:
        super().__init__("worktree could not be created for %s" % repo_id)
        self.repo_id = repo_id


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


# --- multi-repository runs (ADR 026 slice 1) ---------------------------------------------------------------------
MAX_ATTEMPT_REPOS = 16
MAX_ATTEMPT_NO = 10_000
MAX_PROTECTED_PATTERNS = 64
_MAX_PATTERN = 256
_HEX_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# Owner decision: in a multi-repository run an agent may change only XML files (``**/*.xml``); a staged path with any
# other name refuses the whole commit for that repository. Regular files only (no symlinks, no gitlinks).
EDITABLE_SUFFIX = ".xml"
_EDITABLE_MODES = frozenset({"100644", "000000"})  # new mode: a regular non-executable file, or deleted


def check_registry_id(value) -> str:
    """A registry repository id: the safe-id rule (a path component and a ref-safe name)."""
    if not isinstance(value, str):
        raise ValueError("repository id must be a string")
    return _safe_id(value, "repository id")


def check_protected_patterns(patterns) -> tuple[str, ...]:
    """Validate operator ``protected_paths``: anchored, repository-relative globs.

    Grammar (a subset of git's ``:(glob)`` pathspec magic): ``/``-separated segments; ``*`` and ``?`` match within one
    segment; a segment that is exactly ``**`` matches any number of segments. No character classes, no negation, no
    leading ``/``, no empty, ``.`` or ``..`` segments. A pattern protects the paths it matches and everything below a
    directory it matches (``.github`` protects ``.github/workflows/x.yml``).
    """
    if not isinstance(patterns, (list, tuple)) or len(patterns) > MAX_PROTECTED_PATTERNS:
        raise ValueError("must be a list of at most %d patterns" % MAX_PROTECTED_PATTERNS)
    out = []
    for pat in patterns:
        if (not isinstance(pat, str) or not pat or len(pat) > _MAX_PATTERN or pat.startswith("/")
                or any(c in pat for c in "\x00\\[]!\n\r") or pat != pat.strip()):
            raise ValueError("each pattern must be a nonempty relative glob without [ ] ! or backslash")
        for seg in pat.split("/"):
            if seg in ("", ".", "..") or ("**" in seg and seg != "**"):
                raise ValueError("pattern segments must not be empty, '.' or '..', and ** must be a whole segment")
        out.append(pat)
    if len(set(out)) != len(out):
        raise ValueError("patterns must not repeat")
    return tuple(out)


def _pattern_regex(pattern: str) -> re.Pattern:
    segs = pattern.split("/")
    parts = []
    for i, seg in enumerate(segs):
        last = i == len(segs) - 1
        if seg == "**":
            parts.append(".*" if last else "(?:[^/]+/)*")
            continue
        body = "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in seg)
        parts.append(body if last else body + "/")
    return re.compile("".join(parts), re.DOTALL)


def matches_protected(path: str, patterns: Sequence[str]) -> bool:
    """True if repository-relative ``path`` or any of its parent directories matches a protected pattern."""
    if not patterns:
        return False
    segs = path.split("/")
    prefixes = ["/".join(segs[:i]) for i in range(1, len(segs) + 1)]
    for pat in patterns:
        rx = _pattern_regex(pat)
        if any(rx.fullmatch(prefix) for prefix in prefixes):
            return True
    return False


def check_staged_paths(records: Sequence[tuple[Sequence[str], str]], protected: Sequence[str]) -> None:
    """The multi-repository path and mode rule (ADR 026 D3 rules 1 and 3, plus the operator's protected paths) over
    ``git diff --cached --raw -z --no-renames`` records. Raises ``CommitRefused`` (a fixed reason and a count):

    * ``path_not_editable``: not ``**/*.xml`` (case-sensitive), or not a plain relative path (empty, ``.``/``..``/
      ``.git`` segment, leading ``/`` or ``-``, backslash or control character);
    * ``path_not_allowed``: matches a protected pattern (protected paths only ever narrow the rule);
    * ``mode_not_allowed``: an added or modified entry that is not a regular non-executable file (``100644``):
      symlinks, executables and gitlinks are refused; a deletion is a deletion.

    Checked in that order, over every record, before anything is committed. Well-formedness (rule 4) is checked on
    the staged blobs by the caller.
    """
    def refuse(reason: str, bad: list) -> None:
        if bad:
            raise CommitRefused(reason, len(bad))

    modes = [(f[0].lstrip(":") if f else "", f[1] if len(f) > 1 else "", name) for f, name in records]
    refuse("path_not_editable", [n for _, _, n in modes
                                 if not n or n.startswith(("/", "-")) or "\\" in n
                                 or any(ord(c) < 32 or ord(c) == 127 for c in n)
                                 or any(seg in ("", ".", "..", ".git") for seg in n.split("/"))
                                 or not n.endswith(EDITABLE_SUFFIX)])
    refuse("path_not_allowed", [n for _, _, n in modes if matches_protected(n, protected)])
    refuse("mode_not_allowed", [n for _, m, n in modes if m not in _EDITABLE_MODES])


@dataclass(frozen=True, slots=True)
class AttemptRepo:
    """One repository of an attempt: where it lives, the pinned baseline commit and the task's access mode."""
    repo_id: str
    repo_path: str
    baseline_sha: str
    access: str  # "write" | "read"


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
        # Object identity must be what is stored: refs/replace/* and info/grafts could make a check see different
        # (clean) objects than the ones a commit or deploy actually uses (ADR 026 re-review).
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": "/dev/null",
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
           "-c", "core.untrackedCache=false", "-c", "core.excludesFile=/dev/null")
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


def _git_raw(cwd: str, args: Iterable[str], *, max_bytes: int = DEFAULT_MAX_BYTES,
             check: bool = True, timeout: float | None = None) -> tuple[int, bytes, bool]:
    """Run git (argv list, no shell, scrubbed env). Output is bounded and the whole call has a
    wall-clock deadline enforced by reader threads; on expiry the entire process group dies."""
    args = list(args)
    deadline = time.monotonic() + (_GIT_TIMEOUT if timeout is None else timeout)
    argv = ["git", "--no-lazy-fetch", "--no-replace-objects", *_HARDEN, *args]
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
    return rc, data, truncated


def _git(cwd: str, args: Iterable[str], *, max_bytes: int = DEFAULT_MAX_BYTES,
         check: bool = True, timeout: float | None = None) -> tuple[int, str, bool]:
    rc, data, truncated = _git_raw(cwd, args, max_bytes=max_bytes, check=check, timeout=timeout)
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


class AttemptWorktrees:
    """Mixin: per-(attempt, repository) worktrees for multi-repository runs (ADR 026 D4, slice 1).

    Layout ``<root>/<run_id>/<task_id>/<attempt_no>/<repo_id>``. A write-access repository gets the Synapse branch
    ``synapse/<run_id>/<task_id>/<attempt_no>``; a read-access one is a detached checkout of the pinned baseline.
    Nothing here writes a ref outside ``refs/heads/synapse/``, pushes, fetches or touches the owner's checkout.
    """

    # --- layout ------------------------------------------------------------------------------------------------
    def attempt_dir(self, run_id: str, task_id: str, attempt_no: int) -> str:
        if type(attempt_no) is not int or not 1 <= attempt_no <= MAX_ATTEMPT_NO:
            raise ValueError("attempt_no out of range")
        return os.path.join(self._root, _safe_id(run_id, "run_id"), _safe_id(task_id, "task_id"), str(attempt_no))

    def attempt_worktree_path(self, run_id: str, task_id: str, attempt_no: int, repo_id: str) -> str:
        return os.path.join(self.attempt_dir(run_id, task_id, attempt_no), check_registry_id(repo_id))

    @staticmethod
    def attempt_branch(run_id: str, task_id: str, attempt_no: int) -> str:
        return f"synapse/{run_id}/{task_id}/{attempt_no}"

    @staticmethod
    def _guard_ref(branch: str) -> str:
        """The single ref-write guard: Synapse creates, moves or deletes branches only under refs/heads/synapse/."""
        if not isinstance(branch, str) or not branch.startswith("synapse/") or ".." in branch or "\x00" in branch:
            raise WorktreeError("refusing to write a ref outside refs/heads/synapse/")
        return branch

    def _contained_attempt(self, path: str) -> None:
        """``path`` is exactly <root>/<run>/<task>/<attempt>/<repo> and no level below root is a symlink."""
        cur = path
        for _ in range(4):
            if os.path.islink(cur):
                raise WorktreeError("symlink inside root_dir")
            cur = os.path.dirname(cur)
        if cur != self._root:
            raise WorktreeError("path outside root_dir")

    def _mkdirs_private(self, directory: str) -> None:
        """Create each level under root (0700) one at a time, refusing a symlink or foreign directory at any level
        (``os.makedirs`` would follow a planted symlink)."""
        rel = os.path.relpath(directory, self._root)
        if rel.startswith("..") or os.path.isabs(rel):
            raise WorktreeError("path outside root_dir")
        cur = self._root
        for part in rel.split(os.sep):
            cur = os.path.join(cur, part)
            try:
                os.mkdir(cur, 0o700)
            except FileExistsError:
                pass
            st = os.lstat(cur)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
                raise WorktreeError("attempt directory is not a private directory")

    def _attempt_tree(self, run_id: str, task_id: str, attempt_no: int, repo_id: str, repo: str) -> str:
        path = self.attempt_worktree_path(run_id, task_id, attempt_no, repo_id)
        if not os.path.lexists(path):
            raise WorktreeError("worktree does not exist")
        self._contained_attempt(path)
        if self._repo_of(path) != repo:
            raise WorktreeError("worktree belongs to a different repository")
        return path

    def _allowlisted(self, repo: str) -> str:
        if not isinstance(repo, str) or repo not in self._repos:
            raise RepoNotAllowlistedError("repo is not on the operator allowlist")
        self._check_repo(repo)
        return repo

    def _lock_all(self, stack: ExitStack, repos: Sequence[tuple[str, str]]) -> None:
        """Take the locks of ``(repo_id, repo_path)`` pairs in ONE global order: ascending (resolved path, repo_id).
        The order must not depend on repo_id alone: a run's bindings outlive registry edits, so two runs may map the
        same ids to different paths. Locks are keyed by the resolved path (``_lock``), so one repository reached
        through two spellings is still one lock, taken once."""
        seen: set[str] = set()
        for real, _rid, repo in sorted((os.path.realpath(path), rid, path) for rid, path in repos):
            if real in seen:
                continue
            seen.add(real)
            stack.enter_context(self._lock(repo))

    # --- create all, or none -------------------------------------------------------------------------------------
    def create_attempt(self, run_id: str, task_id: str, attempt_no: int,
                       repos: Sequence[AttemptRepo]) -> dict[str, str]:
        """Create every worktree of one attempt from its pinned baseline, or none.

        Per-repository locks are taken in ascending repository-path order (``_lock_all``; deadlock-free even when two
        runs bind the same ids to different paths) and held for the whole create.
        Each new tree is checked (owning repository, HEAD equals the baseline, clean). On any failure every worktree
        and branch made by this call is removed and ``AttemptCreateError(repo_id)`` names the repository that failed.
        Returns ``{repo_id: path}``.
        """
        adir = self.attempt_dir(run_id, task_id, attempt_no)
        if not isinstance(repos, (list, tuple)) or not 1 <= len(repos) <= MAX_ATTEMPT_REPOS:
            raise ValueError("an attempt needs 1 to %d repositories" % MAX_ATTEMPT_REPOS)
        ordered = sorted(repos, key=lambda r: r.repo_id)
        ids = [r.repo_id for r in ordered]
        if len(set(ids)) != len(ids) or len({r.repo_path for r in ordered}) != len(ordered):
            raise ValueError("attempt repositories must be distinct")
        for r in ordered:
            check_registry_id(r.repo_id)
            self._allowlisted(r.repo_path)
            if r.access not in ("write", "read"):
                raise ValueError("access must be write or read")
            if not isinstance(r.baseline_sha, str) or not _HEX_SHA.fullmatch(r.baseline_sha):
                raise ValueError("baseline_sha must be a full lowercase hex commit id")
        branch = self._guard_ref(self.attempt_branch(run_id, task_id, attempt_no))
        made: list[tuple[AttemptRepo, str]] = []
        current = ordered[0].repo_id
        with ExitStack() as locks:
            self._lock_all(locks, [(r.repo_id, r.repo_path) for r in ordered])
            try:
                self._mkdirs_private(adir)
                for r in ordered:
                    current = r.repo_id
                    path = os.path.join(adir, r.repo_id)
                    self._contained_attempt(path)
                    if os.path.lexists(path):
                        raise WorktreeError("attempt worktree already exists")
                    repo = r.repo_path
                    self._refuse_dangerous_config(repo)
                    rc, out, _ = _git(repo, ["rev-parse", "--verify", "--quiet", "--end-of-options",
                                             f"{r.baseline_sha}^{{commit}}"], check=False)
                    if rc != 0 or out.strip() != r.baseline_sha:
                        raise WorktreeError("baseline commit is missing")
                    _git(repo, ["worktree", "prune"], check=False)
                    if r.access == "write":
                        rc, _, _ = _git(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False)
                        if rc == 0:
                            raise WorktreeError("attempt branch already exists")  # never reuse another attempt's
                        made.append((r, path))
                        _git(repo, ["worktree", "add", "-b", branch, path, r.baseline_sha])
                    else:
                        made.append((r, path))
                        _git(repo, ["worktree", "add", "--detach", path, r.baseline_sha])
                    if self._repo_of(path) != repo:
                        raise WorktreeError("worktree does not belong to its repository")
                    head, clean = self._tree_state(repo, path)
                    if head != r.baseline_sha or not clean:
                        raise WorktreeError("new worktree is not the pinned baseline")
            except BaseException as exc:
                for r, path in reversed(made):
                    self._discard_locked(r, path, branch)
                try:
                    os.rmdir(adir)
                except OSError:
                    pass
                if isinstance(exc, (WorktreeError, ValueError, OSError)):
                    raise AttemptCreateError(current) from exc
                raise
        return {r.repo_id: os.path.join(adir, r.repo_id) for r in ordered}

    def _discard_locked(self, r: AttemptRepo, path: str, branch: str) -> None:
        """Best effort removal of one attempt worktree (caller holds the repository lock)."""
        repo = r.repo_path
        try:
            self._refuse_dangerous_config(repo)
            if os.path.lexists(path):
                self._contained_attempt(path)
                _git(repo, ["worktree", "remove", "--force", path], check=False)
                if os.path.lexists(path) and not os.path.islink(path):
                    shutil.rmtree(path, ignore_errors=True)
            _git(repo, ["worktree", "prune"], check=False)
            if r.access == "write":
                _git(repo, ["branch", "-D", "--", self._guard_ref(branch)], check=False)
        except (WorktreeError, OSError):
            pass

    def discard_attempt(self, run_id: str, task_id: str, attempt_no: int, repos: Sequence[AttemptRepo]) -> None:
        """Best effort: remove an attempt's worktrees (used when they could not be recorded before the spawn)."""
        branch = self._guard_ref(self.attempt_branch(run_id, task_id, attempt_no))
        ordered = sorted(repos, key=lambda r: r.repo_id)
        with ExitStack() as locks:
            self._lock_all(locks, [(r.repo_id, self._allowlisted(r.repo_path)) for r in ordered])
            for r in reversed(ordered):
                self._discard_locked(r, self.attempt_worktree_path(run_id, task_id, attempt_no, r.repo_id), branch)
        try:
            os.rmdir(self.attempt_dir(run_id, task_id, attempt_no))
        except OSError:
            pass

    def remove_attempt_clean(self, run_id: str, task_id: str, attempt_no: int,
                             repos: Sequence[AttemptRepo]) -> dict[str, str]:
        """Non-forced removal of an attempt's worktrees that provably never ran (ADR 026 D4 step 8).

        Per repository (locks in repo_id order): ``absent`` (nothing there), ``removed`` (a clean tree still at the
        pinned baseline: ``git worktree remove`` without --force, and its attempt branch deleted only while it still
        points at the baseline), or ``kept_dirty`` / ``kept_moved`` / ``kept_foreign`` (left untouched and reported).
        """
        branch = self._guard_ref(self.attempt_branch(run_id, task_id, attempt_no))
        out: dict[str, str] = {}
        ordered = sorted(repos, key=lambda r: r.repo_id)
        with ExitStack() as locks:
            self._lock_all(locks, [(r.repo_id, self._allowlisted(r.repo_path)) for r in ordered])
            for r in ordered:
                repo, path = r.repo_path, self.attempt_worktree_path(run_id, task_id, attempt_no, r.repo_id)
                self._refuse_dangerous_config(repo)
                if not os.path.lexists(path):
                    out[r.repo_id] = "absent"
                else:
                    try:
                        self._contained_attempt(path)
                        if self._repo_of(path) != repo:
                            raise WorktreeError("foreign")
                    except WorktreeError:
                        out[r.repo_id] = "kept_foreign"
                        continue
                    head, clean = self._tree_state(repo, path)
                    if not clean:
                        out[r.repo_id] = "kept_dirty"
                        continue
                    if head != r.baseline_sha:
                        out[r.repo_id] = "kept_moved"
                        continue
                    _git(repo, ["worktree", "remove", path])
                    out[r.repo_id] = "removed"
                if r.access == "write":
                    rc, tip, _ = _git(repo, ["rev-parse", "--verify", "--quiet", "--end-of-options",
                                             f"refs/heads/{branch}^{{commit}}"], check=False)
                    if rc == 0 and tip.strip() == r.baseline_sha:
                        _git(repo, ["branch", "-D", "--", branch], check=False)
        try:
            os.rmdir(self.attempt_dir(run_id, task_id, attempt_no))
        except OSError:
            pass
        return out

    def attempt_tree_problem(self, run_id: str, task_id: str, attempt_no: int, repo_id: str, repo: str,
                             recorded_path: str, baseline_sha: str) -> str | None:
        """Spawn-time recheck of one recorded attempt worktree (ADR 026 D4 step 2). Fixed codes, never raises."""
        try:
            self._allowlisted(repo)
            path = self.attempt_worktree_path(run_id, task_id, attempt_no, repo_id)
            if recorded_path != path:
                return "worktree_mismatch"
            path = self._attempt_tree(run_id, task_id, attempt_no, repo_id, repo)
            head, clean = self._tree_state(repo, path)
        except (WorktreeError, ValueError, OSError):
            return "worktree_mismatch"
        if head != baseline_sha:
            return "head_moved"
        if not clean:
            return "worktree_dirty"
        return None

    # --- facts ---------------------------------------------------------------------------------------------------
    def _tree_state(self, repo: str, path: str) -> tuple[str, bool]:
        _, head, _ = self._tree_git(repo, path, ["rev-parse", "HEAD"])
        _, porc, trunc = self._tree_git(repo, path, ["status", "--porcelain=v1", "-z", "--untracked-files=normal",
                                                     "--ignore-submodules=all"], max_bytes=4096)
        return head.strip(), not porc and not trunc

    def attempt_tree_state(self, run_id: str, task_id: str, attempt_no: int, repo_id: str,
                           repo: str) -> tuple[str, bool]:
        """(HEAD, clean) of one attempt worktree (the pre-spawn recheck)."""
        self._allowlisted(repo)
        return self._tree_state(repo, self._attempt_tree(run_id, task_id, attempt_no, repo_id, repo))

    def attempt_branch_head(self, run_id: str, task_id: str, attempt_no: int, repo: str) -> str | None:
        """Current SHA of the attempt branch in ``repo`` (read from the repository, not the tree), or None."""
        self._allowlisted(repo)
        branch = self._guard_ref(self.attempt_branch(run_id, task_id, attempt_no))
        rc, out, _ = _git(repo, ["rev-parse", "--verify", "--quiet", "--end-of-options",
                                 f"refs/heads/{branch}^{{commit}}"], check=False)
        sha = out.strip()
        return sha if rc == 0 and _HEX_SHA.fullmatch(sha) else None

    def attempt_commit_landed(self, run_id: str, task_id: str, attempt_no: int, repo_id: str, repo: str,
                              head_before: str, protected: Sequence[str] = ()) -> str | None:
        """Crash recovery: the commit SHA if the branch HEAD is a direct child of ``head_before`` and the worktree's
        index equals that commit's tree (the commit landed), else None. Read-only."""
        head = self.attempt_branch_head(run_id, task_id, attempt_no, repo)
        if head is None or head == head_before:
            return None
        rc, parents, _ = _git(repo, ["rev-list", "--parents", "-n", "1", "--end-of-options", head], check=False)
        if rc != 0 or parents.split() != [head, head_before]:
            return None
        path = self._attempt_tree(run_id, task_id, attempt_no, repo_id, repo)
        rc, _, _ = self._tree_git(repo, path, ["diff", "--cached", "--quiet", "--no-ext-diff", "--no-textconv",
                                               "--ignore-submodules=none", head, "--"], check=False)
        if rc != 0:
            return None
        try:  # the commit landed; it only counts as ours if the committed change passes the full commit rule again
            self.check_commit_range(repo, head_before, head, protected)
        except CommitRefused:
            return None
        return head

    def check_commit_range(self, repo: str, base: str, head: str, protected: Sequence[str] = ()) -> int:
        """Re-run the D3 rule on a committed change (``diff-tree -r -z --raw --no-renames base head`` and the blobs
        from the object store): paths, protected paths, modes and XML well-formedness. Raises ``CommitRefused``;
        returns the number of changed paths. Read-only."""
        for sha in (base, head):
            if not isinstance(sha, str) or not _HEX_SHA.fullmatch(sha):
                raise CommitRefused("bad_commit")
        self._allowlisted(repo)
        self._refuse_dangerous_config(repo)
        _, raw, trunc = _git(repo, ["diff-tree", "-r", "-z", "--raw", "--no-renames", "--no-abbrev",
                                    "--no-ext-diff", "--no-textconv", "--end-of-options", base, head],
                             max_bytes=_COMMIT_LISTING_BYTES)
        if trunc:
            raise CommitRefused("too_many_paths")
        tokens = raw.split("\x00")
        records = [(t.split(" "), tokens[i + 1] if i + 1 < len(tokens) else "")
                   for i, t in enumerate(tokens) if t.startswith(":")]
        if len(records) > MAX_COMMIT_PATHS:
            raise CommitRefused("too_many_paths")
        check_staged_paths(records, check_protected_patterns(list(protected)))
        self._check_blobs(repo, records)
        return len(records)

    @staticmethod
    def _check_blobs(repo: str, records) -> None:
        bad = 0
        for fields, _name in records:
            if len(fields) > 3 and fields[1] == "100644":  # added or modified: the blob, never the work tree file
                if not _HEX_SHA.fullmatch(fields[3]):
                    raise CommitRefused("xml_not_well_formed")
                rc, blob, too_big = _git_raw(repo, ["cat-file", "blob", fields[3]], max_bytes=MAX_XML_BYTES,
                                             check=False)
                if rc != 0 or too_big:
                    bad += 1
                    continue
                try:
                    check_xml(blob)
                except XmlRefused:
                    bad += 1
        if bad:
            raise CommitRefused("xml_not_well_formed", bad)

    # --- host commit ---------------------------------------------------------------------------------------------
    def _commit_status(self, repo: str, path: str) -> list[str]:
        """``status --porcelain=v1 -z --untracked-files=all --ignored=matching`` entries (bounded, refused if over)."""
        _, porc, trunc = self._tree_git(repo, path, ["status", "--porcelain=v1", "-z", "--untracked-files=all",
                                                     "--ignored=matching", "--ignore-submodules=all"],
                                        max_bytes=_COMMIT_LISTING_BYTES)
        entries = [e for e in porc.split("\x00") if e]
        if trunc or len(entries) > MAX_COMMIT_PATHS:
            raise CommitRefused("too_many_paths")
        return entries

    def commit_attempt(self, run_id: str, task_id: str, attempt_no: int, repo_id: str, repo: str, message: str, *,
                       expected_head: str, protected: Sequence[str] = ()) -> dict:
        """Host-side commit of one write-access attempt worktree onto its attempt branch.

        Same hardening as ``commit`` plus the ADR 026 D3 rule: no ignored path may be present
        (``ignored_path_present``), every staged path passes ``check_staged_paths`` (XML only, not protected, mode
        100644), nothing untracked remains after staging (``untracked_path_present``), and every added or modified
        blob, read from the object store, is well-formed XML (``xml_not_well_formed``). The branch HEAD must still be ``expected_head`` (the value
        recorded in the commit intent). With nothing to commit it returns ``committed: False`` and
        ``head == head_before`` (the candidate is the baseline). On a refusal the index is reset and the working tree
        is left exactly as the agent left it. Returns {"head_before","head","paths","committed"}.
        """
        if (not isinstance(message, str) or not message.strip() or len(message) > MAX_COMMIT_MESSAGE
                or "\x00" in message):
            raise ValueError("invalid commit message")
        self._allowlisted(repo)
        protected = check_protected_patterns(list(protected))
        branch = self._guard_ref(self.attempt_branch(run_id, task_id, attempt_no))
        path = self._attempt_tree(run_id, task_id, attempt_no, repo_id, repo)
        with self._lock(repo):
            _, head_before, _ = self._tree_git(repo, path, ["rev-parse", "HEAD"])
            head_before = head_before.strip()
            rc, cur, _ = self._tree_git(repo, path, ["symbolic-ref", "--short", "-q", "HEAD"], check=False)
            if rc != 0 or cur.strip() != branch:
                raise CommitRefused("not_on_attempt_branch")
            if head_before != expected_head:
                raise CommitRefused("branch_moved")
            entries = self._commit_status(repo, path)
            if any(e.startswith("!! ") for e in entries):
                raise CommitRefused("ignored_path_present", sum(e.startswith("!! ") for e in entries))
            if not entries:
                return {"head_before": head_before, "head": head_before, "paths": 0, "committed": False}
            try:
                self._tree_git(repo, path, ["add", "-A", "--"])
                _, raw, raw_trunc = self._tree_git(repo, path, ["diff", "--cached", "--raw", "-z", "--no-renames",
                                                                "--no-abbrev", "--no-ext-diff", "--no-textconv",
                                                                "--ignore-submodules=none"],
                                                   max_bytes=_COMMIT_LISTING_BYTES)
                if raw_trunc:
                    raise CommitRefused("too_many_paths")
                tokens = raw.split("\x00")
                records = [(t.split(" "), tokens[i + 1] if i + 1 < len(tokens) else "")
                           for i, t in enumerate(tokens) if t.startswith(":")]
                if len(records) > MAX_COMMIT_PATHS:
                    raise CommitRefused("too_many_paths")
                if not records:
                    self._tree_git(repo, path, ["reset", "-q", "--"], check=False)
                    return {"head_before": head_before, "head": head_before, "paths": 0, "committed": False}
                check_staged_paths(records, protected)
                after = self._commit_status(repo, path)
                if any(e.startswith(("?? ", "!! ")) for e in after):
                    raise CommitRefused("untracked_path_present", sum(e.startswith(("?? ", "!! ")) for e in after))
                self._check_blobs(repo, records)
                # Bind the commit to the tree that was checked: write the index to a tree, re-check that exact tree
                # against the baseline, then commit THAT tree with commit-tree and move only the attempt branch from
                # head_before to it (update-ref with the expected old value). An index rewritten after the checks
                # (same-uid agent, sandbox off) cannot slip into the commit.
                _, tree, _ = self._tree_git(repo, path, ["write-tree"])
                tree = tree.strip()
                if not _HEX_SHA.fullmatch(tree):
                    raise CommitRefused("git_error")
                _, commit, _ = _git(repo, ["-c", "commit.gpgsign=false", "-c", "user.useConfigOnly=false",
                                           "commit-tree", tree, "-p", head_before, "-m", message.strip()])
                commit = commit.strip()
                if not _HEX_SHA.fullmatch(commit):
                    raise CommitRefused("git_error")
                self.check_commit_range(repo, head_before, commit, protected)
                _git(repo, ["update-ref", "-m", "synapse attempt commit", f"refs/heads/{branch}", commit, head_before])
            except BaseException:
                self._tree_git(repo, path, ["reset", "-q", "--"], check=False)
                raise
            head = commit
            # The worktree's index/HEAD now follow the branch; refresh the index to the commit (files untouched).
            self._tree_git(repo, path, ["reset", "-q", "--"], check=False)
        return {"head_before": head_before, "head": head, "paths": len(records), "committed": True}


class WorktreeManager(AttemptWorktrees):
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
            # keyed by the resolved path: two spellings of one repository must share one lock
            key = os.path.realpath(repo)
            tl = _locks.setdefault(key, threading.Lock())
        return _RepoLock(tl, os.path.join(self._root, f".lock-{hashlib.sha256(key.encode()).hexdigest()[:16]}"))

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
        # git takes this worktree's common dir (config, attributes, refs) from here: it must be the
        # owning repository's .git that the guard checks, as written by `git worktree add`.
        try:
            with open(os.path.join(gitdir, "commondir"), "rb") as fh:
                common = fh.read(4096).decode("utf-8", "replace").strip()
        except OSError as exc:
            raise WorktreeError("worktree metadata missing") from exc
        if os.path.normpath(os.path.join(gitdir, common)) != os.path.join(repo, ".git"):
            raise WorktreeError("worktree commondir does not point at its repository")
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
        if isinstance(repo, str) and repo in self._repos:
            self._check_repo(repo)  # a missing/odd .git stays a ValueError, as before
            self._refuse_dangerous_config(repo)  # guard first: no git query at all on a refused repo
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
            self._refuse_dangerous_config(repo)  # also on the idempotent path below
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
        _, porc, t1 = self._tree_git(repo, path, ["status", "--porcelain=v1", "-z", "--untracked-files=normal",
                                                     "--ignore-submodules=all"], max_bytes=max_bytes)
        _, stat_out, t2 = self._tree_git(repo, path, ["diff", "--stat", "--no-ext-diff", "--no-textconv",
                                      "--no-color", "--ignore-submodules=all", "HEAD", "--"], max_bytes=max_bytes)
        n = len([e for e in porc.split("\x00") if e])
        return WorktreeStatus(not porc, head.strip(), br.strip() if rc == 0 else "",
                              n, stat_out, t1 or t2)

    def diff(self, run_id: str, task_id: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> BoundedText:
        found = self._existing(run_id, task_id)
        if found is None:
            raise WorktreeError("worktree does not exist")
        _, out, trunc = self._tree_git(found[1], found[0], ["diff", "--no-ext-diff", "--no-textconv", "--no-color",
                                        "--ignore-submodules=all", "HEAD", "--"], max_bytes=max_bytes)
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
            _, porc, trunc = self._tree_git(repo, path, ["status", "--porcelain=v1", "-z", "--untracked-files=all",
                                                         "--ignore-submodules=all"],
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
                                                           "--ignore-submodules=all", sha, "HEAD", "--"], max_bytes=max_bytes)
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
            # Also on force: `worktree remove`/`branch -D` would fire config hooks of a refused repo.
            self._refuse_dangerous_config(repo)
            if not force:
                _, porc, _ = self._tree_git(repo, path, ["status", "--porcelain=v1", "-z",
                                                         "--untracked-files=normal", "--ignore-submodules=all"])
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
