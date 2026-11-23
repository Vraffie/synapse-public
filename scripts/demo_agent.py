#!/usr/bin/env python3
"""End-to-end demo for REAL agent runs (see docs/operations/agent-runs.md).

Default mode uses a FAKE `claude` executable written into a temp dir: no real binary, no network, no
quota. Everything goes through the public surface (`python -m synapse.cli` against a real daemon
subprocess started with --claude-cli-config).

Opt-in real mode: SYNAPSE_REAL_CLAUDE=1 (optionally SYNAPSE_CLAUDE_EXE=/path/to/claude) runs a trivial
READ-ONLY prompt (tools Read/Glob/Grep) against a throwaway git repo, plus an approval-gated variant
whose human approval uses the approver token from this process's memory (never printed, passed to the
CLI child only through SYNAPSE_APPROVER_TOKEN).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
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
FAKE = '''#!{py}
import json, sys, time
mode = sys.stdin.read()
open("spawns.log", "a").write(mode.split()[0] + "\\n")
def emit(o): sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
emit({{"type": "system", "subtype": "init"}})
if mode.startswith("ok"):
    open("agent-output.txt", "w").write("written by the fake agent\\n")
    emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "fake agent done"}}]}}}})
    emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Edit", "input": {{}}}}]}}}})
    emit({{"type": "result", "subtype": "success", "is_error": False, "usage": {{"input_tokens": 1, "output_tokens": 1}}}})
elif mode.startswith("hang"):
    time.sleep(120)
'''
REAL_PROMPT = "Read the file hello.txt in the current directory and reply with only its first word. Do not modify anything."


class DemoError(AssertionError):
    pass


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("SYNAPSE_APPROVER_TOKEN", None)
    return env


def git(cwd: Path, *args: str) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "d@d", "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "d@d"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


class AgentDemo:
    def __init__(self, base: Path, *, real: bool, quiet: bool = False):
        self.base, self.real, self.quiet = base, real, quiet
        self.state = base / "state"
        self.state.mkdir(mode=0o700)
        self.repo = (base / "repo").resolve()
        self.root = (base / "worktrees").resolve()
        self.proc: subprocess.Popen | None = None
        self.log = base / "daemon.log"
        self.approver = ""
        self.outputs: list[str] = []
        self.report: dict = {"checks": {}, "mode": "real" if real else "fake"}
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "hello.txt").write_text("synapse is a demo\n")
        git(self.repo, "add", "hello.txt")
        git(self.repo, "commit", "-q", "-m", "init")
        if real:
            exe = os.environ.get("SYNAPSE_CLAUDE_EXE") or shutil.which("claude")
            if not exe:
                raise DemoError("SYNAPSE_REAL_CLAUDE=1 but no claude executable found (set SYNAPSE_CLAUDE_EXE)")
            self.exe, self.tools = str(Path(exe).resolve()), ["Read", "Glob", "Grep"]
        else:
            fake = base / "bin" / "claude"
            fake.parent.mkdir()
            fake.write_text(FAKE.format(py=sys.executable))
            fake.chmod(0o755)
            self.exe, self.tools = str(fake), ["Read", "Edit"]
        cfg = base / "agent.json"
        cfg.write_text(json.dumps({"executable": self.exe, "allowed_tools": self.tools, "enabled": True,
                                   "max_timeout_seconds": 600, "repos": [str(self.repo)], "worktree_root": str(self.root)}))
        cfg.chmod(0o600)
        self.cfg = cfg

    def say(self, text: str) -> None:
        if not self.quiet:
            print(text, flush=True)

    def check(self, name: str, ok: bool) -> None:
        self.report["checks"][name] = bool(ok)
        self.say(("PASS  " if ok else "FAIL  ") + name)
        if not ok:
            raise DemoError("check failed: " + name)

    # -- processes
    def start_daemon(self) -> None:
        log = open(self.log, "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "synapse.daemon", "--state-dir", str(self.state), "--migrations", str(MIGRATIONS),
                                      "--claude-cli-config", str(self.cfg),
                                      "--allow-same-user-agents"], stdout=log, stderr=log, env=child_env())
        log.close()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise DemoError("daemon exited early; see " + str(self.log))
            if self.ctl("health", check=False).returncode == 0:
                return
            time.sleep(0.05)
        raise DemoError("daemon did not become ready")

    def stop_daemon(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def ctl(self, *args: str, check: bool = True, approver: str | None = None, stdin: str | None = None):
        env = child_env()
        if approver is not None:
            env["SYNAPSE_APPROVER_TOKEN"] = approver
        done = subprocess.run([sys.executable, "-m", "synapse.cli", "--state-dir", str(self.state), *args], input=stdin,
                              capture_output=True, text=True, timeout=60, env=env)
        self.outputs.extend((done.stdout, done.stderr))
        if check and done.returncode != 0:
            raise DemoError(f"agentctl {args[0]} failed: {done.stderr.strip()}")
        return done

    def ctl_json(self, *args: str, **kw):
        return json.loads(self.ctl(*args, **kw).stdout)

    def wait(self, what: str, predicate, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.2)
        raise DemoError("timed out waiting for " + what)

    def run_state(self, run_id: str) -> str:
        return self.ctl_json("run", run_id)["run"]["state"]

    def start(self, prompt: str, key: str, *extra: str, check: bool = True):
        return self.ctl("agent-start", "--repo", str(self.repo), "--base-ref", "main", "--prompt-file", "-",
                        *[a for t in self.tools for a in ("--tool", t)], "--timeout-seconds", "300" if self.real else "60",
                        "--idempotency-key", key, "--correlation-id", "demo", *extra, stdin=prompt, check=check)

    # -- scenario
    def run_all(self) -> dict:
        self.start_daemon()
        self.approver = self.ctl("approver-init").stdout.strip()
        del self.outputs[-2:]  # the one-time stdout is the token itself
        self.say("daemon up; operator and approver tokens provisioned (values not shown)")
        prompt = REAL_PROMPT if self.real else "ok demo prompt"

        # 1. plain run, replay, show
        first = json.loads(self.start(prompt, "plain-1").stdout)
        again = json.loads(self.start(prompt, "plain-1").stdout)
        self.check("start returns a receipt and an identical replay", first == again and first["state"] == "running")
        run_id = first["run_id"]
        self.wait("agent run to finish", lambda: self.run_state(run_id) in ("succeeded", "failed", "recovering"), 300 if self.real else 40)
        shown = self.ctl_json("agent-show", run_id)
        self.report["plain"] = {"run_state": shown["run_state"], "outcome": shown["outcome"], "summary": (shown["summary_text"] or "")[:200],
                                "tool_names": shown["tool_names"], "worktree": None if not shown["worktree"] else shown["worktree"]["status"]}
        self.check("agent run succeeds with a succeeded outcome", shown["run_state"] == "succeeded" and shown["outcome"] == "succeeded")
        if not self.real:
            self.check("fake agent output is visible as a worktree diff/status", shown["worktree"]["status"] == "dirty"
                       and shown["summary_text"] == "fake agent done" and shown["tool_names"] == ["Edit"])
            self.check("the main checkout was not touched", not (self.repo / "agent-output.txt").exists()
                       and git(self.repo, "status", "--porcelain").strip() == "")
        self.check("the worktree lives under worktree_root", shown["tasks"][0]["spec"]["worktree_path"].startswith(str(self.root) + "/"))

        # 2. approval-gated run: parked, human approval, then it runs
        gated = json.loads(self.start(prompt, "gated-1", "--approval-operation", "agent.run", "--approval-target", "demo/repo").stdout)
        gid = gated["run_id"]
        approval = self.wait("approval request", lambda: next((a for a in self.ctl_json("approvals")["approvals"] if a["run_id"] == gid), None))
        self.check("gated task parks before any process exists", self.ctl_json("agent-show", gid)["tasks"][0]["state"] == "awaiting_approval"
                   and not (Path(gated["worktree_path"]) / "spawns.log").exists())
        refused = self.decide(approval, "approved", (self.state / "operator.token").read_text().strip(), check=False)
        self.check("operator token cannot approve", refused.returncode != 0)
        self.decide(approval, "approved", self.approver)
        self.wait("gated run to finish", lambda: self.run_state(gid) in ("succeeded", "failed", "recovering"), 300 if self.real else 40)
        self.report["gated"] = {"run_state": self.run_state(gid)}
        self.check("approved gated run succeeds", self.run_state(gid) == "succeeded")

        # 2b. multi-task run: two independent tasks and one that depends on both, one worktree each
        tasks = [{"id": t, "prompt": (REAL_PROMPT if self.real else "ok demo " + t), "allowed_tools": list(self.tools),
                  "depends_on": deps} for t, deps in (("a", []), ("b", []), ("c", ["a", "b"]))]
        multi_prompts = [t["prompt"] for t in tasks]
        multi = json.loads(self.ctl("agent-start", "--repo", str(self.repo), "--base-ref", "main", "--tasks-file", "-",
                                    "--timeout-seconds", "300" if self.real else "60", "--idempotency-key", "multi-1",
                                    "--correlation-id", "demo", stdin=json.dumps(tasks)).stdout)
        mid = multi["run_id"]
        self.wait("multi-task run to finish", lambda: self.run_state(mid) in ("succeeded", "failed", "recovering"), 600 if self.real else 60)
        mshown = self.ctl_json("agent-show", mid)
        paths = [t["worktree_path"] for t in multi["tasks"]]
        self.report["multi"] = {"run_state": mshown["run_state"], "tasks": {t["node_id"]: t["state"] for t in mshown["tasks"]}}
        self.check("multi-task run succeeds with every task completed", mshown["run_state"] == "succeeded"
                   and sorted(self.report["multi"]["tasks"].values()) == ["completed"] * 3)
        self.check("each multi-task task has its own worktree under worktree_root",
                   len(set(paths)) == 3 and all(p.startswith(str(self.root) + "/") for p in paths))

        # 3. cancel (fake only: the real agent finishes too quickly to be worth racing)
        if not self.real:
            hung = json.loads(self.start("hang demo", "hang-1").stdout)
            self.wait("hung process", lambda: (Path(hung["worktree_path"]) / "spawns.log").exists())
            self.ctl("agent-cancel", hung["run_id"], "--idempotency-key", "cancel-1", "--correlation-id", "demo")
            self.wait("cancelled", lambda: self.run_state(hung["run_id"]) == "cancelled")
            self.check("cancel stops the agent and the run is cancelled", True)

        # 4. nothing sensitive leaked
        tokens = [(self.state / "operator.token").read_bytes(), self.approver.encode()]
        blobs = ["\n".join(self.outputs).encode(), self.log.read_bytes()]
        blobs += [p.read_bytes() for p in self.state.glob("synapse.sqlite*") if p.is_file()]
        self.check("no token appears in CLI output, daemon log or database",
                   all(t not in b for b in blobs for t in tokens))
        with sqlite3.connect(f"file:{self.state / 'synapse.sqlite'}?mode=ro", uri=True) as db:
            leaked = any(any(x in repr(r) for x in [prompt, *multi_prompts]) for t in [x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                         for r in db.execute(f'SELECT * FROM "{t}"'))
        self.check("the prompt text is not in any database table", not leaked)
        return self.report

    def decide(self, approval: dict, decision: str, token: str, check: bool = True):
        return self.ctl("approval-decide", approval["id"], "--decision", decision, "--expected-version", str(approval["state_version"]),
                        "--operation", approval["operation"], "--target", approval["target"],
                        "--candidate-sha256", approval["candidate_manifest_sha256"], "--policy-version", approval["policy_version"],
                        "--reason", "demo", "--idempotency-key", f"d-{decision}-{approval['id']}", "--correlation-id", "demo",
                        check=check, approver=token)

    def close(self) -> None:
        self.stop_daemon()
        subprocess.run(["pkill", "-9", "-f", self.exe], check=False) if not self.real else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    args = parser.parse_args(argv)
    real = os.environ.get("SYNAPSE_REAL_CLAUDE") == "1"
    with tempfile.TemporaryDirectory(prefix="synapse-agent-demo-") as tmp:
        demo = None
        try:
            demo = AgentDemo(Path(tmp), real=real, quiet=args.json)
            report = demo.run_all()
        except DemoError as exc:
            print("demo failed: " + str(exc), file=sys.stderr)
            return 1
        finally:
            if demo is not None:
                demo.close()
    print(json.dumps(report) if args.json else "\n" + json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
