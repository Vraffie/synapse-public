"""Strict loader for the operator's Claude CLI profile (``--claude-cli-config``).

The file is operator-owned configuration, not client input: it must be a small,
user-owned regular file that is not group/world writable, with exactly the
documented keys. Anything else fails startup rather than being ignored.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat

from synapse.providers.claude_cli import ClaudeCliProfile
from synapse.providers.sandbox_launcher import find_bwrap

MAX_CONFIG_BYTES = 64 * 1024
_REQUIRED = {"executable", "allowed_tools", "enabled"}
_OPTIONAL = {"max_timeout_seconds", "repos", "worktree_root", "require_approval",
             "require_approval_for_modes", "require_approval_for_tools", "sandbox"}
MAX_REPOS = 32


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
class AgentConfig:
    """Validated operator config. ``repos``/``worktree_root`` are both set or both empty."""
    profile: ClaudeCliProfile
    repos: tuple[str, ...] = ()
    worktree_root: str | None = None
    approval_policy: ApprovalPolicy = ApprovalPolicy()


def _abs_path(value, what: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or not os.path.isabs(value):
        raise AgentConfigError("%s must be an absolute path" % what)
    if os.path.normpath(value) != value or value == os.sep:
        raise AgentConfigError("%s must be normalized and not the filesystem root" % what)
    return value


def _overlaps(a: str, b: str) -> bool:
    pa, pb = Path(a), Path(b)
    return pa == pb or pa in pb.parents or pb in pa.parents


def _check_repos(data: dict, state_dir: Path) -> tuple[tuple[str, ...], str | None]:
    if ("repos" in data) != ("worktree_root" in data):
        raise AgentConfigError("repos and worktree_root must be configured together")
    if "repos" not in data:
        return (), None
    repos = data["repos"]
    if not isinstance(repos, list) or not 1 <= len(repos) <= MAX_REPOS:
        raise AgentConfigError("repos must be a list of 1 to %d absolute paths" % MAX_REPOS)
    clean = tuple(_abs_path(r, "repo") for r in repos)
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
    return clean, root


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
    repos, root = _check_repos(data, state_dir)
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
    return AgentConfig(profile, repos, root, _approval_policy(data, profile))
