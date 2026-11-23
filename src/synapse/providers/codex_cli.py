"""Codex CLI provider adapter (disabled by default; runs only for a task that names a Codex profile).

Runs `codex exec --json` as a supervised subprocess in a caller-supplied git worktree.
Same-user, single trust domain, like the Claude adapter: the child runs as the daemon's OS
user, with a private per-run scratch HOME/CODEX_HOME, the prompt on stdin (never argv),
its own process group, bounded output and rlimits. See docs/operations/codex-cli-provider.md.

Capability restriction differs from Claude: Codex has no tool allowlist. The only
restriction is Codex's own sandbox (`-s read-only` or `-s workspace-write`); inside it the
agent may run any shell command, and network access is NOT disabled by this adapter. The
adapter never passes `danger-full-access`, `--dangerously-bypass-approvals-and-sandbox` or
`--approve-for-me`; callers cannot pass `-c` config overrides (the adapter adds a fixed set that
tightens `workspace-write`, see _SANDBOX_TIGHTENING).

The caller must be the sole reaper of the child (no SIGCHLD handler, no other waiter).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import json
import logging
import os
import re
import resource
import select
import shutil
import signal
import stat
import subprocess
import threading
import time
from uuid import uuid4

from synapse.domain.contracts import validate_id
from synapse.infrastructure.mock_process_identity import (
    ProcessIdentity, ProcessStatus, capture_mock_child, stop_mock_child_group,
)
from synapse.infrastructure.spawn_journal import SpawnJournal
from synapse.providers.claude_cli import validate_worktree
from synapse.providers.credential_sweep import write_marker
from synapse.providers.contracts import (
    ExecutionRequest, ProviderCapabilities, ProviderEvent, ProviderHealth, UsageObservation,
)
from synapse.providers.repo_config_guard import RepoConfigRefused, check_repo_config

_LOG = logging.getLogger(__name__)

# Versions whose `codex exec --json` argv and event schema were checked against this adapter
# (docs/operations/codex-cli-provider.md). Other versions are reported by probe() as
# "unverified_version"; that is advisory, and unknown event shapes still fail closed.
VERIFIED_CLI_VERSIONS = ("0.170.0",)
_VERSION_RE = re.compile(r"^codex-cli (\d{1,4}\.\d{1,4}\.\d{1,4})(?:\s|$)")
_VERSION_TIMEOUT_SECONDS = 10.0
_VERSION_MAX_BYTES = 4096

# `-s danger-full-access` is deliberately NOT allowed.
SANDBOX_MODES = frozenset({"read-only", "workspace-write"})
# extra_args is an ALLOWLIST (flag -> full-argument pattern), verified against `codex exec --help`
# 0.170.0. Only the `--flag=value` spelling is accepted so each entry is exactly one argv element.
# `-c/--config`, `-s`, `-C`, `--add-dir`, `--dangerously-*`, `--approve-for-me`, `--oss`, `-p`,
# `--enable/--disable`, `-i`, `-o` and `--output-schema` are refused.
_MODEL_RE = r"[A-Za-z0-9][A-Za-z0-9._\-]{0,63}"
_ALLOWED_EXTRA: Mapping[str, re.Pattern[str]] = {
    "--model": re.compile(rf"--model={_MODEL_RE}"),
}
# Fixed (never caller-supplied) config overrides that tighten `workspace-write`. Observed on 0.170.0:
# by default that sandbox is NOT confined to the worktree, it also lets the agent write under /tmp and
# $TMPDIR (a real run created a file in a sibling directory under /tmp). `exclude_slash_tmp` and
# `exclude_tmpdir_env_var` remove those writable roots; `network_access=false` states the network
# policy explicitly (the key exists in the 0.170.0 binary; its effect is not yet verified here).
_SANDBOX_TIGHTENING: Mapping[str, tuple[str, ...]] = {
    "read-only": (),
    "workspace-write": ("-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                        "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                        "-c", "sandbox_workspace_write.network_access=false"),
}
# Env passed through from the daemon. HOME, CODEX_HOME and TMPDIR are replaced by a per-run
# scratch dir; no OpenAI/Codex credential variables are passed through (auth is the scratch
# copy of auth.json, see _place_credentials).
_ENV_ALLOWLIST = ("PATH", "LANG")
_RLIMIT_NOFILE = 4096
_RLIMIT_FSIZE = 1024 * 1024 * 1024
_NPROC_HEADROOM = 1024
_MAX_PROMPT_BYTES = 100_000
_MAX_CREDENTIAL_BYTES = 1 << 20
# Item types that count as tool use when an `item.started` event names them. Only the type name
# is surfaced, never the command line, paths or arguments (they can contain secrets).
_TOOL_ITEM_TYPE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_NON_TOOL_ITEMS = frozenset({"agent_message", "reasoning", "error", "user_message"})


_MAX_COUNT = 2**63 - 1  # SQLite INTEGER; same bound as the Claude adapter


def _count(value: object) -> int | None:
    """A reported token count, or None (unknown) when negative, non-int or not storable."""
    return value if type(value) is int and 0 <= value <= _MAX_COUNT else None


KEEP_FINISHED = 64  # finished executions retained for usage()/cancel() after the stream ends


class CodexOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"  # process outcome not established: never retry blindly


# Task eligibility for a Codex profile (ADR 016 decision 4). A Synapse task carries Claude-shaped
# `allowed_tools` and `permission_mode`; Codex has no tool allowlist, so they only select a sandbox
# class. Tools the sandbox cannot express (any `Bash(...)` pattern, WebFetch, ...) make the task
# ineligible rather than being silently widened, and the Claude-only modes `dontAsk` and `manual`
# have no Codex equivalent.
CODEX_TASK_TOOLS = frozenset({"Read", "Glob", "Grep", "Edit", "Write"})
_WRITE_TOOLS = frozenset({"Edit", "Write"})
_MODE_SANDBOX = {"plan": "read-only", "acceptEdits": "workspace-write"}


class CodexIneligible(ValueError):
    """The task's tools or permission mode cannot be mapped onto a Codex sandbox."""


