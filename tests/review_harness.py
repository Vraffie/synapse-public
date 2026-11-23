"""Offline harness for AgentWorkflowEngine review/evidence tests: a scripted fake provider, never a real CLI."""
from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from synapse.domain.runs import RunChange, RunState
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.infrastructure.daemon_database import daemon_database
from synapse.orchestration.agent_engine import AgentProviderRouter, AgentWorkflowEngine
from synapse.persistence import agent_tasks as at
from synapse.persistence.run_store import write_run_transition
from synapse.persistence.workflow_store import create_run, publish_workflow
from synapse.providers.claude_cli import ClaudeCliProfile
from synapse.providers.contracts import ProviderEvent

ROOT = Path(__file__).resolve().parents[1]
READ = ["Glob", "Grep", "Read"]
EDIT = ["Edit", "Glob", "Grep", "Read", "Write"]
SHA_IN_PROMPT = re.compile(r"Candidate SHA-256: ([0-9a-f]{64})")

# A script gets (request_input) and returns (list of output texts, terminal kind).
Script = Callable[[dict], tuple[list[str], str]]


class FakeProvider:
    def __init__(self, scripts: dict[str, Script], calls: list):
        self.scripts, self.calls = scripts, calls
        self._pending: dict[str, tuple[str, dict]] = {}

    async def start(self, request):
        handle = "h-%d" % len(self.calls)
        self.calls.append((request.task_id, dict(request.input)))
        self._pending[handle] = (request.task_id, dict(request.input))
        return handle

    async def stream(self, handle):
        task_id, data = self._pending.pop(handle)
        texts, terminal = self.scripts[task_id](data)
        yield ProviderEvent("started")
        for t in texts:
            yield ProviderEvent("output", t)
        yield ProviderEvent(terminal)

    async def cancel(self, handle):
        return None

    async def usage(self, handle):
        return None


def verdict_from_prompt(verdict: str = "approve", *, extra: str = "", sha: str | None = None) -> Script:
    """A well-behaved reviewer: echo the candidate hash it was given."""
    def script(data):
        given = SHA_IN_PROMPT.search(data["prompt"]).group(1)
        return [extra + "Looked at it.\nSYNAPSE-REVIEW-VERDICT v1 %s %s" % (verdict, sha or given)], "succeeded"
    return script


def say(*texts: str, terminal: str = "succeeded") -> Script:
    return lambda data: (list(texts), terminal)


class Env:
    def __init__(self, db, tmp_path: Path, nodes: list[dict]):
        self.db, self.tmp = db, tmp_path
        blobs = tmp_path / "blobs"
        blobs.mkdir(mode=0o700, exist_ok=True)
        self.blobs = ArtifactBlobStore(blobs, max_bytes=1 << 20)
        now = datetime.now(timezone.utc).isoformat()
        for pid in (at.AGENT_PROFILE, "reviewer-b"):
            db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,enabled,max_concurrency,created_at)"
                       " VALUES(?,'claude','p','builtin:test',1,4,?)", (pid, now))
        publish_workflow(db, workflow_id="wf", version="1", nodes=nodes, principal="operator", idempotency_key="p")
        created = create_run(db, workflow_id="wf", version="1", principal="operator", idempotency_key="c").response
        self.run_id, self.tasks = created["run_id"], created["tasks"]
        db.execute("BEGIN IMMEDIATE")
        write_run_transition(db, RunChange(self.run_id, RunState.QUEUED, RunState.RUNNING, 1), principal="operator")
        db.execute("COMMIT")
        self.calls: list = []

    def spec(self, *, tools=READ, mode="plan", profile_id=None, worktree=None, head=None, timeout=60):
        wt = worktree or str(self.tmp)
        s = {"prompt_artifact_id": "prompt-artifact", "prompt_sha256": hashlib.sha256(b"Do the task.").hexdigest(), "allowed_tools": list(tools), "permission_mode": mode, "worktree": wt, "timeout_seconds": timeout,
             "approval_step": None, "worktree_head": head}
        if profile_id is not None:
            s["profile_id"] = profile_id
        return s

    def engine(self, specs: dict, scripts: dict, **kw) -> AgentWorkflowEngine:
        profile = ClaudeCliProfile(executable="/bin/true", allowed_tools=tuple(EDIT), state_dir=str(self.tmp),
                                   enabled=True)
        calls = self.calls
        router = AgentProviderRouter(profile, specs, lambda p: FakeProvider(scripts, calls),
                                     profile_resolver=lambda pid: profile)
        kw.setdefault("profile_check", lambda pid: None)
        return AgentWorkflowEngine(self.db, router, self.blobs, specs, lambda t: b"Do the task.", routing_dry_run=None,
                                   **kw)

    def run(self, engine) -> str:
        return asyncio.run(engine.run(self.run_id, at.AGENT_PROFILE, {t: ("agent",) for t in self.tasks.values()}))

    def state(self, node: str) -> str:
        return self.db.execute("SELECT state FROM tasks WHERE id=?", (self.tasks[node],)).fetchone()[0]

    def events(self, event_type: str) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT payload_json FROM events WHERE type=? ORDER BY rowid", (event_type,))]

    def audit(self, action: str) -> list[tuple]:
        return self.db.execute("SELECT decision,resource,details_json FROM audit_entries WHERE action=? ORDER BY rowid",
                               (action,)).fetchall()

    def spawned(self) -> list[str]:
        node_of = {v: k for k, v in self.tasks.items()}
        return [node_of[t] for t, _ in self.calls]


@contextmanager
def environment(tmp_path: Path, nodes: list[dict]):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    with daemon_database(state / "db.sqlite", ROOT / "migrations") as db:
        yield Env(db, tmp_path, nodes)


AUTHOR_REVIEW = [{"id": "author", "dependencies": []}, {"id": "review", "dependencies": ["author"]}]
