#!/usr/bin/env python3
"""One-command local start for real, sandboxed agent runs on this machine.

    python scripts/synapse_up.py up [REPO] [--tools Read,Glob,Grep,Edit] [--no-desktop] [--dry-run]
    python scripts/synapse_up.py workspace FOLDER
    python scripts/synapse_up.py up --workspace FOLDER --repo NAME [--repo NAME ...] [--dry-run] [...]
    python scripts/synapse_up.py down

`workspace FOLDER` lists the git repositories inside a project folder (names and read-only git queries only;
see docs/operations/workspace.md) and how to register them. `up --workspace FOLDER --repo NAME ...` registers
the chosen ones (NAME as shown in that table); unusable repositories and anything outside FOLDER are refused.
`--dry-run` prints which paths would be added and writes nothing.

If ~/.config/synapse/default-account (or $SYNAPSE_DEFAULT_ACCOUNT) holds one line `ID /abs/path/to/claude-config-dir`,
`up` declares that Claude account as profile ID (see docs/operations/agent-profiles.md), starts the daemon with it and
records ID as the default profile that agentctl and the desktop use when none is named. Also private, also not in
this repository.
`up` with no REPO and no --workspace registers your default repositories: the absolute paths, one per line ('#' starts a
comment), in $SYNAPSE_DEFAULT_REPOS or ~/.config/synapse/default-repos. The list is yours and private; it is not
part of this repository.

What `up` does (idempotent; safe to run again for another repo):
  1. creates the private state dir (default ~/.local/state/synapse, mode 0700; SYNAPSE_STATE overrides);
  2. finds the Claude binary (SYNAPSE_CLAUDE_EXE or `claude` on PATH); if it lies under a directory the sandbox
     protects (the native install does: ~/.local/share/claude/...), copies it to ~/.local/lib/synapse/claude
     so `"sandbox": true` can start it (see docs/operations/claude-cli-provider.md);
  3. merges REPO (or the chosen workspace repos) into agent.json (sandbox ON, mode 0600, never turned off here);
  4. starts the daemon with --allow-same-user-agents (agents run as you; the sandbox is the boundary, ADR 019)
     and, unless --no-desktop, the desktop console.
It never touches tokens or approvals: the operator token is created by the daemon and read by agentctl.
`down` stops the daemon and desktop it started.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:
    from synapse.application import workspace as ws
except ImportError:  # run from a checkout without the package installed
    sys.path.insert(0, str(ROOT / "src"))
    from synapse.application import workspace as ws
# Mirrors providers/claude_cli.py::_DENY_HOME_SUBDIRS for the ones an executable realistically lives under.
PROTECTED_HOME_SUBDIRS = (".local/share", ".config", ".claude", ".codex", ".ssh", ".gnupg")
DEFAULT_TOOLS = ("Read", "Glob", "Grep")
_PROFILE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
MAX_REPOS = 32  # the daemon's limit for the `repos` allowlist (docs/operations/agent-runs.md)


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


def read_config(path: Path) -> dict:
    cfg: dict = {}
    if path.exists():
        cfg = json.loads(path.read_text())
        if cfg.get("sandbox") is False:
            raise UpError("%s has sandbox disabled; refusing to start (edit it yourself if you mean that)" % path)
    return cfg


def _entry_path(entry) -> str | None:
    if isinstance(entry, str):
        return os.path.normpath(entry)
    if isinstance(entry, dict) and isinstance(entry.get("path"), str):
        return os.path.normpath(entry["path"])
    return None


def repo_entry(repo: Path) -> dict | str:
    """The registry object for a repository: id = directory name (the manifest and multi-repo runs name repositories
    by it), baseline = the checked-out branch plus main/master/develop when they exist (edit agent.json for more)."""
    def branches() -> list[str]:
        out = subprocess.run(["git", "-C", str(repo), "for-each-ref", "--format=%(refname)", "refs/heads"],
                             capture_output=True, text=True, timeout=20, check=False)
        return out.stdout.split() if out.returncode == 0 else []
    head = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "-q", "HEAD"], capture_output=True, text=True,
                          timeout=20, check=False).stdout.strip()
    existing = branches()
    refs = [r for r in dict.fromkeys([head, "refs/heads/main", "refs/heads/master", "refs/heads/develop"]) if r in existing]
    if not refs or not _SAFE_ID.fullmatch(repo.name):
        return str(repo)  # not a git checkout, detached HEAD or an unusable directory name: a plain single-repo entry
    return {"id": repo.name, "path": str(repo), "baseline_refs": refs, "default_baseline_ref": refs[0]}


def merge_repos(existing: list, new: list[Path]) -> tuple[list, list[str]]:
    """(merged allowlist, paths actually added). New repositories become registry objects (see repo_entry); existing
    entries, plain or object, are kept as they are. Refuses a new repo nested in (or containing) another one:
    the daemon refuses nested allowlist entries at start."""
    repos: list = []
    paths: list[str] = []
    for r in existing:  # normalised so "/a/b/" and "/a/b" are one entry
        path = _entry_path(r)
        if path is not None and path not in paths:
            repos.append(r if isinstance(r, dict) else path)
            paths.append(path)
    added: list[str] = []
    for repo in new:
        repo = Path(os.path.realpath(repo))
        s = str(repo)
        if s in paths:
            i = paths.index(s)
            if isinstance(repos[i], str):  # a plain entry registered earlier becomes an object (multi-repo runs need it)
                repos[i] = repo_entry(repo)
            continue
        for other in paths:
            if Path(other) in repo.parents or repo in Path(other).parents:
                raise UpError("%s is nested with already registered %s; the allowlist must not nest" % (s, other))
        repos.append(repo_entry(repo))
        paths.append(s)
        added.append(s)
    if len(repos) > MAX_REPOS:
        raise UpError("agent.json would list %d repos; the daemon accepts at most %d" % (len(repos), MAX_REPOS))
    ids = [r["id"] for r in repos if isinstance(r, dict) and "id" in r]
    if len(set(ids)) != len(ids):
        raise UpError("two registered repositories share the directory name %s; rename one" % next(i for i in ids if ids.count(i) > 1))
    return repos, added


def write_config(path: Path, executable: Path, repo, tools: tuple[str, ...], worktrees: Path) -> dict:
    """Merge REPO (a Path, or a list of Paths) into agent.json. The sandbox is forced on; an existing
    config with it off is refused."""
    cfg = read_config(path)
    repos, _ = merge_repos(cfg.get("repos", []), list(repo) if isinstance(repo, (list, tuple)) else [repo])
    cfg.update({"executable": str(executable), "enabled": True, "sandbox": True, "repos": repos,
                "worktree_root": str(worktrees),
                "allowed_tools": sorted(set(cfg.get("allowed_tools", [])) | set(tools))})
    tmp = path.with_name(path.name + ".tmp")
    if os.path.lexists(tmp):  # leftover from a crash (or a planted symlink): never write through it
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        os.fchmod(fh.fileno(), 0o600)
        json.dump(cfg, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)
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


def _folder(raw: str) -> str:
    if ws.has_control_chars(raw):
        raise UpError("folder must not contain control characters")
    return os.path.realpath(os.path.expanduser(raw))


def _show(value: str) -> str:
    """Names come from the scanned disk: print them escaped so they cannot drive the terminal."""
    return value if value.isprintable() else repr(value)


def scan_table(scan) -> str:
    rows = [("REPO", "BRANCH", "HEAD", "COMMITS", "UNCOMMITTED", "USABLE")]
    for r in scan.repos:
        branch = r.branch or ("(detached)" if r.detached else "-")
        if r.dirty_count is None:
            dirty = "unknown"
        else:
            dirty = ("yes (%d%s)" % (r.dirty_count, "+" if r.dirty_truncated else "")) if r.dirty_count else "no"
        usable = "yes" if r.usable else "NO: " + r.reason
        rows.append((_show(r.rel), _show(branch), r.short_head, r.commit_count_display, dirty, _show(usable)))
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row[:5], widths)) + "  " + row[5] for row in rows]
    out = ["Workspace %s%s" % (_show(scan.root), " (the folder itself is a repository)" if scan.root_is_repo else ""),
           *lines]
    if not scan.repos:
        out.append("(no git repositories found)")
    for s in scan.skipped:
        out.append("skipped %s: %s" % (_show(os.path.relpath(s.path, scan.root)), s.reason))
    if scan.truncated:
        out.append("NOTE: scan stopped at its entry/repository limit; the list above is incomplete.")
    if scan.depth_limited:
        out.append("NOTE: some folders deeper than the depth limit were not looked into.")
    if any(r.dirty_count for r in scan.repos):
        out.append("NOTE: uncommitted changes are NOT visible to agents: a run's worktree is cut from a committed "
                   "ref, and its results land on a worktree branch, never in your working tree.")
    usable = [r.rel for r in scan.repos if r.usable]
    if usable:
        out.append("\nRegister (operator only; restart the daemon afterwards):\n  python scripts/synapse_up.py up "
                   "--workspace %s %s [--dry-run]" % (shlex.quote(scan.root), " ".join("--repo " + shlex.quote(n) for n in usable)))
    else:
        out.append("\nNothing here can be registered.")
    return "\n".join(out)


def workspace(args) -> int:
    try:
        scan = ws.discover_repos(_folder(args.folder))
    except ValueError as exc:
        raise UpError(str(exc)) from exc
    print(scan_table(scan))
    return 0


def default_account_file() -> Path:
    return Path(os.environ.get("SYNAPSE_DEFAULT_ACCOUNT") or Path.home() / ".config" / "synapse" / "default-account")


def default_account() -> tuple[str, Path] | None:
    """(profile id, Claude config dir) from the operator's private file, or None when there is no such file."""
    path = default_account_file()
    try:
        lines = [x.strip() for x in path.read_text(encoding="utf-8").splitlines()]
    except FileNotFoundError:
        return None
    except OSError:
        raise UpError("cannot read %s" % _show(str(path))) from None
    lines = [x for x in lines if x and not x.startswith("#")]
    parts = lines[0].split(None, 1) if len(lines) == 1 else []
    if len(parts) != 2 or not _PROFILE_ID.fullmatch(parts[0]) or parts[0] == "default" or parts[0].startswith("synapse.builtin."):
        raise UpError("%s must hold one line: ID /absolute/path/to/claude-config-dir" % _show(str(path)))
    config_dir = Path(parts[1])
    if not config_dir.is_absolute() or not config_dir.is_dir() or config_dir.is_symlink():
        raise UpError("the Claude config dir of account %s must be an existing absolute directory (not a symlink)" % parts[0])
    if config_dir.stat().st_mode & 0o077:
        raise UpError("%s must be mode 0700 (the daemon refuses anything wider)" % _show(str(config_dir)))
    return parts[0], config_dir


