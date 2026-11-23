"""Exact-cache key and cacheability rules (M5.4 cache 1/2, ADR 017 D6). Pure: stores nothing.

This module decides, from daemon-owned facts only, whether a task's result *may* be looked up or
stored in an exact cache, and if so derives the key. It has no storage, no I/O and no caller yet:
the ``result_cache`` table and the ``cache.lookup`` / ``cache.stored`` events (ADR 017 D2, D3) need
a migration that is blocked on migration numbering, so nothing is ever cached today.

Rules (every "no" is a bypass with a reason, never an error the caller must handle):

- Default is **no**: a task is cacheable only if its class is ``read_only_analysis`` *and* an
  operator policy marked it cacheable. Edit, review, verification, validation and independent-check
  tasks are never cacheable; an unknown class is not cacheable.
- Any approval-gated task bypasses with ``approval_gated``.
- Tools: only the read-only allowlist below. ``Bash`` in any form, write-capable tools and unknown
  tools bypass. ``acceptEdits`` / ``bypassPermissions`` / ``auto`` / unknown permission modes bypass.
- The manifest must have a known head and a worktree known to be clean (dirty or unknown: bypass).
- A missing tool/CLI version bypasses (a version change must be able to miss).
- Only a ``succeeded`` attempt that was not budget-blocked may be stored (``store_decision``).
- A hit is never a verification result and never satisfies, extends or inherits an approval
  (``CacheHit``): verifiers still run on the candidate and approvals are still required.

The key is SHA-256 of canonical JSON over the key material: cache schema version, policy version,
approval-policy hash, provider, profile, CLI version, model (``None`` = unknown, distinct from any
model), sorted tool list, permission mode, settings hash, spec hash, manifest hash, repo head,
clean marker, task class and sorted validator hashes. Exact equality only: nothing is normalised
beyond ordering of sets, so there is no semantic reuse. Settings are hashed and only the digest is
kept; a settings mapping whose key names look like credentials is refused outright.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import re
from typing import Any, ClassVar

from synapse.application.context_manifest import ContextManifest
from synapse.persistence.idempotency import canonical_request_hash

CACHE_SCHEMA_VERSION = 1
# ADR 017 D6 lists only read-only analysis (operator-marked) among task results that may be cached; a
# "validation" task is a check on a candidate, which D6 puts with verifications: never cacheable.
CACHEABLE_CLASSES = frozenset({"read_only_analysis"})
NEVER_CACHEABLE_CLASSES = frozenset({"edit", "review", "verification", "validation", "independent_check"})
READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "LS"})
WRITE_CAPABLE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "WebFetch", "WebSearch", "Task"})
CACHEABLE_MODES = frozenset({"plan", "dontAsk", "manual"})
STORABLE_OUTCOMES = frozenset({"succeeded"})

MAX_TEXT = 256
MAX_TOOLS = 64
MAX_VALIDATORS = 64
MAX_SETTINGS_BYTES = 64 * 1024
MAX_SETTINGS_DEPTH = 8
MAX_SETTINGS_NODES = 2048  # counted during the walk, before any serialisation: bounds breadth

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEAD = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SENSITIVE_KEY = re.compile(r"token|secret|credential|passw|api[_-]?key|authorization|auth|cookie|session|private[_-]?key",
                            re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CacheKeyInputs:
    """Daemon-owned facts about one attempt. The agent never supplies any of these."""

    task_class: str
    operator_marked_cacheable: bool
    approval_gated: bool
    policy_version: str
    approval_policy_sha256: str
    provider: str
    profile_id: str
    cli_version: str | None
    model: str | None
    allowed_tools: Sequence[str]
    permission_mode: str
    settings: Mapping[str, Any]
    spec_sha256: str
    manifest: ContextManifest
    validator_sha256s: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class CacheKey:
    sha256: str
    material: Mapping[str, Any]  # hashes, ids, versions only: safe to put in a cache.lookup event


@dataclass(frozen=True, slots=True)
class CacheBypass:
    reason: str


CacheDecision = CacheKey | CacheBypass


@dataclass(frozen=True, slots=True)
class CacheHit:
    """A served entry. It is a reused output, never evidence and never authority."""

    key_sha256: str
    origin_attempt_id: str
    counts_as_verification: ClassVar[bool] = False
    satisfies_approval: ClassVar[bool] = False
    requires_verification: ClassVar[bool] = True


def _text(value: object, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT or _CONTROL.search(value):
        raise ValueError(field)
    return value


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(field)
    return value


def _settings_digest(settings: object) -> str:
    if not isinstance(settings, Mapping):
        raise ValueError("settings")

    nodes = 0

    def walk(value: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_SETTINGS_NODES:
            raise ValueError("settings_nodes")
        if depth > MAX_SETTINGS_DEPTH:
            raise ValueError("settings")
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("settings")
                if _SENSITIVE_KEY.search(key):
                    raise PermissionError("credential_in_settings")
                walk(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, depth + 1)
        elif not (value is None or isinstance(value, (str, bool, int, float))):
            raise ValueError("settings")

    walk(settings, 0)
    try:
        encoded = json.dumps(settings, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("settings") from exc
    if len(encoded.encode("utf-8")) > MAX_SETTINGS_BYTES:
        raise ValueError("settings")
    return canonical_request_hash(json.loads(encoded))


def _tool_problem(tools: Sequence[str]) -> str | None:
    for tool in tools:
        name = tool.split("(", 1)[0].strip()
        if name in READ_ONLY_TOOLS and name == tool:
            continue
        if name == "Bash":
            return "bash_tool"
        if name in WRITE_CAPABLE_TOOLS:
            return "write_capable_tool"
        return "unknown_tool"
    return None


def derive_cache_key(inputs: CacheKeyInputs) -> CacheDecision:
    """Return the exact cache key, or a bypass with its reason. Never raises: any doubt bypasses."""
    try:
        return _derive(inputs)
    except PermissionError as exc:
        return CacheBypass(str(exc))
    except Exception as exc:  # fail closed: malformed input never reaches a cache
        field = exc.args[0] if isinstance(exc, ValueError) and exc.args and isinstance(exc.args[0], str) else "input"
        return CacheBypass(f"invalid_input:{field}" if re.fullmatch(r"[a-z0-9_]{1,40}", field) else "invalid_input")


def _derive(i: CacheKeyInputs) -> CacheDecision:
    if not isinstance(i, CacheKeyInputs):
        raise ValueError("inputs")
    task_class = _text(i.task_class, "task_class")
    if task_class in NEVER_CACHEABLE_CLASSES:
        return CacheBypass(f"{task_class}_task")
    if task_class not in CACHEABLE_CLASSES:
        return CacheBypass("unknown_task_class")
    if i.operator_marked_cacheable is not True:
        return CacheBypass("not_marked_cacheable")
    if i.approval_gated is not False:
        return CacheBypass("approval_gated")

    if isinstance(i.allowed_tools, (str, bytes)) or not isinstance(i.allowed_tools, Sequence):
        raise ValueError("allowed_tools")
    if not i.allowed_tools or len(i.allowed_tools) > MAX_TOOLS:
        raise ValueError("allowed_tools")
    tools = [_text(t, "allowed_tools") for t in i.allowed_tools]
    problem = _tool_problem(tools)
    if problem is not None:
        return CacheBypass(problem)
    mode = _text(i.permission_mode, "permission_mode")
    if mode not in CACHEABLE_MODES:
        return CacheBypass("write_capable_mode" if mode in {"acceptEdits", "bypassPermissions", "auto"} else "unknown_mode")

    manifest = i.manifest
    if not isinstance(manifest, ContextManifest):
        raise ValueError("manifest")
    if manifest.repo_head is None or not _HEAD.fullmatch(manifest.repo_head):
        return CacheBypass("unknown_head")
    if manifest.worktree_clean is None:
        return CacheBypass("unknown_cleanliness")
    if manifest.worktree_clean is not True:
        return CacheBypass("dirty_worktree")

    if i.cli_version is None:
        return CacheBypass("unknown_tool_version")
    if isinstance(i.validator_sha256s, (str, bytes)) or not isinstance(i.validator_sha256s, Sequence) \
            or len(i.validator_sha256s) > MAX_VALIDATORS:
        raise ValueError("validator_sha256s")
    material = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "policy_version": _text(i.policy_version, "policy_version"),
        "approval_policy_sha256": _sha(i.approval_policy_sha256, "approval_policy_sha256"),
        "provider": _text(i.provider, "provider"),
        "profile_id": _text(i.profile_id, "profile_id"),
        "cli_version": _text(i.cli_version, "cli_version"),
        "model": _text(i.model, "model", optional=True),
        "allowed_tools": sorted(set(tools)),
        "permission_mode": mode,
        "settings_sha256": _settings_digest(i.settings),
        "spec_sha256": _sha(i.spec_sha256, "spec_sha256"),
        "manifest_sha256": manifest.sha256,
        "repo_head": manifest.repo_head,
        "worktree_clean": True,
        "task_class": task_class,
        "validator_sha256s": sorted({_sha(v, "validator_sha256s") for v in i.validator_sha256s}),
    }
    return CacheKey(canonical_request_hash(material), material)


def store_decision(decision: CacheDecision, *, outcome: str, budget_blocked: bool) -> CacheBypass | None:
    """Whether a finished attempt's result may be stored under ``decision``; None means it may."""
    if not isinstance(decision, CacheKey):
        return decision if isinstance(decision, CacheBypass) else CacheBypass("invalid_input")
    if budget_blocked is not False:
        return CacheBypass("budget_blocked")
    if outcome not in STORABLE_OUTCOMES:
        return CacheBypass(f"outcome_{outcome}" if outcome in {"unknown", "failed", "cancelled", "timed_out"}
                           else "outcome_unrecognised")
    return None


def may_serve(decision: CacheDecision, hit: CacheHit) -> bool:
    """True only when the current attempt is cacheable and the stored key is exactly its key."""
    return (isinstance(decision, CacheKey) and isinstance(hit, CacheHit)
            and isinstance(hit.key_sha256, str) and hit.key_sha256 == decision.sha256
            and isinstance(hit.origin_attempt_id, str) and bool(hit.origin_attempt_id.strip()))
