"""Opt-in Linux namespace launcher for one fixed synthetic mock worker.

This module is not wired to the daemon or a provider adapter. Callers must
supply a worktree they already own and authorize; no live CLI is executed.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import stat
import subprocess

_BWRAP = "/usr/bin/bwrap"
_PYTHON = "/usr/bin/python3"
_OUTPUT = b"synapse mock candidate v1\n"
_PROGRAM = "import sys; sys.stdout.buffer.write(b'synapse mock candidate v1\\n')"


class SandboxUnavailable(RuntimeError):
    """Required sandbox isolation was unavailable or failed."""


def _worktree(managed_root: Path, worktree: Path) -> Path:
    root = Path(managed_root)
    tree = Path(worktree)
    for path in (root, tree):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("Managed root and worktree must be user-owned directories")
    root_resolved = root.resolve(strict=True)
    tree_resolved = tree.resolve(strict=True)
    if root_resolved == tree_resolved or not tree_resolved.is_relative_to(root_resolved):
        raise ValueError("Worktree must be below the managed root")
    return tree_resolved


def _command(worktree: Path, program: str) -> list[str]:
    # program is a trusted built-in constant in production. Tests exercise the
    # namespace boundary with adversarial programs via this private helper.
    return [
        _BWRAP, "--unshare-all", "--die-with-parent", "--new-session",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib64", "/lib64",
        "--symlink", "usr/lib", "/lib", "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp", "--bind", str(worktree), "/work", "--chdir", "/work",
        "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", "--",
        _PYTHON, "-I", "-c", program,
    ]


def _start(managed_root: Path, worktree: Path, program: str) -> subprocess.Popen:
    tree = _worktree(managed_root, worktree)
    try:
        return subprocess.Popen(_command(tree, program), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                                close_fds=True, start_new_session=True)
    except OSError as exc:
        raise SandboxUnavailable("Cannot start required mock sandbox") from exc


def _stop(process: subprocess.Popen, *, grace: float = 1.0) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()


def run_fixed_mock(managed_root: Path, worktree: Path, *, timeout: float = 10.0) -> bytes:
    """Run the fixed synthetic worker or fail closed; no CLI arguments are accepted."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise ValueError("timeout must be greater than zero and at most 300 seconds")
    process = _start(managed_root, worktree, _PROGRAM)
    try:
        output, _ = process.communicate(timeout=timeout)
        if process.returncode != 0 or output != _OUTPUT:
            raise SandboxUnavailable("Mock sandbox failed or returned an invalid response")
        return output
    except BaseException:
        _stop(process)
        raise
