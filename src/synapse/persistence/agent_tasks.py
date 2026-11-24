"""Per-run real-agent task specs: closed, validated, stored immutably.

A task spec is supplied by a trusted runtime caller (never by an agent). Each
spec is validated against the operator's Claude CLI profile and recorded once as
a hashed ``agent.tasks_configured`` event in the transaction that starts the run
(events are immutable). The prompt text is NOT placed in the event stream: it is
stored as a private artifact blob, and the event carries only its SHA-256, byte
length and artifact id. A run is only understood as an agent run when its
profile, its single config event and every referenced prompt artifact agree.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import sqlite3
from typing import Any

from synapse.domain.contracts import EventEnvelope, encode_payload, validate_id
from synapse.persistence import mock_scenarios as ms
from synapse.providers.claude_cli import PERMISSION_MODES, validate_worktree

AGENT_KIND = "agent-v1"
AGENT_PROFILE = "synapse.builtin.agent.claude.v1"
EVENT_TYPE = "agent.tasks_configured"
MAX_PROMPT_BYTES = 32 * 1024
MAX_TIMEOUT_SECONDS = 3600
MAX_NODES = 16
MAX_ALLOWED_TOOLS = 50  # same bound as the HTTP service; the approver subject lists every tool
# bypassPermissions removes every guardrail; auto hands decisions to the CLI itself.
AGENT_PERMISSION_MODES = frozenset(PERMISSION_MODES - {"bypassPermissions", "auto"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SPEC_KEYS = {"prompt", "worktree", "allowed_tools", "permission_mode", "timeout_seconds",
              "repo", "base_ref", "worktree_head", "profile_id"}
_REQUIRED_SPEC_KEYS = {"prompt", "worktree", "allowed_tools", "permission_mode", "timeout_seconds"}
_PROFILE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_HEAD = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
SPEC_HASH_VERSION = 1
_STEP_INPUT_KEYS = {"operation", "target", "policy_version", "ttl_seconds"}


class AgentSpecError(ValueError):
    """The supplied agent task specs are invalid for this run or operator profile."""


# --- multi-repository task specs (ADR 026 D5) ------------------------------------------------------------------------
# A multi-repository spec carries ``repos: [{repo_id, access}]`` and NO worktree, worktree_head, repo or base_ref: the
# pinned baselines live in run_repo_bindings and each attempt's paths in attempt_worktrees. The recorded spec also
# carries the run's ``bindings_sha256`` so the full-spec hash (what an approval binds) covers the declared
# repositories, their access modes AND their pinned baselines.
MAX_SPEC_REPOS = 16
_REPO_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MULTI_SPEC_KEYS = {"prompt", "allowed_tools", "permission_mode", "timeout_seconds", "repos", "profile_id"}
_MULTI_REQUIRED = {"prompt", "allowed_tools", "permission_mode", "timeout_seconds", "repos"}


def check_repo_decls(value: Any) -> list[dict[str, str]]:
    """``[{repo_id, access}]``: 1 to 16 distinct repositories sorted by repo_id, access ``write`` or ``read``."""
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_SPEC_REPOS:
        raise ValueError("repos must list 1 to %d repositories" % MAX_SPEC_REPOS)
    out = []
    for r in value:
        if not isinstance(r, dict) or set(r) != {"repo_id", "access"}:
            raise ValueError("each repos entry must be {repo_id, access}")
        rid = r["repo_id"]
        if not isinstance(rid, str) or not _REPO_ID.fullmatch(rid) or ".." in rid or rid.endswith((".", ".lock")):
            raise ValueError("repo_id is invalid")
        if r["access"] not in ("write", "read"):
            raise ValueError("access must be write or read")
        out.append({"access": r["access"], "repo_id": rid})
    ids = [r["repo_id"] for r in out]
    if ids != sorted(set(ids)):
        raise ValueError("repos must be distinct and sorted by repo_id")
    return out


def is_multi_repo(spec: Any) -> bool:
    return isinstance(spec, dict) and "repos" in spec


@dataclass(frozen=True, slots=True)
class ParsedSpecs:
    specs: dict[str, dict[str, Any]]   # canonical, without artifact ids
    prompts: dict[str, bytes]          # node -> prompt bytes


def spec_hash(spec: dict[str, Any]) -> str:
    """Canonical SHA-256 of the WHOLE execution spec an approval must bind (security review F6).

    Covers the prompt digest, repo, base ref, worktree identity (path and the HEAD recorded when it
    was created), tool set, permission mode and timeout. The approval step itself (operation, target,
    ttl) is bound separately by the approval row. Changing any covered field changes this digest, so
    the execution-time binding recheck fails closed.

    A multi-repository spec (``repos`` present) hashes a different, closed body: kind ``agent_run_multi``, the
    declared repositories and access modes, and the run's ``bindings_sha256`` (pinned baselines); no path or HEAD.
    """
    if "repos" in spec:
        multi = {"v": SPEC_HASH_VERSION, "kind": "agent_run_multi", "prompt_sha256": spec["prompt_sha256"],
                 "repos": spec["repos"], "bindings_sha256": spec["bindings_sha256"],
                 "allowed_tools": sorted(spec["allowed_tools"]), "permission_mode": spec["permission_mode"],
                 "timeout_seconds": float(spec["timeout_seconds"])}
        if spec.get("profile_id") is not None:
            multi["profile_id"] = spec["profile_id"]
        return hashlib.sha256(json.dumps(multi, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    body = {"v": SPEC_HASH_VERSION, "prompt_sha256": spec["prompt_sha256"], "repo": spec.get("repo"),
            "base_ref": spec.get("base_ref"), "worktree": spec["worktree"], "worktree_head": spec.get("worktree_head"),
            "allowed_tools": sorted(spec["allowed_tools"]), "permission_mode": spec["permission_mode"],
            "timeout_seconds": float(spec["timeout_seconds"])}
    if spec.get("profile_id") is not None:  # only a non-default profile, so default specs keep their v1 hash
        body["profile_id"] = spec["profile_id"]
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _check_optional_text(spec: dict[str, Any], key: str, limit: int, absolute: bool = False) -> str | None:
    value = spec.get(key)
    if value is None:
        return None
    if (not isinstance(value, str) or not value or len(value) > limit or "\x00" in value
            or (absolute and not value.startswith("/"))):
        raise AgentSpecError("%s is invalid" % key)
    return value


def _check_timeout(value: Any, ceiling: float) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not 0 < value <= min(ceiling, MAX_TIMEOUT_SECONDS)):
        raise AgentSpecError("timeout_seconds must be a positive number within the operator and global bounds")


def parse_task_specs(raw: Any, *, operator_tools: tuple[str, ...], max_timeout: float,
                     state_dir: str, bindings_sha256: str | None = None) -> ParsedSpecs:
    """Validate the closed shape and operator bounds. The worktree is checked with
    the provider adapter's own ``validate_worktree`` (fails closed)."""
    if not isinstance(raw, dict) or not raw or len(raw) > MAX_NODES:
        raise AgentSpecError("task specs must be a nonempty object with at most %d nodes" % MAX_NODES)
    specs: dict[str, dict[str, Any]] = {}
    prompts: dict[str, bytes] = {}
    seen_worktrees: set[str] = set()
    for node, spec in raw.items():
        validate_id(node)
        multi = is_multi_repo(spec)
        if multi:
            if not _MULTI_REQUIRED <= set(spec) <= _MULTI_SPEC_KEYS | {"approval_step"}:
                raise AgentSpecError("multi-repository task spec has missing or unknown fields")
            if not isinstance(bindings_sha256, str) or not _SHA256.fullmatch(bindings_sha256):
                raise AgentSpecError("a multi-repository task needs the run's repository bindings")
        elif not isinstance(spec, dict) or not _REQUIRED_SPEC_KEYS <= set(spec) <= _SPEC_KEYS | {"approval_step"}:
            raise AgentSpecError("task spec has missing or unknown fields")
        prompt = spec["prompt"]
        if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
            raise AgentSpecError("prompt must be a nonempty string without NUL")
        try:
            data = prompt.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise AgentSpecError("prompt must be valid UTF-8") from exc
        if len(data) > MAX_PROMPT_BYTES:
            raise AgentSpecError("prompt exceeds %d bytes" % MAX_PROMPT_BYTES)
        tools = spec["allowed_tools"]
        if not isinstance(tools, list) or not tools or any(not isinstance(t, str) for t in tools):
            raise AgentSpecError("allowed_tools must be a nonempty list of strings")
        if len(tools) > MAX_ALLOWED_TOOLS:
            raise AgentSpecError("allowed_tools must not exceed %d entries" % MAX_ALLOWED_TOOLS)
        if not set(tools) <= set(operator_tools):
            raise AgentSpecError("allowed_tools must be a subset of the operator allowlist")
        mode = spec["permission_mode"]
        if mode not in AGENT_PERMISSION_MODES:
            raise AgentSpecError("permission_mode must be one of %s" % ", ".join(sorted(AGENT_PERMISSION_MODES)))
        _check_timeout(spec["timeout_seconds"], max_timeout)
        profile_id = spec.get("profile_id")
        if profile_id is not None and (not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id)
                                       or profile_id == "default"):  # the default is represented by absence
            raise AgentSpecError("profile_id is invalid")
        digest = hashlib.sha256(data).hexdigest()
        if multi:
            try:
                repos = check_repo_decls(spec["repos"])
            except ValueError as exc:
                raise AgentSpecError("invalid repos: %s" % exc) from exc
            recorded = {"prompt_sha256": digest, "prompt_bytes": len(data), "repos": repos,
                        "bindings_sha256": bindings_sha256, "allowed_tools": sorted(set(tools)),
                        "permission_mode": mode, "timeout_seconds": spec["timeout_seconds"]}
            if profile_id is not None:
                recorded["profile_id"] = profile_id
            specs[node] = {**recorded, "approval_step": _approval_step(node, spec, recorded)}
            prompts[node] = data
            continue
        try:
            worktree = validate_worktree(spec["worktree"], state_dir)
        except ValueError as exc:
            raise AgentSpecError("invalid worktree: %s" % exc) from exc
        if worktree in seen_worktrees:
            raise AgentSpecError("tasks must not share a worktree")
        seen_worktrees.add(worktree)
        digest = hashlib.sha256(data).hexdigest()
        repo = _check_optional_text(spec, "repo", 4096, absolute=True)
        base_ref = _check_optional_text(spec, "base_ref", 256)
        profile_id = spec.get("profile_id")
        if profile_id is not None and (not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id)
                                       or profile_id == "default"):  # the default is represented by absence
            raise AgentSpecError("profile_id is invalid")
        head = spec.get("worktree_head")
        if head is not None and (not isinstance(head, str) or not _HEAD.fullmatch(head)):
            raise AgentSpecError("worktree_head must be a lowercase hex commit id")
        if "approval_step" in spec and (repo is None or base_ref is None or head is None):
            # Fail closed: an approval must bind the worktree identity and enable the spawn-time recheck.
            raise AgentSpecError("an approval_step requires repo, base_ref and worktree_head")
        recorded = {"prompt_sha256": digest, "prompt_bytes": len(data), "repo": repo, "base_ref": base_ref,
                    "worktree": worktree, "worktree_head": head,
                    "allowed_tools": sorted(set(tools)), "permission_mode": mode,
                    "timeout_seconds": spec["timeout_seconds"]}
        if profile_id is not None:
            recorded["profile_id"] = profile_id
        specs[node] = {**recorded, "approval_step": _approval_step(node, spec, recorded)}
        prompts[node] = data
    return ParsedSpecs(specs, prompts)


