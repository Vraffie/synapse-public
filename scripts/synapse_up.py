#!/usr/bin/env python3
"""One-command local start for real, sandboxed agent runs on this machine.

    python scripts/synapse_up.py up REPO [--tools Read,Glob,Grep,Edit] [--no-desktop]
    python scripts/synapse_up.py down

What `up` does (idempotent; safe to run again for another repo):
  1. creates the private state dir (default ~/.local/state/synapse, mode 0700; SYNAPSE_STATE overrides);
  2. finds the Claude binary (SYNAPSE_CLAUDE_EXE or `claude` on PATH); if it lies under a directory the sandbox
     protects (the native install does: ~/.local/share/claude/...), copies it to ~/.local/lib/synapse/claude
     so `"sandbox": true` can start it (see docs/operations/claude-cli-provider.md);
  3. merges REPO into agent.json (sandbox ON, mode 0600, never turned off here);
  4. starts the daemon with --allow-same-user-agents (agents run as you; the sandbox is the boundary, ADR 019)
     and, unless --no-desktop, the desktop console.
It never touches tokens or approvals: the operator token is created by the daemon and read by agentctl.
`down` stops the daemon and desktop it started.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Mirrors providers/claude_cli.py::_DENY_HOME_SUBDIRS for the ones an executable realistically lives under.
PROTECTED_HOME_SUBDIRS = (".local/share", ".config", ".claude", ".codex", ".ssh", ".gnupg")
DEFAULT_TOOLS = ("Read", "Glob", "Grep")


class UpError(Exception):
    pass


def state_dir() -> Path:
    return Path(os.environ.get("SYNAPSE_STATE") or Path.home() / ".local" / "state" / "synapse")


def worktree_root(state: Path) -> Path:
    """Agent worktrees live NEXT TO the state dir: the daemon refuses a worktree_root inside it."""
    return state.with_name(state.name + "-worktrees")


def is_protected(path: Path, home: Path) -> bool:
    resolved = path.resolve()
    return any(resolved == home / sub or (home / sub) in resolved.parents for sub in PROTECTED_HOME_SUBDIRS)


def find_claude(env=os.environ) -> Path:
    found = env.get("SYNAPSE_CLAUDE_EXE") or shutil.which("claude")
    if not found:
        raise UpError("no claude binary found: install Claude Code or set SYNAPSE_CLAUDE_EXE")
    exe = Path(found).resolve()
    if not exe.is_file() or not os.access(exe, os.X_OK):
        raise UpError("claude binary is not an executable file: %s" % exe)
    return exe


def usable_executable(exe: Path, home: Path) -> Path:
    """The path to put in agent.json: the binary itself, or a copy outside the sandbox's protected paths."""
    if not is_protected(exe, home):
        return exe
    dest = home / ".local" / "lib" / "synapse" / "claude"
    dest.parent.mkdir(parents=True, exist_ok=True)
    stale = (not dest.is_file() or dest.stat().st_size != exe.stat().st_size
             or dest.stat().st_mtime < exe.stat().st_mtime)
    if stale:
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.copyfile(exe, tmp)
        tmp.chmod(0o755)
        os.replace(tmp, dest)
    return dest


def write_config(path: Path, executable: Path, repo: Path, tools: tuple[str, ...], worktrees: Path) -> dict:
    """Merge REPO into agent.json. The sandbox is forced on; an existing config with it off is refused."""
    cfg: dict = {}
    if path.exists():
        cfg = json.loads(path.read_text())
        if cfg.get("sandbox") is False:
            raise UpError("%s has sandbox disabled; refusing to start (edit it yourself if you mean that)" % path)
    repos = [r for r in cfg.get("repos", []) if isinstance(r, str)]
    if str(repo) not in repos:
        repos.append(str(repo))
    cfg.update({"executable": str(executable), "enabled": True, "sandbox": True, "repos": repos,
                "worktree_root": str(worktrees),
                "allowed_tools": sorted(set(cfg.get("allowed_tools", [])) | set(tools))})
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh, indent=1)
    os.replace(tmp, path)
    return cfg


