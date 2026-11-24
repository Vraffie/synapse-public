"""Strict loader for the operator's Claude CLI profile (``--claude-cli-config``).

The file is operator-owned configuration, not client input: it must be a small,
user-owned regular file that is not group/world writable, with exactly the
documented keys. Anything else fails startup rather than being ignored.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat

from synapse.infrastructure.worktrees import check_protected_patterns, check_registry_id
from synapse.providers.claude_cli import ClaudeCliProfile
from synapse.providers.sandbox_launcher import find_bwrap

MAX_CONFIG_BYTES = 64 * 1024
_REQUIRED = {"executable", "allowed_tools", "enabled"}
_OPTIONAL = {"max_timeout_seconds", "repos", "worktree_root", "require_approval",
             "require_approval_for_modes", "require_approval_for_tools", "sandbox"}
MAX_REPOS = 32
MAX_REGISTRY_ENTRIES = 16  # object entries (multi-repository registry); the candidate manifest's repository limit
MAX_BASELINE_REFS = 16
_ENTRY_KEYS = {"id", "path"}
_ENTRY_OPTIONAL = {"baseline_refs", "default_baseline_ref", "protected_paths"}


class AgentConfigError(ValueError):
    """The operator Claude CLI config is missing, unsafe or invalid."""


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise AgentConfigError("duplicate key in config")
    return dict(pairs)


def _no_constants(name):
    raise AgentConfigError("non-finite number in config")


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """Server-side approval requirement (security review F6). Default: nothing is mandatory.

    ``require_all`` makes every agent run need an ``approval_step``; otherwise a run needs one when
    its permission mode is in ``modes`` or it asks for any tool in ``tools``.
    """
    require_all: bool = False
    modes: frozenset[str] = frozenset()
    tools: frozenset[str] = frozenset()

    def required_for(self, permission_mode: str, allowed_tools) -> bool:
        return self.require_all or permission_mode in self.modes or bool(self.tools & set(allowed_tools))


@dataclass(frozen=True, slots=True)
class RepoEntry:
    """One operator registry entry (the object form of ``repos``, ADR 026 D1).

    Only object entries can take part in a multi-repository run; a plain path string stays a legacy single-repository
    allowlist entry. ``baseline_refs`` are full ``refs/heads/...`` names; ``protected_paths`` are anchored globs
    (see ``synapse.infrastructure.worktrees.matches_protected``).
    """
    id: str
    path: str
    baseline_refs: tuple[str, ...]
    default_baseline_ref: str | None
    protected_paths: tuple[str, ...]

    def canonical(self) -> dict:
        return {"id": self.id, "path": self.path, "baseline_refs": list(self.baseline_refs),
                "default_baseline_ref": self.default_baseline_ref, "protected_paths": list(self.protected_paths)}

    @property
    def entry_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def registry_sha256(entries) -> str:
    """Digest of the validated registry (object entries, sorted by id), logged at daemon start."""
    body = [e.canonical() for e in sorted(entries, key=lambda e: e.id)]
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Validated operator config. ``repos``/``worktree_root`` are both set or both empty.

    ``repos`` is every allowlisted path (plain strings and object entries); ``registry`` holds only the object
    entries, for multi-repository runs.
    """
    profile: ClaudeCliProfile
    repos: tuple[str, ...] = ()
    worktree_root: str | None = None
    approval_policy: ApprovalPolicy = ApprovalPolicy()
    registry: tuple[RepoEntry, ...] = ()