def _approval_step(node: str, spec: dict[str, Any], recorded: dict[str, Any]) -> dict[str, Any] | None:
    if "approval_step" not in spec:
        return None
    raw_step = spec["approval_step"]
    if not isinstance(raw_step, dict) or not {"operation", "target", "policy_version"} <= set(raw_step) <= _STEP_INPUT_KEYS:
        raise AgentSpecError("approval_step has missing or unknown fields")
    try:
        # The approval binds the WHOLE spec: candidate_sha256 is the full-spec hash.
        return ms.parse_approval_steps({node: {**raw_step, "candidate_sha256": spec_hash(recorded)}})[node]
    except ms.ScenarioError as exc:
        raise AgentSpecError(str(exc)) from exc


def _check_multi_shape(node: str, spec: dict[str, Any]) -> None:
    keys = {"prompt_sha256", "prompt_bytes", "prompt_artifact_id", "repos", "bindings_sha256", "allowed_tools",
            "permission_mode", "timeout_seconds", "approval_step"}
    if set(spec) - {"profile_id"} != keys:
        raise ValueError("bad spec keys")
    if check_repo_decls(spec["repos"]) != spec["repos"]:
        raise ValueError("bad repos")
    if not isinstance(spec["bindings_sha256"], str) or not _SHA256.fullmatch(spec["bindings_sha256"]):
        raise ValueError("bad bindings digest")


