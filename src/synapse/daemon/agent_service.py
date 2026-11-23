"""Operator-facing orchestration of a real agent run (API/CLI backend).

``AgentRunService.start`` turns one validated operator request into: a
single-node workflow, a queued run, a git worktree and ``AgentRuntime.start``.
Everything that can be checked without side effects is checked first; a failure
after the worktree exists removes it and cancels the not-yet-started run. The
prompt is only ever handed to ``AgentRuntime.start`` (private artifact blob);
it is never logged, audited or placed in an event by this module.
See docs/operations/agent-runs.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone

from synapse.api.agent_commands import AgentRunError
from synapse.daemon.agent_profiles import DEFAULT_PROFILE_ID, mode_within
from synapse.daemon.agent_runtime import AgentDisabled, AgentRuntime
from synapse.daemon.profile_registry import Cooldowns, ProfileRegistry, ProfileSpec
from synapse.domain.contracts import EventEnvelope, encode_payload
from synapse.domain.runs import RunChange, RunState
from synapse.domain.workflow import State, StateChange
from synapse.infrastructure.worktrees import BoundedText, WorktreeError, WorktreeManager
from synapse.persistence import agent_tasks as at
from synapse.persistence import mock_scenarios as ms
from synapse.persistence.attempt_resume import resumed_count
from synapse.persistence.idempotency import CommandResult, IdempotencyConflict, canonical_request_hash, execute_idempotent
from synapse.persistence.run_snapshot import RunNotFound
from synapse.persistence.run_store import write_run_transition
from synapse.persistence.state_store import StateConflict, write_task_transition
from synapse.persistence.workflow_store import create_run, publish_workflow
from synapse.providers.codex_cli import CodexIneligible, sandbox_for_task

_LOG = logging.getLogger(__name__)
PRINCIPAL = "local-operator"
OPERATION = "agent.run.start"
WORKFLOW_ID = "synapse.agent.single"
WORKFLOW_KEY = "agent-run-single-node-workflow-v1"
NODE = "agent"
_FINISHED_RUN_STATES = frozenset({"succeeded", "failed", "cancelled"})
POLICY_VERSION = "1"
DEFAULT_TIMEOUT = 600
MAX_TOOLS = at.MAX_ALLOWED_TOOLS
MAX_TASKS = 6
_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_RETRIES = 20
SHOW_TEXT_BYTES = 8 * 1024
SHOW_DIFF_BYTES = 16 * 1024
# Conservative ref grammar (a subset of what git accepts): no option-like, whitespace,
# control or glob characters, no '..', '@{', trailing '.lock' or '/'.
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@+~^-]{0,255}")
_TOKEN = re.compile(r"[A-Za-z0-9._:/@+-]{1,128}")


def profile_policy(runtime):
    return runtime.approval_policy


def worktree_checker(manager: WorktreeManager):
    """Blocking ``(run_id, task_id, path) -> (HEAD sha, clean)`` for the runtime's spawn-time recheck.

    ``path`` is the worktree the task will be spawned in; it must be the very directory the manager
    inspects for (run_id, task_id), otherwise the check would vouch for a different tree (fails closed).
    """
    def check(run_id: str, task_id: str, path: str) -> tuple[str, bool]:
        if manager.worktree_path(run_id, task_id) != path:
            raise WorktreeError("spawn path differs from the inspected worktree")
        status = manager.status(run_id, task_id, max_bytes=4096)
        return status.head, status.clean and not status.truncated
    return check


def _bad(message: str) -> AgentRunError:
    return AgentRunError(422, message)


class AgentRunService:
    def __init__(self, db: sqlite3.Connection, runtime: AgentRuntime, manager: WorktreeManager | None,
                 registry: ProfileRegistry | None = None):
        self.db, self.runtime, self.manager = db, runtime, manager
        self.registry = registry  # None: only the implicit default profile (the --claude-cli-config one)
        self._start_lock = asyncio.Lock()

    # ------------------------------------------------------------ validation
    @property
    def configured(self) -> bool:
        profile = self.runtime.profile
        return self.manager is not None and profile is not None and profile.enabled

    def _require_configured(self) -> None:
        if not self.configured:
            raise AgentRunError(503, "Agent runs are not configured (enable the Claude CLI config with repos and worktree_root)")

    def resolve_profile(self, profile_id: str | None = None) -> ProfileSpec:
        """The registry profile a request names (``None`` or ``default``: the --claude-cli-config profile).

        Raises a 422 for an unknown or disabled profile. Does not select fallbacks and starts nothing.
        """
        if profile_id is None or profile_id == DEFAULT_PROFILE_ID:
            base = self.runtime.profile
            if self.registry is not None and self.registry.get(DEFAULT_PROFILE_ID) is not None:
                return self.registry.get(DEFAULT_PROFILE_ID)
            return ProfileSpec(DEFAULT_PROFILE_ID, "claude", base.executable, tuple(base.allowed_tools), 1,
                               base.enabled, float(base.max_timeout_seconds))
        spec = None if self.registry is None or not isinstance(profile_id, str) else self.registry.get(profile_id)
        if spec is None:
            raise _bad("unknown profile")
        if not spec.enabled:
            raise _bad("profile is disabled")
        if spec.provider not in ("claude", "codex"):
            raise _bad("profile provider is not supported")
        if spec.provider == "codex" and getattr(self.runtime.profile, "sandbox", True) is not False:
            raise _bad("codex profiles are refused while the base profile is sandboxed")
        return spec

    def validate(self, req: dict, profile_id: str | None = None) -> dict:
        """Pure checks of the body against the operator profile. Returns the canonical request.

        Either the legacy single-task body (``prompt`` ...) or ``tasks`` (1..MAX_TASKS tasks with
        ``depends_on``); the result always carries ``tasks`` plus ``legacy`` (single-task shape).
        """
        profile_id = profile_id if profile_id is not None else req.get("profile")
        spec = self.resolve_profile(profile_id)
        ref = req["base_ref"]
        if (not isinstance(ref, str) or not _REF.fullmatch(ref) or ".." in ref or "@{" in ref
                or ref.endswith((".lock", "/", ".")) or "//" in ref):
            raise _bad("invalid base_ref")
        if req.get("tasks") is not None:
            if any(req.get(k) is not None for k in ("prompt", "allowed_tools")):
                raise _bad("give either tasks or a single prompt, not both")
            raw = req["tasks"]
            if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TASKS:
                raise _bad("tasks must contain 1 to %d entries" % MAX_TASKS)
            tasks = []
            for t in raw:
                # A task may name its own profile (ADR 016): tools, mode and timeout are checked against it.
                tspec = spec if not isinstance(t, dict) or t.get("profile") is None else self.resolve_profile(t["profile"])
                tasks.append(self._validate_task(
                    t, default_mode=req.get("permission_mode", tspec.permission_mode or "acceptEdits"),
                    default_timeout=req.get("timeout_seconds", DEFAULT_TIMEOUT), spec=tspec))
            self._check_graph(tasks)
            return {"repo": req["repo"], "base_ref": ref, "tasks": tasks, "legacy": False,
                    "profile": self._explicit_profile(spec)}
        task = self._validate_task({**{k: req[k] for k in ("prompt", "allowed_tools", "permission_mode", "timeout_seconds")},
                                    "approval_step": req.get("approval_step"), "id": NODE, "depends_on": []},
                                   default_mode=None, default_timeout=None, spec=spec)
        return {"repo": req["repo"], "base_ref": ref, "tasks": [task], "legacy": True,
                "profile": self._explicit_profile(spec)}

    @staticmethod
    def _explicit_profile(spec: ProfileSpec) -> str | None:
        """The id only for a non-default profile, so requests without one keep their old fingerprint."""
        return None if spec.id == DEFAULT_PROFILE_ID else spec.id

    def _validate_task(self, t: dict, *, default_mode, default_timeout, spec: ProfileSpec) -> dict:
        profile = spec
        node = t.get("id")
        if not isinstance(node, str) or not _NODE_ID.fullmatch(node):
            raise _bad("task id must be 1-64 characters of [A-Za-z0-9._-]")
        prompt = t.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
            raise _bad("prompt must be a nonempty string without NUL")
        try:
            data = prompt.encode("utf-8", "strict")
        except UnicodeEncodeError:
            raise _bad("prompt must be valid UTF-8") from None
        if len(data) > at.MAX_PROMPT_BYTES:
            raise _bad("prompt is too large")
        tools = t.get("allowed_tools")
        if (not isinstance(tools, list) or not 1 <= len(tools) <= MAX_TOOLS
                or any(not isinstance(x, str) for x in tools)):
            raise _bad("allowed_tools must be a nonempty list of tool names")
        mode = t.get("permission_mode", default_mode)
        if spec.provider == "codex":
            # Codex has no tool allowlist: tools and mode only select a sandbox class (ADR 016 decision 4).
            # Anything it cannot express (Bash patterns, other tools, dontAsk/manual, Edit/Write under plan) is a 422.
            try:
                sandbox_for_task(tools, mode)
            except CodexIneligible as exc:
                raise _bad(str(exc)) from None
        if not set(tools) <= set(profile.allowed_tools):
            raise _bad("allowed_tools must be a subset of the operator allowlist")
        if mode not in at.AGENT_PERMISSION_MODES:
            raise _bad("permission_mode is not permitted")
        if not mode_within(mode, spec.permission_mode):
            raise _bad("permission_mode exceeds the profile's limit")
        timeout = t.get("timeout_seconds", default_timeout)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout
                <= min(profile.max_timeout_seconds, at.MAX_TIMEOUT_SECONDS)):
            raise _bad("timeout_seconds is outside the permitted range")
        deps = t.get("depends_on", [])
        if (not isinstance(deps, list) or len(deps) > MAX_TASKS or any(not isinstance(d, str) for d in deps)
                or len(set(deps)) != len(deps)):
            raise _bad("depends_on must be a list of distinct task ids")
        step = t.get("approval_step")
        if step is None and profile_policy(self.runtime).required_for(mode, tools):
            # Refuse (never auto-add): the operator must name what the human is asked to approve.
            raise _bad("operator policy requires an approval_step for this run")
        if step is not None:
            for key in ("operation", "target"):
                if not isinstance(step.get(key), str) or not _TOKEN.fullmatch(step[key]):
                    raise _bad("approval_step operation and target must be 1-128 characters of [A-Za-z0-9._:/@+-]")
            ttl = step.get("ttl_seconds", ms.DEFAULT_TTL_SECONDS)
            if type(ttl) is not int or not 1 <= ttl <= ms.MAX_TTL_SECONDS:
                raise _bad("approval_step ttl_seconds is outside the permitted range")
            step = {"operation": step["operation"], "target": step["target"], "ttl_seconds": ttl}
        out = {"id": node, "prompt": prompt, "allowed_tools": sorted(set(tools)), "permission_mode": mode,
               "timeout_seconds": timeout, "approval_step": step, "depends_on": sorted(deps)}
        if t.get("profile") is not None:
            out["profile"] = profile.id  # only when named, so requests without it keep their fingerprint
        return out

    @staticmethod
    def _check_graph(tasks: list[dict]) -> None:
        ids = [t["id"] for t in tasks]
        if len(set(ids)) != len(ids):
            raise _bad("task ids must be unique")
        graph = {t["id"]: t["depends_on"] for t in tasks}
        for node, deps in graph.items():
            if node in deps or not set(deps) <= set(ids):
                raise _bad("depends_on must name other tasks of this run")
        done: set[str] = set()
        while len(done) < len(graph):  # Kahn: a pass that adds nothing means a cycle
            ready = [n for n, d in graph.items() if n not in done and set(d) <= done]
            if not ready:
                raise _bad("task dependencies must be acyclic")
            done.update(ready)

    @staticmethod
    def _fingerprint(req: dict) -> dict:
        """What idempotency compares: the request minus prompt text, plus each prompt's digest."""
        def one(t):
            return {**{k: v for k, v in t.items() if k != "prompt"},
                    "prompt_sha256": hashlib.sha256(t["prompt"].encode()).hexdigest()}
        extra = {"profile": req["profile"]} if req.get("profile") else {}
        if req["legacy"]:  # unchanged shape, so earlier receipts still replay
            t = one(req["tasks"][0])
            return {"repo": req["repo"], "base_ref": req["base_ref"], **{k: t[k] for k in (
                "allowed_tools", "permission_mode", "timeout_seconds", "approval_step", "prompt_sha256")}, **extra}
        return {"repo": req["repo"], "base_ref": req["base_ref"], "tasks": [one(t) for t in req["tasks"]], **extra}

    def _receipt(self, key: str, request_hash: str) -> dict | None:
        row = self.db.execute("SELECT request_sha256,response_json FROM idempotency_keys WHERE principal=? AND operation=? AND key=?",
                              (PRINCIPAL, OPERATION, key)).fetchone()
        if row is None:
            return None
        if row[0] != request_hash:
            raise IdempotencyConflict("Idempotency key reused for a different request")
        return json.loads(row[1])

    # ----------------------------------------------------------------- start
    async def start(self, key: str, request: dict) -> CommandResult:
        """Validate everything, then create run + worktree and start the agent. Database work stays
        on the event-loop thread; every git call runs in a worker thread (a hung git must never block
        approvals, cancel or the expiry sweep). Starts are serialized by a lock."""
        async with self._start_lock:
            return await self._start(key, request)

    async def _start(self, key: str, request: dict) -> CommandResult:
        self._require_configured()
        req = self.validate(request)
        request_hash = canonical_request_hash(self._fingerprint(req))
        receipt = self._receipt(key, request_hash)
        if receipt is not None:
            return CommandResult(receipt, replayed=True)
        # Side-effect-free git pre-flight: allowlist, ref resolves, no dangerous repo config.
        try:
            await asyncio.to_thread(self.manager.check_base, req["repo"], req["base_ref"])
        except ValueError as exc:
            raise _bad("repo is not on the allowlist" if "allowlist" in str(exc) else "base_ref does not resolve to a commit") from None
        except WorktreeError:
            raise _bad("repository cannot be used for an agent run") from None
        if self.runtime._busy() or self.db.execute(
                "SELECT 1 FROM mock_run_config c JOIN runs r ON r.id=c.run_id WHERE r.state='running' LIMIT 1").fetchone():
            raise AgentRunError(409, "Another run is active")

        run_id, task_ids = self._queue_run(key, req)
        created: list[str] = []
        paths: dict[str, str] = {}
        try:
            specs = {}
            for task in req["tasks"]:
                node = task["id"]
                try:
                    paths[node] = await asyncio.to_thread(self.manager.create, run_id, task_ids[node], req["repo"], req["base_ref"])
                except (ValueError, WorktreeError):
                    raise _bad("worktree could not be created") from None
                created.append(task_ids[node])
                try:
                    recorded = await asyncio.to_thread(worktree_checker(self.manager), run_id, task_ids[node], paths[node])
                except (ValueError, WorktreeError):
                    raise _bad("worktree could not be created") from None
                if not recorded[1]:
                    raise _bad("worktree could not be created")
                # Identity recorded for the full-spec approval binding and the spawn-time recheck.
                spec = {"prompt": task["prompt"], "worktree": paths[node], "allowed_tools": task["allowed_tools"],
                        "permission_mode": task["permission_mode"], "timeout_seconds": task["timeout_seconds"],
                        "repo": req["repo"], "base_ref": req["base_ref"], "worktree_head": recorded[0]}
                named = task["profile"] if "profile" in task else req.get("profile")
                if named and named != DEFAULT_PROFILE_ID:
                    spec["profile_id"] = named
                if task["approval_step"] is not None:
                    spec["approval_step"] = {**task["approval_step"], "policy_version": POLICY_VERSION}
                specs[node] = spec
            try:
                self.runtime.start(run_id, "agent-start:" + run_id, specs)
            except AgentDisabled:
                raise AgentRunError(503, "Agent runs are not configured") from None
            except at.AgentSpecError:
                raise _bad("agent task specification was rejected") from None
            except (StateConflict, IdempotencyConflict):
                raise AgentRunError(409, "Agent run cannot be started") from None
        except BaseException:
            await self._cleanup(run_id, created)
            raise
        if req["legacy"]:
            node = req["tasks"][0]["id"]
            response = {"run_id": run_id, "task_id": task_ids[node], "state": "running", "worktree_path": paths[node],
                        "repo": req["repo"], "base_ref": req["base_ref"]}
        else:
            response = {"run_id": run_id, "state": "running", "repo": req["repo"], "base_ref": req["base_ref"],
                        "tasks": [{"id": t["id"], "task_id": task_ids[t["id"]], "worktree_path": paths[t["id"]]} for t in req["tasks"]]}
        return execute_idempotent(self.db, principal=PRINCIPAL, operation=OPERATION, key=key,
                                  request=self._fingerprint(req),
                                  execute=lambda db: response)

    def _queue_run(self, key: str, req: dict) -> tuple[str, dict[str, str]]:
        """Idempotently publish the workflow (single node, or the requested DAG) and queue a fresh run.

        A prior attempt for the same key that failed was cancelled, so each retry
        uses the next derived key; a still-queued run (crash before start) is reused."""
        if req["legacy"]:
            workflow_id, workflow_key = WORKFLOW_ID, WORKFLOW_KEY
            nodes = [{"id": NODE, "dependencies": []}]
        else:
            nodes = [{"id": t["id"], "dependencies": list(t["depends_on"])} for t in sorted(req["tasks"], key=lambda t: t["id"])]
            digest = hashlib.sha256(json.dumps(nodes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
            workflow_id, workflow_key = "synapse.agent.dag." + digest, "agent-run-dag-workflow-v1-" + digest
        publish_workflow(self.db, workflow_id=workflow_id, version="1", nodes=nodes,
                         principal=PRINCIPAL, idempotency_key=workflow_key)
        base = hashlib.sha256(key.encode()).hexdigest()[:40]
        for n in range(MAX_RETRIES):
            made = create_run(self.db, workflow_id=workflow_id, version="1", principal=PRINCIPAL,
                              idempotency_key="agent-run:%s:%d" % (base, n)).response
            state = self.db.execute("SELECT state FROM runs WHERE id=?", (made["run_id"],)).fetchone()[0]
            if state == "queued":
                return made["run_id"], dict(made["tasks"])
            if state == "running":
                # Crash between a successful start and the stored receipt: refuse rather than guess.
                raise AgentRunError(409, "A run for this idempotency key is already in progress")
        raise AgentRunError(409, "Too many failed attempts for this idempotency key; use a new key")

    async def _cleanup(self, run_id: str, created_task_ids: list[str]) -> None:
        """Best effort, idempotent: remove the (unused) worktrees, cancel the queued run."""
        state = self.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if state is None or state[0] != "queued":
            return  # the start committed: the run (and its worktrees) are real, never touch them
        for task_id in created_task_ids:
            try:
                await asyncio.shield(asyncio.to_thread(self.manager.remove, run_id, task_id, force=True))
            except Exception:
                _LOG.error("Could not remove worktree of failed agent run %s", run_id)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT state,state_version FROM runs WHERE id=?", (run_id,)).fetchone()
                if row is not None and row[0] == "queued":
                    for tid, tstate, tver in self.db.execute("SELECT id,state,state_version FROM tasks WHERE run_id=?", (run_id,)).fetchall():
                        if tstate == "created":
                            write_task_transition(self.db, StateChange(tid, State.CREATED, State.CANCELLED, tver), principal=PRINCIPAL)
                    write_run_transition(self.db, RunChange(run_id, RunState.QUEUED, RunState.CANCELLED, row[1]), principal=PRINCIPAL)
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
        except Exception:
            _LOG.error("Could not cancel queued run %s after a failed agent start", run_id)

    # ------------------------------------------------------------ profiles
    def _declared(self, profile_id) -> ProfileSpec:
        """A declared (non-default) registry profile, or a 422; cooldowns apply to nothing else."""
        spec = None if self.registry is None or not isinstance(profile_id, str) else self.registry.get(profile_id)
        if spec is None or profile_id == DEFAULT_PROFILE_ID:
            raise _bad("unknown profile")
        return spec

    def _load(self, profile_id: str) -> int:
        """Worker slots a profile occupies now (same rule as ``claim_task``)."""
        return self.db.execute(
            "SELECT COUNT(*) FROM ("
            " SELECT a.profile_id AS p FROM task_attempts a JOIN tasks t ON t.id=a.task_id JOIN runs r ON r.id=t.run_id "
            " WHERE a.state NOT IN ('completed','failed','cancelled') AND r.state NOT IN ('failed','cancelled','succeeded')"
            " UNION ALL SELECT t.assigned_profile_id FROM tasks t JOIN runs r ON r.id=t.run_id "
            " WHERE t.state IN ('assigned','running') AND r.state NOT IN ('failed','cancelled','succeeded') AND NOT EXISTS ("
            "  SELECT 1 FROM task_attempts a WHERE a.task_id=t.id AND a.state NOT IN ('completed','failed','cancelled'))"
            ") WHERE p=?", (profile_id,)).fetchone()[0]

    def list_profiles(self) -> dict:
        """Operator view of the registry: never credentials, executable paths or the base profile's settings."""
        if self.registry is None:
            specs, chain = [self.resolve_profile(None)], ()
        else:
            specs, chain = list(self.registry.profiles), self.registry.chain
        out = []
        for spec in specs:
            declared = spec.id != DEFAULT_PROFILE_ID
            position = "primary" if chain and spec.id == chain[0] else "fallback" if spec.id in chain[1:] else None
            out.append({"id": spec.id, "provider": spec.provider, "enabled": bool(spec.enabled),
                        "max_concurrency": spec.max_concurrency,
                        "load": self._load(spec.id) if declared else None,  # the default is capped by the run row
                        "cooldown_remaining_seconds": round(self.runtime.cooldowns.remaining(spec.id), 3) if declared else 0.0,
                        "position": position})
        return {"profiles": out}

    def set_cooldown(self, profile_id, seconds) -> dict:
        self._declared(profile_id)
        try:
            Cooldowns().set(profile_id, seconds)  # range check on a scratch object: nothing is in force yet
        except ValueError:
            raise _bad("seconds must be above 0 and at most 86400") from None
        # Cooldowns only extend, so record what will actually be in force as well as what was asked for.
        effective = max(self.runtime.cooldowns.remaining(profile_id), float(seconds))
        self._audit_profile("profile.cooldown_set", profile_id,  # audit first, then the effect
                            {"seconds": seconds, "effective_seconds": round(effective, 3)})
        self.runtime.cooldowns.set(profile_id, seconds)
        return {"id": profile_id, "cooldown_remaining_seconds": round(self.runtime.cooldowns.remaining(profile_id), 3)}

    def clear_cooldown(self, profile_id) -> dict:
        self._declared(profile_id)
        before = self.runtime.cooldowns.remaining(profile_id)
        # Clearing is the permissive direction: the audit must succeed first.
        self._audit_profile("profile.cooldown_cleared", profile_id, {"was_cooling": before > 0})
        self.runtime.cooldowns.clear(profile_id)
        return {"id": profile_id, "cooldown_remaining_seconds": 0.0}

    def _audit_profile(self, event_type: str, profile_id: str, payload: dict) -> None:
        try:
            event = EventEnvelope.create(aggregate_type="profile", aggregate_id=profile_id, event_type=event_type,
                                         payload={"profile_id": profile_id, "principal": PRINCIPAL, **payload})
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
                                "VALUES(?,?,?,NULL,?,?,?,?)",
                                (event.id, event.aggregate_type, profile_id, event.event_type, event.schema_version,
                                 encode_payload(event.payload), event.occurred_at))
                self.db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
        except Exception:
            _LOG.error("Profile change %s for %s was refused: the audit event could not be written", event_type, profile_id)
            raise AgentRunError(503, "Profile change could not be recorded") from None

    # ------------------------------------------------------- worktree removal
    async def remove_worktree(self, run_id: str, force: bool = False) -> dict:
        """Operator-requested removal of a FINISHED agent run's worktree (never automatic).

        Refused while the run is active or recovering (its process or evidence may still matter) and,
        unless ``force``, while the tree has uncommitted changes. ``force`` discards uncommitted work and
        unmerged commits on the run's branch; that choice is recorded in an audit event.
        Idempotent: an already-removed worktree reports ``removed: false``.
        """
        if self.manager is None:
            raise AgentRunError(503, "Agent runs are not configured (enable the Claude CLI config with repos and worktree_root)")
        row = self.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None or not at.is_agent_run(self.db, run_id):
            raise RunNotFound("Agent run not found")
        if row[0] not in _FINISHED_RUN_STATES:
            raise AgentRunError(409, "The run has not finished; cancel it or wait before removing its worktree")
        manager = self.manager
        task_ids = [t for (t,) in self.db.execute("SELECT id FROM tasks WHERE run_id=? ORDER BY id", (run_id,)).fetchall()]
        refusal = AgentRunError(409, "A worktree has uncommitted changes or could not be removed; "
                                     "inspect it, or retry with force to discard its work")
        if not force:
            # All-or-nothing for the dirty case: look at EVERY tree before deleting any, so a dirty task
            # never leaves its siblings half removed. A tree that is already gone has nothing to check.
            for task_id in task_ids:
                try:
                    status = await asyncio.to_thread(manager.status, run_id, task_id)
                except WorktreeError:
                    continue
                if not status.clean:
                    raise refusal
        results: list[dict] = []
        failed = False
        try:
            for task_id in task_ids:
                try:
                    gone = bool(await asyncio.to_thread(manager.remove, run_id, task_id, force))
                except WorktreeError:
                    failed = True  # e.g. changed since the check: stop, report what was already removed
                    break
                results.append({"task_id": task_id, "removed": gone})
        finally:
            removed_ids = [r["task_id"] for r in results if r["removed"]]
            if removed_ids:  # audit exactly what was removed, even when a later task could not be
                self._audit_worktree_removed(run_id, force, removed_ids)
        if failed:
            raise refusal
        return {"run_id": run_id, "removed": any(r["removed"] for r in results), "tasks": results}

    async def commit_worktree(self, run_id: str, message: str) -> dict:
        """Operator-requested host-side commit of a FINISHED agent run's edits onto each task's own branch.

        The sandboxed agent cannot write to the repository's git metadata, so this is how its edits become a
        reviewable commit. Never automatic, never touches the owner's checkout or branch, never pushes. Tasks
        with nothing to commit are skipped; if no task has anything, the request is refused (409). Audited with
        the task ids and resulting commit ids (never the message).
        """
        if self.manager is None:
            raise AgentRunError(503, "Agent runs are not configured (enable the Claude CLI config with repos and worktree_root)")
        row = self.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None or not at.is_agent_run(self.db, run_id):
            raise RunNotFound("Agent run not found")
        if row[0] not in _FINISHED_RUN_STATES:
            raise AgentRunError(409, "The run has not finished; cancel it or wait before committing its worktree")
        manager = self.manager
        task_ids = [t for (t,) in self.db.execute("SELECT id FROM tasks WHERE run_id=? ORDER BY id", (run_id,)).fetchall()]
        results: list[dict] = []
        try:
            for task_id in task_ids:
                if await asyncio.to_thread(manager.worktree_repo, run_id, task_id) is None:
                    continue  # worktree already removed
                refusal = AgentRunError(409, "A worktree could not be committed (nested repository, .gitmodules, too many "
                                             "paths, or it was tampered with or changed); inspect it. Tasks listed in the "
                                             "audit event agent.worktree_committed may already be committed")
                try:
                    status = await asyncio.to_thread(manager.status, run_id, task_id)
                    if status.clean:
                        continue
                    out = await asyncio.to_thread(manager.commit, run_id, task_id, message)
                except ValueError:
                    raise AgentRunError(422, "Invalid commit message") from None
                except WorktreeError:
                    raise refusal from None
                results.append({"task_id": task_id, "branch": out["branch"], "head": out["head"], "paths": out["paths"]})
        finally:
            if results:  # audit what was committed even when a later task refused
                self._audit_worktree_event(run_id, "agent.worktree_committed", {
                    "tasks": [{"task_id": r["task_id"], "head": r["head"]} for r in results]})
        if not results:
            raise AgentRunError(409, "Nothing to commit: every worktree is clean or gone")
        return {"run_id": run_id, "tasks": results}

    def _audit_worktree_removed(self, run_id: str, force: bool, task_ids: list[str]) -> None:
        self._audit_worktree_event(run_id, "agent.worktree_removed", {"forced": bool(force), "task_ids": list(task_ids)})

    def _audit_worktree_event(self, run_id: str, event_type: str, extra: dict) -> None:
        try:
            event = EventEnvelope.create(aggregate_type="run", aggregate_id=run_id, event_type=event_type,
                                         payload={"run_id": run_id, "principal": PRINCIPAL, **extra})
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
                                "VALUES(?,?,?,?,?,?,?,?)",
                                (event.id, event.aggregate_type, run_id, run_id, event.event_type, event.schema_version,
                                 encode_payload(event.payload), event.occurred_at))
                self.db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
        except Exception:
            _LOG.error("Worktree event %s of run %s happened but the audit event could not be written", event_type, run_id)

    # ---------------------------------------------------------------- cancel
    def cancel(self, run_id: str, key: str):
        return self.runtime.cancel(run_id, key)

    # ------------------------------------------------------------------ show
    async def show(self, run_id: str) -> dict:
        """Read-only agent view of one run: specs (never the prompt), outcome and worktree state."""
        if self.db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            raise RunNotFound("Run does not exist")
        if not at.is_agent_run(self.db, run_id):
            raise RunNotFound("Run is not an agent run")
        specs = at.load_task_specs(self.db, run_id)
        run_state = self.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        tasks = []
        for task_id, node, state, assigned in self.db.execute(
                "SELECT id,node_id,state,assigned_profile_id FROM tasks WHERE run_id=? ORDER BY id", (run_id,)).fetchall():
            spec = specs[node]
            step = spec["approval_step"]
            attempt = self.db.execute("SELECT id,attempt_no,state,profile_id FROM task_attempts WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1", (task_id,)).fetchone()
            tasks.append({
                "task_id": task_id, "node_id": node, "state": state, "profile_id": assigned,
                "spec": {**({"profile": spec["profile_id"]} if spec.get("profile_id") else {}),
                         "repo": spec["repo"], "base_ref": spec["base_ref"], "worktree_head": spec["worktree_head"],
                         "spec_sha256": None if step is None else step["candidate_sha256"],
                         "permission_mode": spec["permission_mode"], "allowed_tools": spec["allowed_tools"],
                         "timeout_seconds": spec["timeout_seconds"], "prompt_sha256": spec["prompt_sha256"],
                         "prompt_bytes": spec["prompt_bytes"], "worktree_path": spec["worktree"],
                         "approval_step": None if step is None else {k: step[k] for k in ("operation", "target", "ttl_seconds")}},
                "attempt": None if attempt is None else {"id": attempt[0], "attempt_no": attempt[1], "state": attempt[2],
                                                         "profile_id": attempt[3]},
                "result": self._summary(task_id),
                "worktree": await self._worktree(run_id, task_id),
            })
        first = tasks[0] if tasks else None
        summary = first and first["result"]
        wt = first and first["worktree"]
        flat = None
        if wt and wt.get("exists"):  # flattened single-task view for simple clients
            flat = {"status": "clean" if wt["clean"] else "dirty", "head": wt["head"], "diff_stat": wt["diff_stat"],
                    "diff": wt["diff"], "truncated": wt["diff_truncated"]}
        return {"run_id": run_id, "run_state": run_state,
                "outcome": summary.get("outcome") if summary else None,
                "summary_text": summary.get("output_text") if summary else None,
                "tool_names": summary.get("tool_names", []) if summary else [],
                "worktree": flat,
                "cancellation_requested": self.db.execute("SELECT 1 FROM events WHERE run_id=? AND type='agent.cancellation_requested' LIMIT 1", (run_id,)).fetchone() is not None,
                "attempts_resumed": resumed_count(self.db, run_id),  # never-spawned attempts re-queued at startup (ADR 024)
                "tasks": tasks}

    def _summary(self, task_id: str) -> dict | None:
        row = self.db.execute("SELECT sha256 FROM artifacts WHERE json_extract(provenance_json,'$.kind')='agent_summary' "
                              "AND json_extract(provenance_json,'$.task_id')=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (task_id,)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(self.runtime.blobs.read(row[0]))
        except Exception:
            return {"error": "summary artifact is unavailable"}
        text = data.get("output_text") if isinstance(data.get("output_text"), str) else ""
        raw = text.encode("utf-8")
        cut = len(raw) > SHOW_TEXT_BYTES
        tools = [t for t in data.get("tool_names", []) if isinstance(t, str)][:100]
        return {"outcome": data.get("outcome"), "detail": data.get("detail"),
                "output_text": raw[:SHOW_TEXT_BYTES].decode("utf-8", "ignore"),
                "output_truncated": bool(data.get("output_truncated")) or cut,
                "tool_names": tools, "tool_use_count": data.get("tool_use_count"), "usage": data.get("usage")}

    async def _worktree(self, run_id: str, task_id: str) -> dict | None:
        if self.manager is None:
            return None
        manager = self.manager

        def collect():
            return (manager.status(run_id, task_id, max_bytes=SHOW_DIFF_BYTES),
                    manager.diff(run_id, task_id, max_bytes=SHOW_DIFF_BYTES))
        try:
            status, diff = await asyncio.to_thread(collect)
            diff: BoundedText
        except WorktreeError:
            return {"exists": False}
        except Exception:
            return {"exists": None, "error": "worktree status unavailable"}
        return {"exists": True, "clean": status.clean, "head": status.head, "branch": status.branch,
                "changed_paths": status.changed_paths, "diff_stat": status.diff_stat,
                "diff": diff.text, "diff_truncated": diff.truncated or status.truncated}
