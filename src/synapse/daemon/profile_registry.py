"""Validated multi-profile registry, pure profile selection and rate-limit cooldowns (ADR 015, B3).

This module is data and decisions only. Nothing here starts a process or opens a database. The daemon
builds its registry with ``agent_profiles`` (``--agent-config``); a ``codex`` profile is never the primary
or a fallback (validated here), so it runs only for a task that names it (ADR 016).

A registry holds up to :data:`MAX_PROFILES` profiles (``claude`` or ``codex``), a ``primary`` and an
ordered ``fallback`` list. Credentials are never part of the registry. The legacy single-profile file
accepted by ``agentd --claude-cli-config`` keeps working: it is read as a one-profile registry.

Selection rules (see :func:`select_profile`): the primary is the default; a task may name a profile;
fallback applies only while no process has started, and only when the chosen profile is disabled,
unavailable (including cooling down) or at its cap; a disabled profile is never selected and neither the
per-profile nor the global cap is ever exceeded. Fallback never "rotates" through profiles to get past a
provider's rate limit: a profile that reported one is put into a cooldown, and that failure is not retried.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from synapse.daemon.agent_config import (
    MAX_CONFIG_BYTES, AgentConfig, AgentConfigError, _abs_path, _check_repos, _no_constants, _no_duplicates,
    load_agent_config,
)
from synapse.domain.contracts import validate_id

MAX_PROFILES = 4
MAX_CONCURRENCY = 6
LEGACY_PROFILE_ID = "claude-default"
LEGACY_CONCURRENCY = 2  # AgentRuntime's default engine cap, so a legacy file behaves as before
DEFAULT_MAX_TIMEOUT_SECONDS = 3600.0
MAX_TIMEOUT_CEILING = 86_400.0
MAX_COOLDOWN_SECONDS = 86_400.0
PROVIDERS = frozenset({"claude", "codex"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_TOOL = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{0,63}(\([^\x00-\x1f()]{1,200}\))?")
_PROFILE_REQUIRED = {"id", "provider", "executable", "allowed_tools", "max_concurrency", "enabled"}
_PROFILE_OPTIONAL = {"max_timeout_seconds", "model"}
_TOP_REQUIRED = {"profiles", "primary"}
_TOP_OPTIONAL = {"fallback", "max_concurrency", "repos", "worktree_root"}


class RegistryError(AgentConfigError):
    """The profile registry is missing, unsafe or invalid (a startup refusal, like AgentConfigError)."""


@dataclass(frozen=True, slots=True)
class ProfileSpec:
    id: str
    provider: str
    executable: str
    allowed_tools: tuple[str, ...]
    max_concurrency: int
    enabled: bool
    max_timeout_seconds: float = DEFAULT_MAX_TIMEOUT_SECONDS
    model: str | None = None
    permission_mode: str | None = None  # ceiling and default mode (agent-config profiles); None: no extra limit
    claude_config_dir: str | None = None  # agent-config claude profiles: the account's config dir (path only)


@dataclass(frozen=True, slots=True)
class ProfileRegistry:
    profiles: tuple[ProfileSpec, ...]
    primary: str
    fallback: tuple[str, ...] = ()
    max_concurrency: int = LEGACY_CONCURRENCY  # global cap across every profile
    repos: tuple[str, ...] = ()
    worktree_root: str | None = None

    def get(self, profile_id: str) -> ProfileSpec | None:
        return next((p for p in self.profiles if p.id == profile_id), None)

    @property
    def chain(self) -> tuple[str, ...]:
        """Preference order: the primary, then the fallbacks."""
        return (self.primary, *self.fallback)


# ---------------------------------------------------------------- validation
def _int_in(value, lo: int, hi: int, what: str) -> int:
    if type(value) is not int or not lo <= value <= hi:
        raise RegistryError("%s must be an integer from %d to %d" % (what, lo, hi))
    return value


def _profile(raw, index: int) -> ProfileSpec:
    where = "profiles[%d]" % index
    if not isinstance(raw, dict) or not _PROFILE_REQUIRED <= set(raw) <= _PROFILE_REQUIRED | _PROFILE_OPTIONAL:
        raise RegistryError("%s must contain %s and only the optional keys %s" % (
            where, ", ".join(sorted(_PROFILE_REQUIRED)), ", ".join(sorted(_PROFILE_OPTIONAL))))
    pid = raw["id"]
    try:
        validate_id(pid)
    except ValueError:
        raise RegistryError("%s id is invalid" % where) from None
    if not _ID.fullmatch(pid):
        raise RegistryError("%s id must be 1-64 characters of [A-Za-z0-9._-]" % where)
    if raw["provider"] not in PROVIDERS:
        raise RegistryError("%s provider must be one of %s" % (where, ", ".join(sorted(PROVIDERS))))
    provider = raw["provider"]
    try:
        executable = _abs_path(raw["executable"], "%s executable" % where)
    except AgentConfigError as exc:
        raise RegistryError(str(exc)) from None
    tools = raw["allowed_tools"]
    # A Codex profile carries no tool names (its restriction is the sandbox mode), so its list may be empty.
    if (not isinstance(tools, list) or len(tools) > 64 or (provider == "claude" and not tools)
            or any(not isinstance(t, str) or not _TOOL.fullmatch(t) for t in tools)):
        raise RegistryError("%s allowed_tools must be a list of up to 64 tool names (at least 1 for claude)" % where)
    if len(set(tools)) != len(tools):
        raise RegistryError("%s allowed_tools must not contain duplicates" % where)
    cap = _int_in(raw["max_concurrency"], 1, MAX_CONCURRENCY, "%s max_concurrency" % where)
    if not isinstance(raw["enabled"], bool):
        raise RegistryError("%s enabled must be a boolean" % where)
    timeout = raw.get("max_timeout_seconds", DEFAULT_MAX_TIMEOUT_SECONDS)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
            or not 0 < timeout <= MAX_TIMEOUT_CEILING):
        raise RegistryError("%s max_timeout_seconds must be a number above 0 and at most %d" % (where, MAX_TIMEOUT_CEILING))
    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not _MODEL.fullmatch(model)):
        raise RegistryError("%s model must be 1-64 characters of [A-Za-z0-9._-]" % where)
    return ProfileSpec(pid, provider, executable, tuple(tools), cap, raw["enabled"], float(timeout), model)


def validate_registry(registry: ProfileRegistry) -> ProfileRegistry:
    """Cross-field rules shared by every way of building a registry. Returns it unchanged."""
    ids = [p.id for p in registry.profiles]
    if not 1 <= len(ids) <= MAX_PROFILES:
        raise RegistryError("profiles must contain 1 to %d entries" % MAX_PROFILES)
    if len(set(ids)) != len(ids):
        raise RegistryError("profile ids must be unique")
    _int_in(registry.max_concurrency, 1, MAX_CONCURRENCY, "max_concurrency")
    if registry.primary not in ids:
        raise RegistryError("primary must name a configured profile")
    fallback = registry.fallback
    if len(set(fallback)) != len(fallback) or registry.primary in fallback:
        raise RegistryError("fallback must not repeat a profile or contain the primary")
    # No cross-provider fallback until the Codex gates pass (ADR 016 decisions 5 and 9): a codex profile runs
    # only when a task names it, never as the default or as a fallback. One rule to lift later.
    if registry.get(registry.primary).provider == "codex":
        raise RegistryError("a codex profile cannot be the primary (it runs only when a task names it)")
    for pid in fallback:
        spec = registry.get(pid)
        if spec is None:
            raise RegistryError("fallback names an unknown profile")
        if spec.provider == "codex":
            raise RegistryError("a codex profile cannot be a fallback (no cross-provider fallback)")
        if not spec.enabled:
            raise RegistryError("fallback profiles must be enabled")
    for spec in registry.profiles:
        if spec.enabled and not (os.path.isfile(spec.executable) and os.access(spec.executable, os.X_OK)):
            raise RegistryError("executable of an enabled profile does not exist or is not executable")
    return registry


def registry_from_single_profile(config: AgentConfig, *, profile_id: str = LEGACY_PROFILE_ID) -> ProfileRegistry:
    """Interpret today's one-profile config as a one-profile registry (nothing about it changes)."""
    p = config.profile
    spec = ProfileSpec(profile_id, "claude", p.executable, tuple(p.allowed_tools), LEGACY_CONCURRENCY, p.enabled,
                       float(p.max_timeout_seconds), None)
    # The legacy loader already validated the executable (when enabled) and repos; re-check the cross-field rules.
    return validate_registry(ProfileRegistry((spec,), profile_id, (), LEGACY_CONCURRENCY, config.repos, config.worktree_root))


