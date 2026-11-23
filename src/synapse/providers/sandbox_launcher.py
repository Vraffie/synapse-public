"""Opt-in bubblewrap (bwrap) launcher for the Claude CLI child (ADR 019, option C2, first slice).

A same-uid mitigation, NOT a boundary equal to a separate uid: the child runs as the
daemon's user inside new mount/PID/IPC/UTS namespaces. The daemon state dir and the real
HOME are not mounted, so they are invisible; the network namespace is deliberately shared
(owner decision), so a network-capable agent can exfiltrate whatever it can read.

Fail closed: if bwrap or unprivileged user namespaces are unavailable the caller must
refuse the run. This module never builds an unsandboxed command line.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence

# Fixed directories only: never resolve the launcher through an agent- or user-writable PATH.
_BWRAP_CANDIDATES = ("/usr/bin/bwrap", "/usr/local/bin/bwrap", "/bin/bwrap")
_PROBE_TIMEOUT = 15.0


class SandboxUnavailable(RuntimeError):
    """The requested sandbox cannot be provided; the run must not start."""


def find_bwrap() -> str | None:
    for path in _BWRAP_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def check_userns(bwrap: str) -> None:
    """Run a trivial sandbox with the same namespace flags; raise SandboxUnavailable on any failure."""
    try:
        done = subprocess.run(
            [bwrap, "--ro-bind", "/", "/", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
             "--die-with-parent", "--", "/bin/true" if os.path.exists("/bin/true") else "/usr/bin/true"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=_PROBE_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxUnavailable("sandbox requested but bwrap could not be run") from exc
    if done.returncode != 0:
        raise SandboxUnavailable(
            "sandbox requested but unprivileged user namespaces are unavailable: "
            + done.stderr.decode("utf-8", "replace").strip()[:200])


def require_sandbox(bwrap: str | None) -> str:
    """Return a usable bwrap path or raise SandboxUnavailable (no fallback)."""
    path = bwrap or find_bwrap()
    if path is None or not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise SandboxUnavailable("sandbox requested but bwrap is not installed")
    check_userns(path)
    return path


def sandbox_homes(env_home: str | None) -> list[str]:
    """Real home dir candidates: $HOME (if set) and the passwd home; fail closed if neither is known.

    Both are returned (realpath'd, deduplicated) because $HOME is attacker-influenceable config
    and may differ from the account's real home.
    """
    homes: list[str] = []
    candidates = [env_home or ""]
    try:
        import pwd
        candidates.append(pwd.getpwuid(os.getuid()).pw_dir or "")
    except (ImportError, KeyError, OSError):
        pass
    for cand in candidates:
        if cand and os.path.isabs(cand) and "\x00" not in cand:
            real = os.path.realpath(cand)
            if real not in homes:
                homes.append(real)
    if not homes:
        raise SandboxUnavailable("sandbox requested but the home directory cannot be determined")
    return homes


def forbidden_home_paths(homes: Sequence[str], subdirs: Sequence[str]) -> list[str]:
    """Every home-relative deny entry, both lexical and symlink-resolved, for every home."""
    out: list[str] = []
    for home in homes:
        for sub in subdirs:
            lexical = os.path.join(home, sub)
            for path in (lexical, os.path.realpath(lexical)):
                if path not in out:
                    out.append(path)
    return out


def git_readonly_dirs(worktree: str, allowed_repos: Sequence[str] = ()) -> list[str]:
    """Common git dir of a linked worktree, derived from the OPERATOR-controlled repo allowlist.

    The worktree's `.git` file lives inside the writable bind mount, so an agent can rewrite it.
    Its content is therefore only a claim to be checked, never a source of mount paths: the
    parsed gitdir must lie inside `<allowlisted repo>/.git/worktrees/`, and the path returned is
    built from the allowlisted repo, not from the file. Anything else raises SandboxUnavailable.
    A real `.git` directory (self-contained clone) needs no extra mount.
    """
    dotgit = os.path.join(worktree, ".git")
    if os.path.islink(dotgit):
        raise SandboxUnavailable("worktree .git must not be a symlink")
    if os.path.isdir(dotgit):
        return []
    if not os.path.isfile(dotgit):
        raise SandboxUnavailable("worktree has no usable .git entry")
    try:
        with open(dotgit, "rb") as fh:
            line = fh.read(4096).decode("utf-8", "replace").strip()
    except OSError as exc:
        raise SandboxUnavailable("worktree .git file is unreadable") from exc
    if not line.startswith("gitdir: ") or "\x00" in line:
        raise SandboxUnavailable("worktree .git file is malformed")
    gitdir = os.path.realpath(line[len("gitdir: "):])
    for repo in allowed_repos:
        common = os.path.join(repo, ".git")
        if os.path.islink(common) or not os.path.isdir(common):
            continue
        real_common = os.path.realpath(common)
        if os.path.dirname(gitdir) == os.path.join(real_common, "worktrees"):
            return [common]
    raise SandboxUnavailable("worktree .git does not point into an allowlisted repository")


_GIT_SECRET_FILES = ("config", "config.worktree", "credentials")


def git_secret_masks(worktree: str, extra_ro_dirs: Sequence[str] = ()) -> list[str]:
    """Existing git files that may hold secrets (remote URLs with embedded tokens, credential
    stores) inside the worktree's `.git` and the mounted common git dirs, plus per-worktree
    `config.worktree` files. They are masked with an empty read-only bind so `git log`/`diff`
    keep working but the agent cannot read (or replace) them. Symlinks are skipped (the launcher
    refuses a symlinked `.git`; a symlinked entry inside is not followed by the bind anyway)."""
    roots = [os.path.join(worktree, ".git"), *extra_ro_dirs]
    found: list[str] = []
    for root in roots:
        if os.path.islink(root) or not os.path.isdir(root):
            continue
        cands = [os.path.join(root, n) for n in _GIT_SECRET_FILES]
        wts = os.path.join(root, "worktrees")
        if os.path.isdir(wts) and not os.path.islink(wts):
            try:
                cands += [os.path.join(wts, n, "config.worktree") for n in sorted(os.listdir(wts))]
            except OSError:
                pass
        for c in cands:
            if os.path.isfile(c) and not os.path.islink(c) and c not in found:
                found.append(c)
    return found


def _root_links(root: str = "/") -> list[str]:
    """Merged-/usr symlinks (or real dirs) for /bin /sbin /lib /lib64, mirrored from the host."""
    args: list[str] = []
    for name in ("bin", "sbin", "lib", "lib64"):
        host = os.path.join(root, name)
        if os.path.islink(host):
            args += ["--symlink", os.readlink(host), "/" + name]
        elif os.path.isdir(host):
            args += ["--ro-bind", host, "/" + name]
    return args


def build_bwrap_argv(bwrap: str, *, worktree: str, scratch: str, executable: str,
                     inner: Sequence[str], forbidden: Sequence[str] = (),
                     extra_ro_dirs: Sequence[str] = (), host_root: str = "/",
                     no_ancestor: Sequence[str] = ()) -> list[str]:
    """Wrap `inner` (argv[0] is `executable`) in bwrap. Pure: no filesystem writes, no env.

    Writable: only `worktree` and `scratch` (both at their host paths, so the worktree's
    absolute `.git` pointers and the Synapse settings paths stay valid). Read-only: /usr,
    /etc, the executable file and `extra_ro_dirs` (the linked worktree's common git dir).
    Everything else, including the state dir and the real HOME, is simply not mounted.
    `forbidden` paths (state dir, credential dirs) must not be bound, lie under a bound path
    or contain one. `no_ancestor` paths (the real HOME) may hold bound paths (a worktree under
    ~/Projects is fine: only the bound path itself is mounted and the rest of HOME stays
    unmounted) but must not themselves be bound or lie under a bound path, which would expose
    them. The executable is a single-file mount but must still not be, lie under or contain a
    forbidden path (a launcher stored in a credential dir is refused).
    Secrets travel via the process environment, never this argv (it is visible in /proc).
    """
    def _contains(outer: str, inner_: str) -> bool:
        return inner_ == outer or inner_.startswith(outer.rstrip("/") + "/")

    for bound in (worktree, scratch, *extra_ro_dirs):
        for bad in forbidden:
            if bad and (_contains(bad, bound) or _contains(bound, bad)):
                raise SandboxUnavailable("a sandbox mount would expose a protected path")
    for bad in forbidden:
        if bad and (_contains(executable, bad) or _contains(bad, executable)):
            raise SandboxUnavailable("a sandbox mount would expose a protected path")
    for bound in (worktree, scratch, executable, *extra_ro_dirs):
        for bad in no_ancestor:
            if bad and _contains(bound, bad):
                raise SandboxUnavailable("a sandbox mount would expose a protected path")
    argv = [
        bwrap, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--unshare-cgroup-try", "--cap-drop", "ALL",
        "--ro-bind", os.path.join(host_root, "usr"), "/usr", *_root_links(host_root),
        "--ro-bind", os.path.join(host_root, "etc"), "/etc", "--ro-bind-try", "/run/systemd/resolve",
        "/run/systemd/resolve", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
    ]
    for path in extra_ro_dirs:
        argv += ["--ro-bind", path, path]
    argv += ["--ro-bind", executable, executable, "--bind", scratch, scratch,
             "--bind", worktree, worktree]
    for secret in git_secret_masks(worktree, extra_ro_dirs):  # after the binds so they sit on top
        # --dev-bind, not --ro-bind/--remount-ro: both add nodev, which makes the char device unopenable
        # (EACCES). Writes land in /dev/null and the mount point cannot be unlinked or renamed (EBUSY).
        argv += ["--dev-bind", "/dev/null", secret]
    argv += ["--chdir", worktree, "--", *inner]
    return argv