def _sha(tasks: dict[str, dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(tasks, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def check_nodes_for_run(db: sqlite3.Connection, run_id: str, specs: dict[str, Any]) -> None:
    """A real-agent run is closed: every node needs a spec and no spec may name another node."""
    nodes = {row[0] for row in db.execute("SELECT node_id FROM tasks WHERE run_id=?", (run_id,))}
    if not nodes or set(specs) != nodes:
        raise AgentSpecError("task specs must name exactly the run's nodes")


def record_task_specs(db: sqlite3.Connection, run_id: str, tasks: dict[str, dict[str, Any]],
                      prompts: dict[str, bytes], artifact_ids: dict[str, str], now: str) -> None:
    """Insert prompt artifact rows and the one immutable config event (caller's transaction).

    ``tasks`` must already carry ``prompt_artifact_id`` per node; blobs were stored by the caller.
    """
    if not db.in_transaction:
        raise RuntimeError("Recording agent task specs requires a transaction")
    for node, data in prompts.items():
        spec = tasks[node]
        db.execute("INSERT INTO artifacts(id,sha256,media_type,byte_size,storage_path,provenance_json,created_at) VALUES(?,?,?,?,?,?,?)",
                   (artifact_ids[node], spec["prompt_sha256"], "text/plain", len(data), spec["prompt_sha256"],
                    json.dumps({"kind": "agent_prompt", "run_id": run_id, "node": node}, sort_keys=True), now))
    event = EventEnvelope.create(aggregate_type="run", aggregate_id=run_id, event_type=EVENT_TYPE,
                                 payload={"run_id": run_id, "kind": AGENT_KIND, "tasks": tasks, "sha256": _sha(tasks)})
    db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
               (event.id, event.aggregate_type, run_id, run_id, event.event_type, event.schema_version,
                encode_payload(event.payload), event.occurred_at))
    db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))