def _read(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise RegistryError("cannot open profile registry: %s" % exc.strerror) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise RegistryError("registry must be a user-owned regular file that is not group/world writable")
        if info.st_size > MAX_CONFIG_BYTES:
            raise RegistryError("registry is too large")
        raw = os.read(fd, MAX_CONFIG_BYTES + 1)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except (UnicodeDecodeError, json.JSONDecodeError, AgentConfigError) as exc:
        raise RegistryError("registry is not valid JSON (or has duplicate keys or non-finite numbers)") from exc
    if not isinstance(data, dict):
        raise RegistryError("registry must be a JSON object")
    return data


def load_registry(path: Path, state_dir: Path) -> ProfileRegistry:
    """Read a registry file, or a legacy single-profile file (detected by its top-level ``executable``).

    ``state_dir`` is the daemon's own (derived, never configured) and is only used to keep repos and the
    worktree root away from it, exactly as for the legacy loader.
    """
    data = _read(Path(path))
    if "executable" in data:  # legacy shape: let the existing strict loader judge it, unchanged
        try:
            return registry_from_single_profile(load_agent_config(Path(path), state_dir))
        except RegistryError:
            raise
        except AgentConfigError as exc:
            raise RegistryError(str(exc)) from exc
    if not _TOP_REQUIRED <= set(data) <= _TOP_REQUIRED | _TOP_OPTIONAL:
        raise RegistryError("registry must contain profiles, primary and only the optional keys %s"
                            % ", ".join(sorted(_TOP_OPTIONAL)))
    raw_profiles = data["profiles"]
    if not isinstance(raw_profiles, list) or not 1 <= len(raw_profiles) <= MAX_PROFILES:
        raise RegistryError("profiles must be a list of 1 to %d entries" % MAX_PROFILES)
    profiles = tuple(_profile(p, i) for i, p in enumerate(raw_profiles))
    fallback = data.get("fallback", [])
    if not isinstance(fallback, list) or any(not isinstance(f, str) for f in fallback) or not isinstance(data["primary"], str):
        raise RegistryError("primary must be a string and fallback a list of profile ids")
    cap = _int_in(data.get("max_concurrency", LEGACY_CONCURRENCY), 1, MAX_CONCURRENCY, "max_concurrency")
    try:
        repos, root, _objects = _check_repos(data, Path(state_dir))  # object entries: see agent_config (ADR 026)
    except AgentConfigError as exc:
        raise RegistryError(str(exc)) from exc
    return validate_registry(ProfileRegistry(profiles, data["primary"], tuple(fallback), cap, repos, root))


# ----------------------------------------------------------------- cooldowns
class Cooldowns:
    """Per-profile "do not use until" marks, set by the caller after a rate-limit or quota failure.

    The clock is injected (monotonic seconds) so behaviour is deterministic in tests. Not thread-safe:
    use it from the daemon's event loop. A cooldown only removes a profile from consideration for a while;
    the failed attempt itself is never retried because of it.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._until: dict[str, float] = {}

    def set(self, profile_id: str, seconds: float) -> None:
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds)
                or not 0 < seconds <= MAX_COOLDOWN_SECONDS):
            raise ValueError("cooldown seconds must be above 0 and at most %d" % MAX_COOLDOWN_SECONDS)
        validate_id(profile_id)
        self._until[profile_id] = max(self._until.get(profile_id, 0.0), self._clock() + seconds)

    def clear(self, profile_id: str) -> None:
        self._until.pop(profile_id, None)

    def remaining(self, profile_id: str) -> float:
        until = self._until.get(profile_id)
        if until is None:
            return 0.0
        left = until - self._clock()
        if left <= 0:
            del self._until[profile_id]
            return 0.0
        return left

    def active(self, profile_id: str) -> bool:
        return self.remaining(profile_id) > 0

    def unavailable(self) -> frozenset[str]:
        """Profiles currently cooling down (expired marks are dropped)."""
        return frozenset(pid for pid in tuple(self._until) if self.remaining(pid) > 0)


# ----------------------------------------------------------------- selection
@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of :func:`select_profile`.

    ``profile_id`` is ``None`` when nothing may start now (``reason`` says why: ``at_global_cap``,
    ``unknown_profile`` or ``no_profile_available``). ``skipped`` lists, in order, each candidate that was
    passed over and why (``disabled``, ``unavailable``, ``at_profile_cap``).
    """
    profile_id: str | None
    reason: str
    fell_back_from: str | None = None
    skipped: tuple[tuple[str, str], ...] = field(default=())


def select_profile(registry: ProfileRegistry, requested_id: str | None, load: Mapping[str, int],
                   unavailable: Collection[str] = (), *, allow_fallback: bool | None = None) -> Decision:
    """Pick the profile for a task that has NOT started a process yet. Pure and deterministic.

    ``load`` is the number of tasks currently running per profile id; ``unavailable`` the ids that must not
    be used right now (for example ``Cooldowns.unavailable()``). With no ``requested_id`` the primary is
    wanted and fallback is allowed. A task that names a profile gets fallback only if ``allow_fallback`` is
    true. Candidates are the wanted profile, then the registry's chain (primary, fallbacks) in order.
    """
    for n in load.values():
        if type(n) is not int or n < 0:
            raise ValueError("load values must be non-negative integers")
    if requested_id is not None and registry.get(requested_id) is None:
        return Decision(None, "unknown_profile")
    if sum(load.values()) >= registry.max_concurrency:
        return Decision(None, "at_global_cap")  # no other profile can help: the cap is global
    wanted = requested_id if requested_id is not None else registry.primary
    fallback_ok = (requested_id is None) if allow_fallback is None else allow_fallback
    candidates = [wanted] + ([p for p in registry.chain if p != wanted] if fallback_ok else [])
    blocked = frozenset(unavailable)
    skipped: list[tuple[str, str]] = []
    for pid in candidates:
        spec = registry.get(pid)
        if not spec.enabled:
            skipped.append((pid, "disabled"))
        elif pid in blocked:
            skipped.append((pid, "unavailable"))
        elif load.get(pid, 0) >= spec.max_concurrency:
            skipped.append((pid, "at_profile_cap"))
        else:
            if pid == wanted:
                return Decision(pid, "requested" if requested_id is not None else "primary", None, tuple(skipped))
            return Decision(pid, "fallback", wanted, tuple(skipped))
    return Decision(None, "no_profile_available", None, tuple(skipped))