def sandbox_for_task(allowed_tools, permission_mode) -> str:
    """The Codex sandbox for a task, or CodexIneligible. Fixed table, nothing configurable.

    ``plan`` -> ``read-only``; ``acceptEdits`` -> ``workspace-write`` when Edit or Write is
    requested, else ``read-only``. ``Edit``/``Write`` with ``plan`` is refused (it would either
    silently drop the write or widen ``plan``).
    """
    if permission_mode not in _MODE_SANDBOX:
        # Fixed text (it reaches API errors, which never echo input).
        raise CodexIneligible("permission_mode is not supported on a codex profile (only plan and acceptEdits)")
    if (isinstance(allowed_tools, (str, bytes)) or not all(isinstance(t, str) for t in allowed_tools)
            or not set(allowed_tools) <= CODEX_TASK_TOOLS):
        raise CodexIneligible("allowed_tools on a codex profile must be a subset of %s"
                              % ", ".join(sorted(CODEX_TASK_TOOLS)))
    writes = bool(set(allowed_tools) & _WRITE_TOOLS)
    if writes and permission_mode == "plan":
        raise CodexIneligible("Edit/Write need permission_mode acceptEdits on a codex profile")
    return "workspace-write" if writes else "read-only"


def cli_version(executable: str, environ: Mapping[str, str]) -> str | None:
    """Run ``<executable> --version``; return "X.Y.Z" or None. Wall-clock bounded by one deadline.

    Own session/process group, scrubbed env, stderr discarded. stdout is read from the raw, non-blocking fd in this
    thread with select against the deadline and capped at _VERSION_MAX_BYTES; once the child has exited only what is
    already in the pipe is read, so a descendant that escaped the group (e.g. ``setsid``) and keeps the pipe open
    cannot delay the result. The group is then killed and the child reaped; the fd is closed with ``os.close``.
    A descendant that left the group via ``setsid`` is not killed (it is outside the group by construction).
    """
    env = {k: v for k, v in environ.items() if k in _ENV_ALLOWLIST}
    deadline = time.monotonic() + _VERSION_TIMEOUT_SECONDS
    try:
        proc = subprocess.Popen([executable, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, cwd="/", start_new_session=True,
                                close_fds=True, bufsize=0)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    fd = os.dup(proc.stdout.fileno())  # our own descriptor: the file object is closed now, never read
    proc.stdout.close()
    os.set_blocking(fd, False)
    buf = bytearray()
    exited = False
    eof = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not exited:
                # WNOWAIT keeps the exited leader as a zombie, so its process-group id cannot be reused before killpg.
                exited = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            if not eof and len(buf) < _VERSION_MAX_BYTES:
                ready, _, _ = select.select([fd], [], [], 0 if exited else min(0.02, remaining))
                if ready:
                    try:
                        chunk = os.read(fd, _VERSION_MAX_BYTES - len(buf))
                    except BlockingIOError:
                        chunk = None
                    if chunk == b"":
                        eof = True
                    elif chunk:
                        buf += chunk
                        continue  # drain what is there before deciding
            elif not exited:
                time.sleep(min(0.02, remaining))
            if exited:
                break  # everything the child wrote is already in the pipe; never wait for a descendant's EOF
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the group: also descendants that outlived the child
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        os.close(fd)
    if not exited or proc.returncode != 0:
        return None
    return parse_cli_version(bytes(buf[:256]).decode("utf-8", "replace"))


_VERSION_CACHE: dict[tuple, str | None] = {}
_VERSION_CACHE_MAX = 16
_VERSION_CACHE_LOCK = threading.Lock()


def cached_cli_version(executable: str, environ: Mapping[str, str]) -> str | None:
    """``cli_version`` cached by the executable's identity (realpath, device, inode, size, mtime_ns); bounded.

    Replacing or modifying the binary changes the key, so the next spawn re-checks. A failed stat is never cached.
    """
    try:
        real = os.path.realpath(executable)
        st = os.stat(real)
    except OSError:
        return None
    key = (real, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    with _VERSION_CACHE_LOCK:
        if key in _VERSION_CACHE:
            return _VERSION_CACHE[key]
    version = cli_version(executable, environ)
    with _VERSION_CACHE_LOCK:
        if len(_VERSION_CACHE) >= _VERSION_CACHE_MAX:
            _VERSION_CACHE.pop(next(iter(_VERSION_CACHE)))
        _VERSION_CACHE[key] = version
    return version


def parse_cli_version(output: str) -> str | None:
    """Return "X.Y.Z" from `codex --version` output such as "codex-cli 0.170.0", else None."""
    m = _VERSION_RE.match(output.strip()[:64])
    return m.group(1) if m else None


@dataclass(frozen=True, slots=True)
class CodexCliProfile:
    """Strictly validated, immutable configuration. Disabled unless enabled=True."""

    executable: str
    state_dir: str
    enabled: bool = False
    sandbox: str = "read-only"
    extra_args: tuple[str, ...] = ()
    credentials_file: str | None = None  # auth.json copied into the scratch CODEX_HOME (0600)
    allow_repo_codex_config: bool = False  # waive the refusal for .codex/AGENTS.md in the worktree
    memory_limit_bytes: int | None = None  # RLIMIT_AS; opt-in
    max_timeout_seconds: float = 3600.0
    max_output_bytes_limit: int = 16 * 1024 * 1024
    max_line_bytes: int = 1024 * 1024
    term_grace_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.executable, str) or "\x00" in self.executable or not os.path.isabs(self.executable):
            raise ValueError("executable must be an absolute path")
        if not isinstance(self.state_dir, str) or "\x00" in self.state_dir or not os.path.isabs(self.state_dir):
            raise ValueError("state_dir must be an absolute path")
        if self.sandbox not in SANDBOX_MODES:
            raise ValueError("unsupported sandbox mode")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a bool")
        if not isinstance(self.extra_args, tuple) or len(self.extra_args) > 16:
            raise ValueError("extra_args must be a short tuple")
        seen: set[str] = set()
        for arg in self.extra_args:
            name = arg.split("=", 1)[0] if isinstance(arg, str) else ""
            pattern = _ALLOWED_EXTRA.get(name)
            if pattern is None or not pattern.fullmatch(arg) or name in seen:
                raise ValueError("extra_args entry is not allowed")
            seen.add(name)
        if self.credentials_file is not None and (
                not isinstance(self.credentials_file, str) or "\x00" in self.credentials_file
                or not os.path.isabs(self.credentials_file)):
            raise ValueError("credentials_file must be an absolute path")
        if self.credentials_file is not None:
            sd = os.path.realpath(self.state_dir)
            if os.path.commonpath([os.path.realpath(os.path.dirname(self.credentials_file)), sd]) == sd:
                raise ValueError("credentials_file must not be inside state_dir")
        if type(self.allow_repo_codex_config) is not bool:
            raise ValueError("allow_repo_codex_config must be a bool")
        if self.memory_limit_bytes is not None and (
                type(self.memory_limit_bytes) is not int or not 1 << 30 <= self.memory_limit_bytes <= 1 << 46):
            raise ValueError("memory_limit_bytes out of range")
        for name, value, lo, hi in (
            ("max_timeout_seconds", self.max_timeout_seconds, 0.001, 86_400),
            ("term_grace_seconds", self.term_grace_seconds, 0, 30),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
                raise ValueError(f"{name} out of range")
        for name, value in (("max_output_bytes_limit", self.max_output_bytes_limit),
                            ("max_line_bytes", self.max_line_bytes)):
            if type(value) is not int or not 1024 <= value <= 256 * 1024 * 1024:
                raise ValueError(f"{name} out of range")
        if self.max_line_bytes > self.max_output_bytes_limit:
            raise ValueError("max_line_bytes must not exceed max_output_bytes_limit")


@dataclass(frozen=True, slots=True)
class _Plan:
    argv: tuple[str, ...]  # complete argv; the prompt is delivered on stdin ("-")
    prompt: str  # written to a 0600 file in the scratch dir, never argv
    cwd: str
    env: Mapping[str, str]
    timeout: float
    max_output: int
    cpu_seconds: int


@dataclass(slots=True)
class _Execution:
    plan: _Plan
    lock: threading.Lock = field(default_factory=threading.Lock)
    process: subprocess.Popen | None = None
    identity: ProcessIdentity | None = None
    reason: str | None = None  # cancelled / timed_out / output_limit / protocol_error
    reaped: bool = False
    stop_status: ProcessStatus | None = None
    spawn_failed: bool = False
    spawn_detail: str | None = None  # repo-config refusal reason (never a path outside the worktree)
    returncode: int | None = None
    streaming: bool = False
    finished: bool = False
    finish_seq: int = 0  # order in which executions finished (pruning keeps the most recently finished)
    result_ok: bool | None = None  # True: turn.completed, False: turn.failed
    usage: UsageObservation | None = None
    scratch: str | None = None  # per-run private HOME/CODEX_HOME root, removed after the run
    credentials_copy: bytes | None = None  # what was placed, to notice an in-scratch token refresh
    journal_key: str | None = None  # spawn journal entry for this process (ADR 023)


class CodexCliProvider:
    """ProviderAdapter running `codex exec --json` as a supervised subprocess.

    Request input keys: prompt (str), worktree (str), timeout_seconds (number, optional),
    max_output_bytes (int, optional). One execution is one process.
    """

    def __init__(self, profile: CodexCliProfile, *, environ: Mapping[str, str] | None = None,
                 spawn_journal: SpawnJournal | None = None) -> None:
        if not isinstance(profile, CodexCliProfile):
            raise TypeError("profile must be a CodexCliProfile")
        self._profile = profile
        self._environ = os.environ if environ is None else environ
        self._journal = spawn_journal  # durable pid record so a restart can fence orphans (ADR 023)
        self._executions: dict[str, _Execution] = {}
        self._finish_counter = 0

    # --- contract surface -------------------------------------------------
    async def probe(self, profile_id: str) -> ProviderHealth:
        """healthy (verified version), unverified_version (runs, version not verified) or unavailable."""
        validate_id(profile_id)
        p = self._profile
        if not (p.enabled and os.path.isfile(p.executable) and os.access(p.executable, os.X_OK)):
            return ProviderHealth(profile_id, "unavailable")
        version = await asyncio.to_thread(self._cli_version)
        if version is None:
            return ProviderHealth(profile_id, "unavailable")
        return ProviderHealth(profile_id, "healthy" if version in VERIFIED_CLI_VERSIONS else "unverified_version")

    def _cli_version(self) -> str | None:
        """Run `<executable> --version` with a scrubbed environment; None on any failure."""
        return cli_version(self._profile.executable, self._environ)

    def cached_cli_version(self) -> str | None:
        """``_cli_version`` cached per executable identity (path, inode, size, mtime); see ``cached_cli_version``."""
        return cached_cli_version(self._profile.executable, self._environ)

    async def capabilities(self, profile_id: str) -> ProviderCapabilities:
        validate_id(profile_id)
        return ProviderCapabilities(True, True, True, False, False, True, False, None)

    async def usage(self, handle: str) -> UsageObservation | None:
        return self._execution(handle).usage

    async def start(self, request: ExecutionRequest) -> str:
        """Validate and plan an execution. The child is spawned when streamed."""
        if not self._profile.enabled:
            raise RuntimeError("Codex CLI provider is disabled")
        if not isinstance(request, ExecutionRequest):
            raise TypeError("ExecutionRequest required")
        plan = await asyncio.to_thread(self._plan, request)  # the repo-config scan walks the tree: off the event loop
        handle = str(uuid4())
        self._executions[handle] = _Execution(plan)
        return handle

    async def cancel(self, handle: str) -> None:
        ex = self._execution(handle)
        if ex.finished or ex.reaped:
            return
        if ex.reason is None:
            ex.reason = "cancelled"
        await asyncio.shield(asyncio.to_thread(self._terminate, ex))

    async def stream(self, handle: str) -> AsyncIterator[ProviderEvent]:
        ex = self._execution(handle)
        if ex.streaming or ex.finished:
            raise RuntimeError("Execution stream has already been consumed")
        ex.streaming = True
        plan = ex.plan
        deadline = time.monotonic() + plan.timeout
        try:
            yield ProviderEvent("started")
            await asyncio.to_thread(self._spawn, ex)
            if ex.process is not None and ex.reason is None:
                async for event in self._read(ex, deadline):
                    yield event
            if ex.process is not None and not ex.reaped:
                await self._settle(ex, deadline)
            yield ProviderEvent(*self._outcome(ex))
        finally:
            ex.finished = True
            self._finish_counter += 1
            ex.finish_seq = self._finish_counter
            if not ex.reaped:
                if ex.reason is None:
                    ex.reason = "cancelled"
                await asyncio.shield(asyncio.to_thread(self._terminate, ex))
            self._cleanup(ex)
            self._prune_finished()

    def _prune_finished(self) -> None:
        """Keep only the newest KEEP_FINISHED finished executions (usage()/cancel() stay valid for those)."""
        # Oldest FINISHED first (not start order): a long-running execution that finishes last is the newest
        # finished entry, so its own usage()/cancel() stay valid however many later runs ended before it.
        done = sorted((h for h, e in self._executions.items() if e.finished), key=lambda h: self._executions[h].finish_seq)
        for h in done[:max(0, len(done) - KEEP_FINISHED)]:
            del self._executions[h]

    # --- planning ----------------------------------------------------------
    def _plan(self, request: ExecutionRequest) -> _Plan:
        p, data = self._profile, request.input
        prompt = data.get("prompt")
        if (not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt
                or len(prompt.encode("utf-8", "strict")) > _MAX_PROMPT_BYTES):
            raise ValueError("prompt must be a non-empty string without NUL, at most 100000 bytes")
        timeout = data.get("timeout_seconds", p.max_timeout_seconds)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= p.max_timeout_seconds:
            raise ValueError("timeout_seconds out of range")
        cap = data.get("max_output_bytes", p.max_output_bytes_limit)
        if type(cap) is not int or not 1 <= cap <= p.max_output_bytes_limit:
            raise ValueError("max_output_bytes out of range")
        cwd = validate_worktree(data.get("worktree"), p.state_dir)
        self._check_repo_config(cwd)
        self._credential_source(cwd)  # refuse an unsafe credential source at start(), before any spawn
        # The trailing "-" makes `codex exec` read the prompt from stdin, so it never appears in
        # /proc/<pid>/cmdline (verified, codex-cli 0.170.0). --ignore-user-config and --ignore-rules
        # keep the (scratch) user config and rules out; -C pins the working root to the worktree.
        argv = (
            p.executable, "exec", "--json", "-s", p.sandbox, *_SANDBOX_TIGHTENING[p.sandbox], "--ephemeral",
            "--ignore-user-config", "--ignore-rules", "-C", cwd, *p.extra_args, "-",
        )
        env = {k: self._environ[k] for k in _ENV_ALLOWLIST if k in self._environ}
        cpu = int(min(float(timeout) + p.term_grace_seconds + 60, 86_400 + 90))
        return _Plan(argv, prompt, cwd, env, float(timeout), cap, cpu)

    def _check_repo_config(self, cwd: str) -> None:
        check_repo_config(cwd, "codex", self._profile.allow_repo_codex_config)  # list: repo_config_guard

    # --- per-run scratch dir ----------------------------------------------
    def _make_scratch(self, ex: _Execution) -> tuple[str, str, str]:
        """Create <state>/codex-run/<uuid>/{home,codex,tmp} (0700). Returns (root, home, codex_home)."""
        base = os.path.join(self._profile.state_dir, "codex-run")
        os.makedirs(base, mode=0o700, exist_ok=True)
        os.chmod(base, 0o700)
        root = os.path.join(base, uuid4().hex)
        os.mkdir(root, 0o700)
        ex.scratch = root
        write_marker(root)  # credential_sweep: lets a startup sweep reclaim this after SIGKILL
        home, codex_home, tmp = (os.path.join(root, n) for n in ("home", "codex", "tmp"))
        for d in (home, codex_home, tmp):
            os.mkdir(d, 0o700)
        self._place_credentials(ex, codex_home)
        self._write_private(os.path.join(root, "prompt.txt"), ex.plan.prompt.encode("utf-8"))
        return root, home, codex_home

    @staticmethod
    def _write_private(path: str, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

    def _credential_source(self, cwd: str | None = None) -> str | None:
        """Resolve and vet the credential source path (None when there is none). Raises ValueError
        for an unsafe one: a symlink, a non-regular file, group/other-accessible, owned by another
        user, or located inside the worktree or the state directory (agent-readable locations)."""
        src = self._profile.credentials_file
        if src is None:
            ch, home = self._environ.get("CODEX_HOME"), self._environ.get("HOME")
            candidates = [os.path.join(ch, "auth.json")] if ch else []
            if home:
                candidates.append(os.path.join(home, ".codex", "auth.json"))
            src = next((c for c in candidates if os.path.lexists(c)), None)
            if src is None:
                return None
        elif not os.path.lexists(src):
            return None  # Codex will report an auth failure
        if cwd is not None:
            for base in (cwd, self._profile.state_dir):
                real = os.path.realpath(base)
                if os.path.commonpath([os.path.realpath(os.path.dirname(src)), real]) == real:
                    raise ValueError("credentials source must not be inside the worktree or state directory")
        self._read_credentials(src)  # vets mode/type/size; the bytes are discarded
        return src

    @staticmethod
    def _read_credentials(src: str) -> bytes:
        try:
            fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            raise ValueError("credentials source is not a readable regular file (symlinks are refused)") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("credentials source must be a regular file")
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise ValueError("credentials source must be owned by the current user with mode 0600 or stricter")
            data = os.read(fd, _MAX_CREDENTIAL_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > _MAX_CREDENTIAL_BYTES:
            raise ValueError("credentials source is too large")
        return data

    def _place_credentials(self, ex: _Execution, codex_home: str) -> None:
        """Copy the single auth.json (file credential backend) into the scratch CODEX_HOME at 0600.

        Source: the profile's credentials_file, else $CODEX_HOME/auth.json, else ~/.codex/auth.json.
        Nothing else from the real HOME or CODEX_HOME is copied.
        """
        src = self._credential_source(ex.plan.cwd)
        if src is None:
            return
        data = self._read_credentials(src)
        self._write_private(os.path.join(codex_home, "auth.json"), data)
        ex.credentials_copy = data

    def _note_credential_refresh(self, ex: _Execution) -> None:
        """Warn (never log contents) if Codex rewrote the scratch auth.json, e.g. refreshed tokens.

        A refresh happens only in the scratch copy, which is then deleted; the source file is never
        written back. If the provider rotates refresh tokens, the source login may need `codex login`.
        """
        if ex.scratch is None or ex.credentials_copy is None:
            return
        try:
            with open(os.path.join(ex.scratch, "codex", "auth.json"), "rb") as fh:
                now = fh.read(1 << 20)
        except OSError:
            return
        if now != ex.credentials_copy:
            _LOG.warning("Codex rewrote the scratch auth.json during a run; the source login was not updated")

    def _cleanup(self, ex: _Execution) -> None:
        root, ex.scratch = ex.scratch, None
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)

    def _preexec(self, cpu_seconds: int):
        mem = self._profile.memory_limit_bytes
        nproc = self._nproc_limit()
        limits = ((resource.RLIMIT_CPU, cpu_seconds), (resource.RLIMIT_NOFILE, _RLIMIT_NOFILE),
                  (resource.RLIMIT_FSIZE, _RLIMIT_FSIZE), (resource.RLIMIT_CORE, 0),
                  (resource.RLIMIT_NPROC, nproc), (resource.RLIMIT_AS, mem))

        def apply() -> None:  # runs in the forked child before exec: setrlimit only
            for res, val in limits:
                if val is None:
                    continue
                _, hard = resource.getrlimit(res)
                if hard != resource.RLIM_INFINITY:
                    val = min(val, hard)
                resource.setrlimit(res, (val, val))
        return apply

    @staticmethod
    def _nproc_limit() -> int | None:
        """RLIMIT_NPROC counts every task (thread) of the uid, so bound it relative to current usage."""
        uid, count = os.getuid(), 0
        try:
            for name in os.listdir("/proc"):
                if name.isdigit():
                    try:
                        if os.stat(f"/proc/{name}").st_uid == uid:
                            count += len(os.listdir(f"/proc/{name}/task"))
                    except OSError:
                        pass
        except OSError:
            return None
        return count + _NPROC_HEADROOM

    # --- process lifecycle (sync helpers run in threads) -------------------
    def _spawn(self, ex: _Execution) -> None:
        with ex.lock:
            if ex.reason is not None or ex.reaped:
                ex.reaped = True
                return
            plan = ex.plan
            try:
                self._check_repo_config(plan.cwd)  # re-check: the worktree may change after start()
                root, home, codex_home = self._make_scratch(ex)
                env = {**plan.env, "HOME": home, "CODEX_HOME": codex_home, "TMPDIR": os.path.join(root, "tmp")}
                if self._journal is not None:
                    ex.journal_key = uuid4().hex
                    self._journal.begin(ex.journal_key)  # intent first: cannot journal means cannot spawn
                with open(os.path.join(root, "prompt.txt"), "rb") as stdin:
                    process = subprocess.Popen(
                        list(plan.argv), cwd=plan.cwd, env=env, shell=False,
                        stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        close_fds=True, start_new_session=True,
                        preexec_fn=self._preexec(plan.cpu_seconds),
                    )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self._cleanup(ex)
                self._journal_finish(ex)  # no process was created
                ex.spawn_failed = True
                if isinstance(exc, RepoConfigRefused):
                    # The text can carry a repo-controlled relative path: escape control characters, cap the length.
                    ex.spawn_detail = repr(str(exc))[1:-1][:200]
                ex.reaped = True
                return
            ex.process = process
            try:
                ex.identity = capture_mock_child(process)
                if self._journal is not None and ex.journal_key is not None:
                    self._journal.record(ex.journal_key, ex.identity)
            except (ValueError, OSError):
                ex.stop_status = ProcessStatus.UNKNOWN
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                ex.reaped = True
                self._cleanup(ex)
                if process.returncode is not None:
                    self._journal_finish(ex)  # killed and reaped; otherwise the entry stays for the next startup

    def _terminate(self, ex: _Execution) -> None:
        """TERM the group, bounded grace, KILL, reap exactly once."""
        with ex.lock:
            if ex.reaped:
                return
            if ex.process is None:
                ex.reaped = True
                return
            ex.stop_status = stop_mock_child_group(
                ex.process, ex.identity, grace_seconds=self._profile.term_grace_seconds)
            ex.reaped = True
            ex.returncode = ex.process.returncode
            self._close(ex)

    def _journal_finish(self, ex: _Execution) -> None:
        key, ex.journal_key = ex.journal_key, None
        if self._journal is not None and key is not None:
            try:
                self._journal.finish(key)
            except OSError:
                _LOG.warning("spawn journal entry could not be removed; the next startup will fence it")

    def _close(self, ex: _Execution) -> None:
        self._note_credential_refresh(ex)
        self._cleanup(ex)
        if ex.stop_status != ProcessStatus.UNKNOWN:  # UNKNOWN: the process may still run, keep the entry
            self._journal_finish(ex)
        try:
            if ex.process is not None and ex.process.stdout is not None:
                ex.process.stdout.close()
        except OSError:
            pass

    def _wait_exit(self, ex: _Execution, deadline: float) -> bool:
        """Observe leader exit without reaping (WNOWAIT) so its PID stays fenced."""
        pid = ex.process.pid
        while True:
            try:
                if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                    return True
            except ChildProcessError:
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    @staticmethod
    def _leader_exited(ex: _Execution) -> bool:
        try:
            return os.waitid(os.P_PID, ex.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            return False

    @staticmethod
    def _kill_group(ex: _Execution) -> None:
        with ex.lock:
            if ex.reaped:
                return
            try:
                os.killpg(ex.process.pid, signal.SIGKILL)  # leader is an unreaped zombie: PID fenced
            except OSError:
                pass

    def _reap_natural(self, ex: _Execution) -> None:
        """Leader exited (zombie, unreaped): SIGKILL stragglers in its group, then reap."""
        with ex.lock:
            if ex.reaped:
                return
            try:
                os.killpg(ex.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                ex.stop_status = ProcessStatus.UNKNOWN
            try:
                ex.returncode = ex.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                ex.stop_status = ProcessStatus.UNKNOWN
            ex.reaped = True
            self._close(ex)

    async def _settle(self, ex: _Execution, deadline: float) -> None:
        if ex.reason is None:
            exited = await asyncio.to_thread(self._wait_exit, ex, deadline)
            if exited:
                await asyncio.to_thread(self._reap_natural, ex)
                return
            if ex.reason is None:
                ex.reason = "timed_out"
        await asyncio.shield(asyncio.to_thread(self._terminate, ex))

    # --- output parsing ----------------------------------------------------
    async def _read(self, ex: _Execution, deadline: float) -> AsyncIterator[ProviderEvent]:
        plan, prof = ex.plan, self._profile
        stdout = ex.process.stdout
        limit = prof.max_line_bytes
        total = 0
        leader_gone = False
        while True:
            fut = asyncio.ensure_future(asyncio.to_thread(stdout.readline, limit + 1))
            try:
                while not fut.done():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        ex.reason = ex.reason or "timed_out"
                        break
                    await asyncio.wait({fut}, timeout=min(remaining, 0.25))
                    if not fut.done() and not leader_gone and ex.reason is None:
                        # Leader exited but a leftover descendant may hold the pipe open:
                        # kill the group (no reap) so readline reaches EOF.
                        leader_gone = await asyncio.to_thread(self._leader_exited, ex)
                        if leader_gone:
                            await asyncio.to_thread(self._kill_group, ex)
                    if ex.reason is not None:
                        break
            except BaseException:
                fut.cancel()
                raise
            if ex.reason is not None:
                await asyncio.shield(asyncio.to_thread(self._terminate, ex))
                await asyncio.gather(fut, return_exceptions=True)
                return
            try:
                line = fut.result()
            except (OSError, ValueError):
                return
            if not line:
                return
            total += len(line)
            if len(line) > limit or total > plan.max_output:
                ex.reason = "output_limit"
                await asyncio.shield(asyncio.to_thread(self._terminate, ex))
                return
            try:
                events = self._parse(ex, line)
            except ValueError:
                ex.reason = "protocol_error"
                await asyncio.shield(asyncio.to_thread(self._terminate, ex))
                return
            for event in events:
                yield event

    def _parse(self, ex: _Execution, line: bytes) -> list[ProviderEvent]:
        """Map one `codex exec --json` line (schema observed on codex-cli 0.170.0) to events."""
        text = line.decode("utf-8", "strict").strip()
        if not text:
            return []
        try:
            msg = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("malformed JSON") from exc
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            raise ValueError("unexpected message shape")
        kind = msg["type"]
        if kind in ("item.started", "item.completed"):
            item = msg.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise ValueError("malformed item")
            itype = item["type"]
            if kind == "item.completed" and itype == "agent_message":
                body = item.get("text")
                if not isinstance(body, str):
                    raise ValueError("malformed agent message")
                return [ProviderEvent("output", body)]
            if kind == "item.started" and itype not in _NON_TOOL_ITEMS and _TOOL_ITEM_TYPE.fullmatch(itype):
                return [ProviderEvent("tool_use", itype)]  # the item type only, never commands or paths
            return []  # reasoning, non-fatal `error` items, completions of tools, unknown future items
        if kind == "turn.completed":
            if ex.result_ok is not None:
                raise ValueError("duplicate terminal turn event")
            ex.result_ok = True
            usage = msg.get("usage")
            if isinstance(usage, dict):
                a, b = usage.get("input_tokens"), usage.get("output_tokens")
                ex.usage = UsageObservation(_count(a), _count(b), "codex-cli")
        elif kind == "turn.failed":
            if ex.result_ok is not None:
                raise ValueError("duplicate terminal turn event")
            ex.result_ok = False
        return []  # thread.started, turn.started, top-level `error` (turn.failed decides), unknown types

    # --- outcome -----------------------------------------------------------
    def _outcome(self, ex: _Execution) -> tuple[str, str | None]:
        if ex.spawn_failed:
            return CodexOutcome.FAILED.value, ex.spawn_detail or "spawn_failed"
        if ex.reason == "cancelled" and ex.process is None:
            return CodexOutcome.CANCELLED.value, None
        if ex.stop_status is ProcessStatus.UNKNOWN or not ex.reaped:
            return CodexOutcome.UNKNOWN.value, "process_outcome_unestablished"
        if ex.reason == "cancelled":
            return CodexOutcome.CANCELLED.value, None
        if ex.reason == "timed_out":
            return CodexOutcome.TIMED_OUT.value, None
        if ex.reason in ("output_limit", "protocol_error"):
            return CodexOutcome.FAILED.value, ex.reason
        if ex.returncode == 0 and ex.result_ok is True:
            return CodexOutcome.SUCCEEDED.value, None
        if ex.returncode == 0 and ex.result_ok is None:
            return CodexOutcome.UNKNOWN.value, "no_turn_completed"
        return CodexOutcome.FAILED.value, "exit_nonzero" if ex.returncode else "turn_failed"

    def _execution(self, handle: str) -> _Execution:
        validate_id(handle)
        try:
            return self._executions[handle]
        except KeyError as exc:
            raise ValueError("Unknown Codex CLI execution") from exc