def _check_shape(node: str, spec: Any) -> None:
    keys = {"prompt_sha256", "prompt_bytes", "prompt_artifact_id", "worktree", "allowed_tools",
            "permission_mode", "timeout_seconds", "approval_step", "repo", "base_ref", "worktree_head"}
    if is_multi_repo(spec):
        _check_multi_shape(node, spec)
    elif not isinstance(spec, dict) or set(spec) - {"profile_id"} != keys:
        raise ValueError("bad spec keys")
    if "profile_id" in spec and (not isinstance(spec["profile_id"], str) or not _PROFILE_ID.fullmatch(spec["profile_id"])
                                 or spec["profile_id"] == "default"):
        raise ValueError("bad profile id")
    validate_id(node)
    validate_id(spec["prompt_artifact_id"])
    if not isinstance(spec["prompt_sha256"], str) or not _SHA256.fullmatch(spec["prompt_sha256"]):
        raise ValueError("bad digest")
    if type(spec["prompt_bytes"]) is not int or not 1 <= spec["prompt_bytes"] <= MAX_PROMPT_BYTES:
        raise ValueError("bad length")
    if "repos" not in spec and (not isinstance(spec["worktree"], str) or not spec["worktree"].startswith("/")):
        raise ValueError("bad worktree")
    tools = spec["allowed_tools"]
    if (not isinstance(tools, list) or not tools or len(tools) > MAX_ALLOWED_TOOLS or tools != sorted(set(tools))
            or any(not isinstance(t, str) for t in tools)):
        raise ValueError("bad tools")
    if spec["permission_mode"] not in AGENT_PERMISSION_MODES:
        raise ValueError("bad mode")
    _check_timeout(spec["timeout_seconds"], MAX_TIMEOUT_SECONDS)
    if "repos" in spec:
        step = spec["approval_step"]
        if step is not None and (ms.parse_approval_steps({node: step}) != {node: step}
                                 or step["candidate_sha256"] != spec_hash(spec)):
            raise ValueError("bad approval step")
        return
    try:
        for key, limit, absolute in (("repo", 4096, True), ("base_ref", 256, False)):
            _check_optional_text(spec, key, limit, absolute)
    except AgentSpecError as exc:
        raise ValueError(str(exc)) from exc
    if spec["worktree_head"] is not None and (not isinstance(spec["worktree_head"], str) or not _HEAD.fullmatch(spec["worktree_head"])):
        raise ValueError("bad worktree head")
    step = spec["approval_step"]
    if step is not None:
        if spec["repo"] is None or spec["base_ref"] is None or spec["worktree_head"] is None:
            raise ValueError("approval step without worktree identity")
        if ms.parse_approval_steps({node: step}) != {node: step} or step["candidate_sha256"] != spec_hash(spec):
            raise ValueError("bad approval step")