def _abs_path(value, what: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or not os.path.isabs(value):
        raise AgentConfigError("%s must be an absolute path" % what)
    if os.path.normpath(value) != value or value == os.sep:
        raise AgentConfigError("%s must be normalized and not the filesystem root" % what)
    return value


def _overlaps(a: str, b: str) -> bool:
    pa, pb = Path(a), Path(b)
    return pa == pb or pa in pb.parents or pb in pa.parents


_BASELINE_REF_MAX = 256


def _baseline_ref(value) -> str:
    """A full local branch name: ``refs/heads/<name>`` with a conservative character set (no SHAs, no remotes)."""
    if (not isinstance(value, str) or not value.startswith("refs/heads/") or len(value) > _BASELINE_REF_MAX
            or not all(c.isascii() and (c.isalnum() or c in "._/-") for c in value) or ".." in value
            or "//" in value or value.endswith(("/", ".", ".lock")) or "/." in value or "/-" in value):
        raise AgentConfigError("baseline refs must be full refs/heads/<branch> names")
    return value


def _repo_entry(raw) -> RepoEntry:
    if not isinstance(raw, dict) or not _ENTRY_KEYS <= set(raw) <= _ENTRY_KEYS | _ENTRY_OPTIONAL:
        raise AgentConfigError("a repos object must have id and path and only the optional keys "
                               "baseline_refs, default_baseline_ref, protected_paths")
    try:
        repo_id = check_registry_id(raw["id"])
    except ValueError as exc:
        raise AgentConfigError("repos object id: %s" % exc) from None
    path = _abs_path(raw["path"], "repo")
    refs = raw.get("baseline_refs")
    default = raw.get("default_baseline_ref")
    if refs is not None:
        if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_BASELINE_REFS:
            raise AgentConfigError("baseline_refs must be a list of 1 to %d refs" % MAX_BASELINE_REFS)
        refs = tuple(_baseline_ref(r) for r in refs)
        if len(set(refs)) != len(refs):
            raise AgentConfigError("baseline_refs must not contain duplicates")
    if default is not None:
        default = _baseline_ref(default)
        if refs is None:
            refs = (default,)
        elif default not in refs:
            raise AgentConfigError("default_baseline_ref must be one of baseline_refs")
    try:
        protected = check_protected_patterns(raw.get("protected_paths", []))
    except ValueError as exc:
        raise AgentConfigError("protected_paths: %s" % exc) from None
    return RepoEntry(repo_id, path, tuple(refs or ()), default, protected)


def _check_repos(data: dict, state_dir: Path) -> tuple[tuple[str, ...], str | None, tuple[RepoEntry, ...]]:
    if ("repos" in data) != ("worktree_root" in data):
        raise AgentConfigError("repos and worktree_root must be configured together")
    if "repos" not in data:
        return (), None, ()
    repos = data["repos"]
    if not isinstance(repos, list) or not 1 <= len(repos) <= MAX_REPOS:
        raise AgentConfigError("repos must be a list of 1 to %d absolute paths or repository objects" % MAX_REPOS)
    entries = tuple(_repo_entry(r) for r in repos if isinstance(r, dict))
    if len(entries) > MAX_REGISTRY_ENTRIES:
        raise AgentConfigError("at most %d repos may be repository objects" % MAX_REGISTRY_ENTRIES)
    if len({e.id for e in entries}) != len(entries):
        raise AgentConfigError("repos object ids must be unique")
    clean = tuple(r["path"] if isinstance(r, dict) else _abs_path(r, "repo") for r in repos)
    if len(set(clean)) != len(clean):
        raise AgentConfigError("repos must not contain duplicates")
    root = _abs_path(data["worktree_root"], "worktree_root")
    state = os.path.realpath(state_dir)
    for repo in clean:
        if os.path.realpath(repo) != repo or not os.path.isdir(os.path.join(repo, ".git")) \
                or os.path.islink(os.path.join(repo, ".git")):
            raise AgentConfigError("each repo must be a symlink-free directory with a real .git directory")
        if _overlaps(repo, state) or _overlaps(repo, root):
            raise AgentConfigError("repos must not overlap the state directory or worktree_root")
    if _overlaps(root, state) or _overlaps(os.path.realpath(root), state):
        raise AgentConfigError("worktree_root must be outside the state directory")
    return clean, root, entries


def _approval_policy(data: dict, profile: ClaudeCliProfile) -> ApprovalPolicy:
    from synapse.persistence.agent_tasks import AGENT_PERMISSION_MODES
    require_all = data.get("require_approval", False)
    if not isinstance(require_all, bool):
        raise AgentConfigError("require_approval must be a boolean")
    lists = {}
    for key, allowed in (("require_approval_for_modes", AGENT_PERMISSION_MODES), ("require_approval_for_tools", None)):
        value = data.get(key, [])
        if (not isinstance(value, list) or len(value) > 64 or any(not isinstance(v, str) for v in value)
                or len(set(value)) != len(value)):
            raise AgentConfigError("%s must be a list of unique strings" % key)
        if allowed is not None and not set(value) <= allowed:
            raise AgentConfigError("%s contains an unknown permission mode" % key)
        if allowed is None and not set(value) <= set(profile.allowed_tools):
            raise AgentConfigError("%s must be a subset of allowed_tools" % key)
        lists[key] = frozenset(value)
    return ApprovalPolicy(require_all, lists["require_approval_for_modes"], lists["require_approval_for_tools"])


def load_claude_cli_profile(path: Path, state_dir: Path) -> ClaudeCliProfile:
    """Profile-only view of :func:`load_agent_config` (kept for existing callers)."""
    return load_agent_config(path, state_dir).profile


def load_agent_config(path: Path, state_dir: Path) -> AgentConfig:
    """Read and validate the config; ``state_dir`` is the daemon's (derived, never configured)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise AgentConfigError("cannot open Claude CLI config: %s" % exc.strerror) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise AgentConfigError("config must be a user-owned regular file that is not group/world writable")
        if info.st_size > MAX_CONFIG_BYTES:
            raise AgentConfigError("config is too large")
        raw = os.read(fd, MAX_CONFIG_BYTES + 1)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentConfigError("config is not valid JSON") from exc
    if not isinstance(data, dict) or not _REQUIRED <= set(data) <= _REQUIRED | _OPTIONAL:
        raise AgentConfigError("config must contain executable, allowed_tools, enabled and only the optional keys "
                               "max_timeout_seconds, repos, worktree_root, require_approval, require_approval_for_modes, "
                               "require_approval_for_tools, sandbox")
    tools = data["allowed_tools"]
    if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
        raise AgentConfigError("allowed_tools must be a list of strings")
    if not isinstance(data["enabled"], bool):
        raise AgentConfigError("enabled must be a boolean")
    sandbox = data.get("sandbox", False)
    if not isinstance(sandbox, bool):
        raise AgentConfigError("sandbox must be a boolean")
    repos, root, registry = _check_repos(data, state_dir)
    extra = {"sandbox": sandbox, "git_repos": tuple(repos)}
    if "max_timeout_seconds" in data:
        extra["max_timeout_seconds"] = data["max_timeout_seconds"]
    try:
        # Per-task tool subsets and permission modes are derived from this profile;
        # "plan" is the least-privileged base. bypassPermissions/api keys are not configurable here.
        profile = ClaudeCliProfile(executable=data["executable"], allowed_tools=tuple(tools),
                                   state_dir=str(Path(state_dir).resolve()), enabled=data["enabled"],
                                   permission_mode="plan", **extra)
    except (ValueError, TypeError) as exc:
        raise AgentConfigError("invalid Claude CLI config: %s" % exc) from exc
    if profile.enabled and not (os.path.isfile(profile.executable) and os.access(profile.executable, os.X_OK)):
        raise AgentConfigError("executable does not exist or is not executable")
    if profile.enabled and profile.sandbox and find_bwrap() is None:
        raise AgentConfigError("sandbox is requested but bwrap is not installed (no unsandboxed fallback)")
    return AgentConfig(profile, repos, root, _approval_policy(data, profile), registry)