def write_private(path: Path, text: str) -> None:
    """Write a 0600 file next to its final name and rename it over, never following a planted link."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def write_account_profile(state: Path, account: tuple[str, Path], tools: tuple[str, ...]) -> Path:
    """<state>/agent-profiles.json declaring the account as a claude profile, and <state>/default-profile naming it."""
    profile_id, config_dir = account
    profiles = state / "agent-profiles.json"
    write_private(profiles, json.dumps({"profiles": [{
        "id": profile_id, "provider": "claude", "allowed_tools": list(tools), "permission_mode": "acceptEdits",
        "max_concurrency": 2, "enabled": True, "claude_config_dir": str(config_dir)}]}, indent=1) + "\n")
    write_private(state / "default-profile", profile_id + "\n")
    return profiles


def default_repos_file() -> Path:
    return Path(os.environ.get("SYNAPSE_DEFAULT_REPOS") or Path.home() / ".config" / "synapse" / "default-repos")


def default_repos() -> list[Path]:
    """The operator's own default repositories (a private file outside this repository), each checked like a REPO argument."""
    path = default_repos_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise UpError("give REPO or --workspace FOLDER --repo NAME, or list your default repositories (absolute paths, "
                      "one per line) in %s" % _show(str(path))) from None
    repos = [Path(line.strip()) for line in lines if line.strip() and not line.strip().startswith("#")]
    if not repos:
        raise UpError("%s lists no repositories" % _show(str(path)))
    if any(not r.is_absolute() for r in repos):
        raise UpError("%s must hold absolute paths" % _show(str(path)))
    return [check_repo(r) for r in repos]