def load_task_specs(db: sqlite3.Connection, run_id: str) -> dict[str, dict[str, Any]] | None:
    """The run's single recorded specs keyed by node, or None when not trustworthy."""
    rows = db.execute("SELECT payload_json FROM events WHERE run_id=? AND type=?", (run_id, EVENT_TYPE)).fetchall()
    if len(rows) != 1:
        return None
    try:
        payload = json.loads(rows[0][0])
        tasks = payload["tasks"]
        if (payload["run_id"] != run_id or payload["kind"] != AGENT_KIND or not isinstance(tasks, dict)
                or not tasks or payload["sha256"] != _sha(tasks)):
            return None
        nodes = {row[0] for row in db.execute("SELECT node_id FROM tasks WHERE run_id=?", (run_id,))}
        if set(tasks) != nodes:
            return None
        multi = [s for s in tasks.values() if is_multi_repo(s)]
        if multi:
            # Every task of a multi-repository run declares repositories, and every spec names THIS run's bindings.
            from synapse.persistence.repo_bindings import bindings_digest, load_run_repos
            bound = load_run_repos(db, run_id)
            if len(multi) != len(tasks) or not bound:
                return None
            if any(s.get("bindings_sha256") != bindings_digest(bound.values()) for s in multi):
                return None
        for node, spec in tasks.items():
            _check_shape(node, spec)
            art = db.execute("SELECT sha256,byte_size FROM artifacts WHERE id=?", (spec["prompt_artifact_id"],)).fetchone()
            if art != (spec["prompt_sha256"], spec["prompt_bytes"]):
                return None
    except (ValueError, KeyError, TypeError):
        return None
    return tasks


def is_agent_run(db: sqlite3.Connection, run_id: str) -> bool:
    """True only for a consistent real-agent run (profile, config row and spec event agree)."""
    row = db.execute("SELECT c.profile_id,c.fixture,p.provider FROM mock_run_config c "
                     "LEFT JOIN profiles p ON p.id=c.profile_id WHERE c.run_id=?", (run_id,)).fetchone()
    if row is None or row[0] != AGENT_PROFILE or row[1] != ms.FIXED_KIND or row[2] != "claude":
        return False
    return load_task_specs(db, run_id) is not None