def check_repo(repo: Path) -> Path:
    repo = repo.resolve()
    if not (repo / ".git").exists():
        raise UpError("%s is not a git repository" % repo)
    return repo


def runner() -> list[str]:
    if os.environ.get("SYNAPSE_RUN"):
        return os.environ["SYNAPSE_RUN"].split()
    return ["uv", "run", "--project", str(ROOT), "--extra", "desktop", "python", "-m"]


def healthy(state: Path) -> bool:
    r = subprocess.run([*runner(), "synapse.cli", "--state-dir", str(state), "health"],
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0 and '"ok"' in r.stdout


def spawn(argv: list[str], log: Path, pidfile: Path) -> int:
    with open(log, "ab") as out:
        proc = subprocess.Popen(argv, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True,
                                cwd=ROOT)
    pidfile.write_text(str(proc.pid))
    return proc.pid


def alive(pidfile: Path) -> bool:
    """True only if the recorded pid is a live Synapse process (guards against pid reuse after a crash)."""
    try:
        pid = int(pidfile.read_text())
        os.kill(pid, 0)
        return b"synapse." in Path("/proc/%d/cmdline" % pid).read_bytes()
    except (OSError, ValueError):
        return False


def up(args) -> int:
    home, state = Path.home(), state_dir()
    repo = check_repo(Path(args.repo))
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    worktrees = worktree_root(state)
    worktrees.mkdir(exist_ok=True, mode=0o700)
    exe = usable_executable(find_claude(), home)
    tools = tuple(t for t in args.tools.split(",") if t)
    cfg_path = state / "agent.json"
    write_config(cfg_path, exe, repo, tools, worktrees)
    pidfile = state / "daemon.pid"
    if alive(pidfile) and healthy(state):
        print("daemon already running; agent.json updated (restart it with `down` then `up` to pick up a new repo)")
    else:
        spawn([*runner(), "synapse.daemon", "--state-dir", str(state), "--claude-cli-config", str(cfg_path),
               "--allow-same-user-agents"], state / "daemon.log", pidfile)
        for _ in range(100):
            if healthy(state):
                break
            if not alive(pidfile):
                raise UpError("daemon exited early; see %s" % (state / "daemon.log"))
            time.sleep(0.3)
        else:
            raise UpError("daemon did not become ready; see %s" % (state / "daemon.log"))
        print("daemon up (sandbox on, agents run as you)")
    if not args.no_desktop and not alive(state / "desktop.pid"):
        spawn([*runner(), "synapse.desktop", "--state-dir", str(state)], state / "desktop.log", state / "desktop.pid")
        print("desktop started")
    print("\nStart a real run:\n  agentctl --state-dir %s agent-start --repo %s --base-ref main --tool Read --tool Glob "
          "--tool Grep --prompt-file - <<< 'your task'\nThen: agent-show RUN, and agent-worktree-commit RUN -m MSG "
          "to keep edits on their own branch." % (state, repo))
    return 0


def down(_args) -> int:
    state = state_dir()
    for name in ("desktop", "daemon"):
        pidfile = state / (name + ".pid")
        if not alive(pidfile):
            continue
        pid = int(pidfile.read_text())
        os.killpg(pid, signal.SIGTERM) if os.getpgid(pid) == pid else os.kill(pid, signal.SIGTERM)
        for _ in range(60):
            if not alive(pidfile):
                break
            time.sleep(0.25)
        else:
            os.kill(pid, signal.SIGKILL)
        print("stopped", name)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("up")
    p.add_argument("repo")
    p.add_argument("--tools", default=",".join(DEFAULT_TOOLS), help="comma-separated tools agents may use")
    p.add_argument("--no-desktop", action="store_true")
    sub.add_parser("down")
    args = parser.parse_args(argv)
    try:
        return up(args) if args.cmd == "up" else down(args)
    except UpError as exc:
        print("synapse_up:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
