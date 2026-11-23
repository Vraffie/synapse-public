#!/usr/bin/env python3
"""End-to-end demo and smoke harness for the mock path (see docs/operations/demo.md).

Orchestrates the public CLIs (`python -m synapse.cli`) against a real daemon
subprocess with a temporary private state directory. The one thing no public
interface can do is start a *gated* mock run (approval_steps are runtime-level
only), so the daemon is launched through this script's `_daemon` subcommand,
which patches `MockRuntime.start` IN THAT PROCESS ONLY to attach approval steps
to workflows named `demo-gated` / `demo-expiry`. The shipped daemon is untouched.

Token values are never printed; the approver token travels to the CLI through
the SYNAPSE_APPROVER_TOKEN environment variable of a child process only.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MIGRATIONS = ROOT / "migrations"
DIGEST = "c" * 64
BINDING = {"operation": "demo.publish", "target": "demo/repo", "candidate_sha256": DIGEST, "policy_version": "1"}
NODES = [{"id": "build", "dependencies": []}, {"id": "review", "dependencies": ["build"]}]
# workflow id -> approval ttl in seconds (None: not gated, plain fixed fixture)
WORKFLOWS = {"demo-plain": None, "demo-gated": 600, "demo-expiry": 1}
# The multi-step pipeline used by scripts/demo.py (the Control Center demo): plan -> edit-config + update-docs
# (parallel) -> review (first attempt asks for changes, the retry passes) -> deploy-verify (human approval gate).
PIPELINE_ID = "demo-pipeline"
PIPELINE_NODES = [{"id": "plan", "dependencies": []},
                  {"id": "edit-config", "dependencies": ["plan"]}, {"id": "update-docs", "dependencies": ["plan"]},
                  {"id": "review", "dependencies": ["edit-config", "update-docs"]},
                  {"id": "deploy-verify", "dependencies": ["review"]}]
PIPELINE_SCENARIOS = {"edit-config": ["slow"], "update-docs": ["slow"], "review": ["reject", "succeed"]}
PIPELINE_GATE = {"deploy-verify": {"operation": "demo.deploy", "target": "demo/staging",
                                   "candidate_sha256": DIGEST, "policy_version": "1", "ttl_seconds": 3600}}
SWEEP_INTERVAL = 0.5
PARK_POLL = 0.2


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("SYNAPSE_APPROVER_TOKEN", None)
    return env


# ---------------------------------------------------------------- daemon child

def _daemon_child(state_dir: Path, pace: float = 0.0) -> None:
    """Run the real daemon with demo-only gating attached to the runtime start.

    pace > 0 (demo.py only) makes every mock task stay 'running' that many seconds so a human watching
    the Control Center can see tasks run in parallel; the synthetic provider itself finishes instantly.
    """
    import asyncio

    from synapse.daemon import __main__ as daemon
    from synapse.daemon.mock_runtime import MockRuntime
    from synapse.providers.mock import MockProvider

    if pace > 0:
        original_stream = MockProvider.stream

        async def paced(self, handle):
            async for event in original_stream(self, handle):
                yield event
                if event.kind == "started":
                    await asyncio.sleep(pace)

        MockProvider.stream = paced

        from synapse.orchestration.mock_engine import MockWorkflowEngine
        original_execute = MockWorkflowEngine._execute

        async def execute(self, run_id, task_id, *a, **k):
            result = await original_execute(self, run_id, task_id, *a, **k)
            row = self.db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row and row[0] == "changes_required":
                # Hold 'changes required' long enough to be seen. The worker stays active meanwhile, so the engine
                # loop keeps polling, and _retryable refuses to re-ready the task until the hold is over.
                self._demo_hold = True
                try:
                    await asyncio.sleep(pace)
                finally:
                    self._demo_hold = False
            return result

        original_retryable = MockWorkflowEngine._retryable
        MockWorkflowEngine._execute = execute
        MockWorkflowEngine._retryable = lambda self, count: not getattr(self, "_demo_hold", False) and original_retryable(self, count)

    original_start, original_init, original_sweeps = MockRuntime.start, MockRuntime.__init__, daemon._with_expiry_sweeps

    def init(self, *a, **k):
        original_init(self, *a, **k)
        self._park_poll = PARK_POLL

    def start(self, run_id, key, scenarios=None, approval_steps=None):
        row = self.db.execute("SELECT workflow_id FROM runs WHERE id=?", (run_id,)).fetchone()
        ttl = WORKFLOWS.get(row[0]) if row else None
        if row and row[0] == "demo-expiry":
            self._park_poll = 3.0  # keep the engine's re-drive out of the way so the SWEEP fails the task
        if row and row[0] == PIPELINE_ID and approval_steps is None and scenarios is None:
            scenarios, approval_steps = PIPELINE_SCENARIOS, PIPELINE_GATE
        if ttl is not None and approval_steps is None:
            approval_steps = {"review": {**BINDING, "ttl_seconds": ttl}}
        return original_start(self, run_id, key, scenarios, approval_steps)

    def sweeps(connection, serve, interval=SWEEP_INTERVAL, startup=None):
        return original_sweeps(connection, serve, SWEEP_INTERVAL, startup)

    MockRuntime.__init__, MockRuntime.start, daemon._with_expiry_sweeps = init, start, sweeps
    daemon.run(state_dir, MIGRATIONS)


# --------------------------------------------------------------------- harness

class DemoError(AssertionError):
    pass


class Demo:
    def __init__(self, state_dir: Path, *, quiet: bool = False, pace: float = 0.0):
        self.state = state_dir
        self.pace = pace
        self.quiet = quiet
        self.proc: subprocess.Popen | None = None
        self.log_path = state_dir.parent / (state_dir.name + "-daemon.log")
        self.approver = ""
        self.seq = 0
        self.report: dict = {"checks": {}, "daemon_starts": 0}
        self.outputs: list[str] = []  # every agentctl stdout/stderr, for leak scans

    def say(self, text: str) -> None:
        if not self.quiet:
            print(text, flush=True)

    # -- processes
    def start_daemon(self) -> None:
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_daemon", str(self.state), str(self.pace)],
                                     stdout=log, stderr=log, env=child_env())
        log.close()
        self.report["daemon_starts"] += 1
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise DemoError("daemon exited early; see " + str(self.log_path))
            if self.ctl("health", check=False).returncode == 0:
                return
            time.sleep(0.05)
        raise DemoError("daemon did not become ready")

    def stop_daemon(self, sig: int = signal.SIGTERM) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None

    def ctl(self, *args: str, check: bool = True, approver: str | None = None) -> subprocess.CompletedProcess:
        env = child_env()
        if approver is not None:
            env["SYNAPSE_APPROVER_TOKEN"] = approver
        done = subprocess.run([sys.executable, "-m", "synapse.cli", "--state-dir", str(self.state), *args],
                              capture_output=True, text=True, timeout=30, env=env)
        self.outputs.extend((done.stdout, done.stderr))
        if check and done.returncode != 0:
            raise DemoError(f"agentctl {args[0]} failed: {done.stderr.strip()}")
        return done

    def ctl_json(self, *args: str):
        return json.loads(self.ctl(*args).stdout)

    # -- steps
    def provision(self) -> None:
        # The first daemon start created operator.token; approver-init prints the new token once.
        done = self.ctl("approver-init")
        self.approver = done.stdout.strip()
        del self.outputs[-2:]  # approver-init's one-time stdout is the token itself; never retain it
        self.say("provisioned operator and approver tokens (values not shown)")

    def publish(self, workflow_id: str, nodes: list | None = None) -> None:
        path = self.state.parent / f"{self.state.name}-{workflow_id}.json"
        path.write_text(json.dumps({"workflow_id": workflow_id, "version": "1", "nodes": nodes or NODES}))
        self.ctl("publish-workflow", str(path), "--idempotency-key", "pub-" + workflow_id, "--correlation-id", "demo")
        path.unlink()

    def queue(self, workflow_id: str) -> str:
        out = self.ctl_json("queue-run", workflow_id, "1", "--idempotency-key", "q-" + workflow_id, "--correlation-id", "demo")
        return out["run_id"]

    def mock_start(self, run_id: str) -> dict:
        return self.ctl_json("mock-start", run_id, "--idempotency-key", "s-" + run_id, "--correlation-id", "demo")

    def run_state(self, run_id: str) -> str:
        return self.ctl_json("run", run_id)["run"]["state"]

    def task_states(self, run_id: str) -> dict[str, str]:
        data = self.ctl_json("tasks", run_id)
        rows = data["tasks"] if isinstance(data, dict) else data
        return {t.get("node_id", t.get("id")): t["state"] for t in rows}

    def wait(self, what: str, predicate, timeout: float = 12.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        raise DemoError("timed out waiting for " + what)

    def approval_for(self, run_id: str) -> dict:
        def find():
            rows = self.ctl_json("approvals")
            rows = rows["approvals"] if isinstance(rows, dict) else rows
            return next((a for a in rows if a["run_id"] == run_id), None)
        return self.wait("approval request for " + run_id, find)

    def decide(self, approval: dict, decision: str, token: str, check: bool = True):
        return self.ctl("approval-decide", approval["id"], "--decision", decision,
                        "--expected-version", str(approval["state_version"]),
                        "--operation", approval["operation"], "--target", approval["target"],
                        "--candidate-sha256", approval["candidate_manifest_sha256"],
                        "--policy-version", approval["policy_version"], "--reason", "demo",
                        "--idempotency-key", f"d-{decision}-{approval['id']}", "--correlation-id", "demo",
                        check=check, approver=token)

    def db(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.state / 'synapse.sqlite'}?mode=ro", uri=True, timeout=5)

    # -- scenario
    def run_all(self, *, restart: bool = True, restore_check: bool = True) -> dict:
        self.start_daemon()
        self.provision()
        for wf in WORKFLOWS:
            self.publish(wf)
        runs: dict[str, str] = {}

        # 1. plain mock run through the public CLI only
        runs["plain"] = self.queue("demo-plain")
        self.mock_start(runs["plain"])
        self.wait("plain run to finish", lambda: self.run_state(runs["plain"]) in ("succeeded", "failed"))
        self.check("plain run succeeds", self.run_state(runs["plain"]) == "succeeded")

        # 2. gated run: park, kill the daemon, restart, separation check, human approval
        runs["gated"] = self.queue("demo-gated")
        self.mock_start(runs["gated"])
        approval = self.approval_for(runs["gated"])
        self.wait("review to park", lambda: self.task_states(runs["gated"]).get("review") == "awaiting_approval")
        if restart:
            self.stop_daemon(signal.SIGKILL)
            self.say("daemon killed (SIGKILL) while parked on approval; restarting")
            self.start_daemon()
            self.wait("run re-driven as running", lambda: self.run_state(runs["gated"]) == "running")
            self.check("parked run survives a hard kill and keeps its task parked",
                       self.task_states(runs["gated"]).get("review") == "awaiting_approval")
        operator = (self.state / "operator.token").read_text().strip()
        refused = self.decide(approval, "approved", operator, check=False)
        self.check("operator token cannot decide", refused.returncode != 0
                   and self.task_states(runs["gated"]).get("review") == "awaiting_approval")
        self.decide(approval, "approved", self.approver)
        self.wait("gated run to finish", lambda: self.run_state(runs["gated"]) in ("succeeded", "failed"))
        self.check("approved gated run succeeds", self.run_state(runs["gated"]) == "succeeded")

        # 3. expiry: undecided approval with a 1s ttl expires, the gated task fails
        runs["expiry"] = self.queue("demo-expiry")
        self.mock_start(runs["expiry"])
        approval_x = self.wait("approval to be expired by the sweep", lambda: next(
            (a for a in [self.approval_for(runs["expiry"])] if a["state"] == "expired"), None))
        self.wait("sweep to fail the gated task", lambda: self.task_states(runs["expiry"]).get("review") == "failed")
        self.check("undecided approval expires and the sweep fails the gated task", approval_x["state"] == "expired")
        self.wait("run to be closed out by the park re-drive", lambda: self.run_state(runs["expiry"]) == "failed", 20)
        self.check("expired gate fails the run", self.run_state(runs["expiry"]) == "failed")
        with self.db() as db:
            audited = db.execute("SELECT COUNT(*) FROM audit_entries WHERE action='approval.expire'").fetchone()[0]
        self.check("expiry is audited", audited == 1)

        # 4. backup of the live database verifies
        if restore_check:
            backup = self.state.parent / (self.state.name + "-backup.sqlite")
            self.ctl("backup", str(backup))
            self.ctl("backup-verify", str(backup))
            self.check("live backup verifies", True)
        self.report["runs"] = runs
        self.report["timeline"] = self.timeline(runs)
        return self.report

    def check(self, name: str, ok: bool) -> None:
        self.report["checks"][name] = bool(ok)
        self.say(("PASS  " if ok else "FAIL  ") + name)
        if not ok:
            raise DemoError("check failed: " + name)

    _SHOWN = ("run.state_changed", "approval.requested", "approval.state_changed", "task.approval_gated",
              "task.approval_resolved", "mock.approval_steps_configured")

    def timeline(self, runs: dict[str, str]) -> list[str]:
        """Compact, time-ordered view from the event stream and the audit records."""
        names = {v: k for k, v in runs.items()}
        lines: list[tuple[str, str]] = []
        with self.db() as db:
            for seq, run, kind, payload, at in db.execute(
                    "SELECT sequence,run_id,type,payload_json,occurred_at FROM events WHERE run_id IS NOT NULL ORDER BY sequence"):
                if kind not in self._SHOWN:
                    continue
                body = json.loads(payload)
                change = f" {body['before']} -> {body['after']}" if "before" in body and "after" in body else ""
                lines.append((at, f"event #{seq:<4} {names.get(run, '?'):<7} {kind}{change}"))
            for seq, who, action, decision, at in db.execute(
                    "SELECT sequence,principal,action,decision,created_at FROM audit_entries "
                    "WHERE action LIKE 'approval.%' ORDER BY sequence"):
                lines.append((at, f"audit #{seq:<4} {who} {action} ({decision})"))
        return [f"{t[11:23]}  {text}" for t, text in sorted(lines, key=lambda x: x[0])]

    def close(self) -> None:
        self.stop_daemon()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "_daemon":
        _daemon_child(Path(argv[1]), float(argv[2]) if len(argv) > 2 else 0.0)
        return 0
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, help="existing private dir (default: a fresh temp dir)")
    parser.add_argument("--no-restart", action="store_true", help="skip the mid-run daemon kill/restart")
    parser.add_argument("--json", action="store_true", help="print a JSON report instead of a human timeline")
    args = parser.parse_args(argv)
    tmp = None
    if args.state_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="synapse-demo-")
        base = Path(tmp.name)
        state = base / "state"
        state.mkdir(mode=0o700)
    else:
        state = args.state_dir
    demo = Demo(state, quiet=args.json)
    try:
        report = demo.run_all(restart=not args.no_restart)
    except DemoError as exc:
        print("demo failed: " + str(exc), file=sys.stderr)
        return 1
    finally:
        demo.close()
        if tmp is not None:
            tmp.cleanup()
    if args.json:
        print(json.dumps(report))
    else:
        print("\ntimeline")
        print("\n".join(report["timeline"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