def chosen_repos(args) -> list[Path]:
    """Validate the requested repos before anything is created or written."""
    if args.workspace is None and not args.repo and not args.repo_names:
        return default_repos()
    if args.workspace is None:
        if not args.repo or args.repo_names:
            raise UpError("give either REPO or --workspace FOLDER --repo NAME [--repo NAME ...]")
        return [check_repo(Path(args.repo))]
    if args.repo or not args.repo_names:
        raise UpError("with --workspace, name the repositories with --repo NAME (see `workspace FOLDER`)")
    try:  # walks names only; git queries run on the chosen repositories alone
        picked = ws.pick_repos(_folder(args.workspace), args.repo_names)
    except ValueError as exc:
        raise UpError(str(exc)) from exc
    for info in picked:
        if info.dirty_count:
            print("note: %s has %d uncommitted change(s); agents only see committed refs"
                  % (_show(info.rel), info.dirty_count))
    return [Path(info.path) for info in picked]


def up(args) -> int:
    home, state = Path.home(), state_dir()
    new = chosen_repos(args)
    account = default_account()
    cfg_path = state / "agent.json"
    _, added = merge_repos(read_config(cfg_path).get("repos", []), new)
    if added:
        print("%s: will add to repos:\n%s" % (cfg_path, "\n".join("  " + _show(a) for a in added)))
    else:
        print("%s: already lists:\n%s" % (cfg_path, "\n".join("  " + _show(str(p)) for p in new)))
    if account:
        print("default account: profile %s runs as the Claude account in %s" % (account[0], _show(str(account[1]))))
    if args.dry_run:
        print("dry run: nothing written, nothing started")
        return 0
    repo = new[0]
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    worktrees = worktree_root(state)
    worktrees.mkdir(exist_ok=True, mode=0o700)
    exe = usable_executable(find_claude(), home)
    tools = tuple(t for t in args.tools.split(",") if t)
    write_config(cfg_path, exe, new, tools, worktrees)
    extra: list[str] = []
    if account:
        extra = ["--agent-config", str(write_account_profile(state, account, tools))]
    else:  # no account any more: a stale default would make clients name a profile the daemon does not have
        (state / "default-profile").unlink(missing_ok=True)
    pidfile = state / "daemon.pid"
    if alive(pidfile) and healthy(state):
        print("daemon already running; agent.json updated (restart it with `down` then `up` to pick up a new repo)")
    else:
        spawn([*runner(), "synapse.daemon", "--state-dir", str(state), "--claude-cli-config", str(cfg_path),
               "--allow-same-user-agents", *extra], state / "daemon.log", pidfile)
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
    print("\nStart a real run:\n  agentctl --state-dir %s agent-start --repo %s --base-ref BASE_REF --tool Read --tool Glob "
          "--tool Grep --idempotency-key KEY --correlation-id KEY --prompt-file - <<< 'your task'\nThen: agent-show RUN, and agent-worktree-commit RUN -m MSG "
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
    p.add_argument("repo", nargs="?")
    p.add_argument("--workspace", metavar="FOLDER", help="register repositories found in this folder")
    p.add_argument("--repo", dest="repo_names", action="append", default=[], metavar="NAME",
                   help="with --workspace: a repository name as shown by `workspace FOLDER` (repeatable)")
    p.add_argument("--tools", default=",".join(DEFAULT_TOOLS), help="comma-separated tools agents may use")
    p.add_argument("--no-desktop", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="print what would be added; write and start nothing")
    w = sub.add_parser("workspace", help="list the git repositories in a project folder (read-only)")
    w.add_argument("folder")
    sub.add_parser("down")
    args = parser.parse_args(argv)
    try:
        return {"up": up, "workspace": workspace, "down": down}[args.cmd](args)
    except UpError as exc:
        print("synapse_up:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
