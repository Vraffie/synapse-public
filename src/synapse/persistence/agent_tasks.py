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
    """
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
                     state_dir: str) -> ParsedSpecs:
    """Validate the closed shape and operator bounds. The worktree is checked with
    the provider adapter's own ``validate_worktree`` (fails closed)."""
    if not isinstance(raw, dict) or not raw or len(raw) > MAX_NODES:
        raise AgentSpecError("task specs must be a nonempty object with at most %d nodes" % MAX_NODES)
    specs: dict[str, dict[str, Any]] = {}
    prompts: dict[str, bytes] = {}
    seen_worktrees: set[str] = set()
    for node, spec in raw.items():
        validate_id(node)
        if not isinstance(spec, dict) or not _REQUIRED_SPEC_KEYS <= set(spec) <= _SPEC_KEYS | {"approval_step"}:
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
        step = None
        if "approval_step" in spec:
            raw_step = spec["approval_step"]
            if not isinstance(raw_step, dict) or not {"operation", "target", "policy_version"} <= set(raw_step) <= _STEP_INPUT_KEYS:
                raise AgentSpecError("approval_step has missing or unknown fields")
            try:
                # The approval binds the WHOLE spec: candidate_sha256 is the full-spec hash.
                step = ms.parse_approval_steps({node: {**raw_step, "candidate_sha256": spec_hash(recorded)}})[node]
            except ms.ScenarioError as exc:
                raise AgentSpecError(str(exc)) from exc
        specs[node] = {**recorded, "approval_step": step}
        prompts[node] = data
    return ParsedSpecs(specs, prompts)


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
    if not isinstance(spec, dict) or set(spec) - {"profile_id"} != keys:
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
    if not isinstance(spec["worktree"], str) or not spec["worktree"].startswith("/"):
        raise ValueError("bad worktree")
    tools = spec["allowed_tools"]
    if (not isinstance(tools, list) or not tools or len(tools) > MAX_ALLOWED_TOOLS or tools != sorted(set(tools))
            or any(not isinstance(t, str) for t in tools)):
        raise ValueError("bad tools")
    if spec["permission_mode"] not in AGENT_PERMISSION_MODES:
        raise ValueError("bad mode")
    _check_timeout(spec["timeout_seconds"], MAX_TIMEOUT_SECONDS)
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
