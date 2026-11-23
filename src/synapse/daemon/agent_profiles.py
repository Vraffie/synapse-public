"""Agent profiles declared by ``agentd --agent-config`` (ADR 016, W1: config and registry only).

The file is operator-owned JSON, read with the same checks as the other operator files (user-owned
regular file, not group/world writable, size bounded, duplicate keys and non-finite numbers refused).
It declares *narrower views* of the base profile given by ``--claude-cli-config``: the executable,
sandbox, repos, worktree root and approval policy always come from that base file, so this file holds
no paths and no secrets. Unknown keys are refused (a credential-looking key gets a specific message).

Every profile is a Claude profile; ``codex`` is refused until W3. The base profile is always present as
the implicit profile :data:`DEFAULT_PROFILE_ID`; without ``--agent-config`` the registry holds only it.

Profiles are synced into the existing ``profiles`` table (no migration): one row per declared profile,
never deleted (attempts and usage reference them); rows no longer declared are disabled.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from synapse.daemon.agent_config import AgentConfig
from synapse.daemon.profile_registry import (
    _ID, _MODEL, _TOOL, MAX_CONCURRENCY, MAX_PROFILES, MAX_TIMEOUT_CEILING, ProfileRegistry, ProfileSpec,
    RegistryError, _int_in, _read, registry_from_single_profile, validate_registry,
)
from synapse.persistence.agent_tasks import AGENT_PERMISSION_MODES

DEFAULT_PROFILE_ID = "default"
RESERVED_PREFIX = "synapse.builtin."
REGISTRY_REF_PREFIX = "registry:"
_REQUIRED = {"id", "provider", "allowed_tools", "permission_mode", "enabled"}
_OPTIONAL = {"model", "max_timeout_seconds", "max_concurrency"}
_SECRETISH = ("key", "token", "secret", "password", "credential", "auth")
# Least to most privileged. A task may ask for a mode no more privileged than its profile's.
MODE_RANK = {"plan": 0, "manual": 1, "acceptEdits": 2, "dontAsk": 3}


def mode_within(requested: str, ceiling: str | None) -> bool:
    return ceiling is None or (requested in MODE_RANK and MODE_RANK[requested] <= MODE_RANK[ceiling])


def _entry(raw, index: int, base: AgentConfig) -> ProfileSpec:
    where = "profiles[%d]" % index
    if not isinstance(raw, dict):
        raise RegistryError("%s must be an object" % where)
    unknown = set(raw) - _REQUIRED - _OPTIONAL
    if unknown:
        secret = sorted(k for k in unknown if any(w in str(k).lower() for w in _SECRETISH))
        if secret:
            raise RegistryError("%s: credentials do not belong in the agent config (key %s refused)" % (where, secret[0]))
        raise RegistryError("%s has unknown key %s" % (where, sorted(unknown)[0]))
    missing = _REQUIRED - set(raw)
    if missing:
        raise RegistryError("%s is missing %s" % (where, ", ".join(sorted(missing))))
    pid = raw["id"]
    if not isinstance(pid, str) or not _ID.fullmatch(pid):
        raise RegistryError("%s id must be 1-64 characters of [A-Za-z0-9._-]" % where)
    if pid == DEFAULT_PROFILE_ID or pid.startswith(RESERVED_PREFIX):
        raise RegistryError("%s id %r is reserved" % (where, pid))
    provider = raw["provider"]
    if provider == "codex":
        raise RegistryError("%s: provider 'codex' is disabled until W3 (ADR 016); only 'claude' is accepted" % where)
    if provider != "claude":
        raise RegistryError("%s provider must be 'claude'" % where)
    tools = raw["allowed_tools"]
    if (not isinstance(tools, list) or not 1 <= len(tools) <= 64
            or any(not isinstance(t, str) or not _TOOL.fullmatch(t) for t in tools) or len(set(tools)) != len(tools)):
        raise RegistryError("%s allowed_tools must be 1 to 64 unique tool names" % where)
    if not set(tools) <= set(base.profile.allowed_tools):
        raise RegistryError("%s allowed_tools must be a subset of the --claude-cli-config allowlist" % where)
    mode = raw["permission_mode"]
    if mode not in AGENT_PERMISSION_MODES or mode not in MODE_RANK:
        raise RegistryError("%s permission_mode must be one of %s" % (where, ", ".join(sorted(MODE_RANK))))
    if not isinstance(raw["enabled"], bool):
        raise RegistryError("%s enabled must be a boolean" % where)
    timeout = raw.get("max_timeout_seconds", base.profile.max_timeout_seconds)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
            or not 0 < timeout <= min(MAX_TIMEOUT_CEILING, base.profile.max_timeout_seconds)):
        raise RegistryError("%s max_timeout_seconds must be above 0 and at most the base profile's %g"
                            % (where, base.profile.max_timeout_seconds))
    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not _MODEL.fullmatch(model)):
        raise RegistryError("%s model must be a 1-64 character alias of [A-Za-z0-9._:-]" % where)
    cap = _int_in(raw.get("max_concurrency", 1), 1, MAX_CONCURRENCY, "%s max_concurrency" % where)
    return ProfileSpec(pid, "claude", base.profile.executable, tuple(sorted(tools)), cap, raw["enabled"],
                       float(timeout), model, mode)


def build_registry(base: AgentConfig, declared: tuple[ProfileSpec, ...] = ()) -> ProfileRegistry:
    """The base profile as ``default`` plus the declared ones; ``default`` is the primary."""
    default = registry_from_single_profile(base, profile_id=DEFAULT_PROFILE_ID)
    if not declared:
        return default
    return validate_registry(ProfileRegistry((*default.profiles, *declared), DEFAULT_PROFILE_ID, (),
                                             default.max_concurrency, base.repos, base.worktree_root))


def load_agent_profiles(path: Path, base: AgentConfig) -> ProfileRegistry:
    """Read ``--agent-config`` and return the registry (``default`` + declared profiles)."""
    data = _read(Path(path))
    if set(data) != {"profiles"}:
        raise RegistryError("agent config must contain exactly the key 'profiles'")
    raw = data["profiles"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_PROFILES - 1:
        raise RegistryError("profiles must be a list of 1 to %d entries (%r is implicit)"
                            % (MAX_PROFILES - 1, DEFAULT_PROFILE_ID))
    return build_registry(base, tuple(_entry(p, i, base) for i, p in enumerate(raw)))


def spec_sha256(spec: ProfileSpec) -> str:
    canonical = json.dumps({"id": spec.id, "provider": spec.provider, "allowed_tools": list(spec.allowed_tools),
                            "permission_mode": spec.permission_mode, "model": spec.model,
                            "max_timeout_seconds": spec.max_timeout_seconds, "max_concurrency": spec.max_concurrency,
                            "enabled": spec.enabled}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def sync_profiles(db: sqlite3.Connection, registry: ProfileRegistry) -> None:
    """Upsert one ``profiles`` row per declared profile (not ``default``, whose row is the built-in agent profile).

    Updates bump ``version`` only when something changed. Rows from an earlier file that are no longer
    declared are disabled, never deleted. Refuses to touch a row this module did not create.
    """
    now = datetime.now(timezone.utc).isoformat()
    declared = {s.id: s for s in registry.profiles if s.id != DEFAULT_PROFILE_ID}
    if db.in_transaction:
        raise RuntimeError("sync_profiles requires an autocommit connection")
    db.execute("BEGIN IMMEDIATE")
    try:
        for spec in declared.values():
            ref = REGISTRY_REF_PREFIX + spec_sha256(spec)
            row = db.execute("SELECT provider,config_ref,enabled,max_concurrency FROM profiles WHERE id=?",
                             (spec.id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,enabled,max_concurrency,created_at) "
                           "VALUES(?,?,?,?,?,?,?)", (spec.id, "claude", "Agent profile " + spec.id, ref,
                                                    int(spec.enabled), spec.max_concurrency, now))
            elif not str(row[1]).startswith(REGISTRY_REF_PREFIX) or row[0] != "claude":
                raise RegistryError("profile id %r collides with an existing non-registry profile" % spec.id)
            elif row[1] != ref or row[2] != int(spec.enabled) or row[3] != spec.max_concurrency:
                db.execute("UPDATE profiles SET config_ref=?,enabled=?,max_concurrency=?,version=version+1 WHERE id=?",
                           (ref, int(spec.enabled), spec.max_concurrency, spec.id))
        for (pid,) in db.execute("SELECT id FROM profiles WHERE config_ref LIKE ? AND enabled=1",
                                 (REGISTRY_REF_PREFIX + "%",)).fetchall():
            if pid not in declared:
                db.execute("UPDATE profiles SET enabled=0,version=version+1 WHERE id=?", (pid,))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
