"""Claude Code CLI provider adapter (prototype, disabled by default).

Runs `claude -p --output-format stream-json` as a supervised subprocess in a
caller-supplied git worktree. Same-user, single trust domain: the child runs as
the daemon's OS user and is NOT sandboxed unless the profile sets `sandbox` (opt-in
bubblewrap, a same-uid mitigation, fail closed), but it gets a private per-run scratch
HOME/CLAUDE_CONFIG_DIR, only Synapse-owned settings (repo/user settings and hooks
are not loaded) and bounded rlimits. See docs/operations/claude-cli-provider.md.

Not wired into the daemon, API or CLI. The caller must be the sole reaper of
the child (no SIGCHLD handler, no other waiter).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import json
import logging
import math
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import stat
import subprocess
import tempfile
import uuid
import threading
import time
from uuid import uuid4

from synapse.domain.contracts import validate_id
from synapse.infrastructure.spawn_journal import SpawnJournal
from synapse.infrastructure.mock_process_identity import (
    ProcessIdentity, ProcessStatus, capture_mock_child, stop_mock_child_group,
)
from synapse.providers.credential_sweep import write_marker
from synapse.providers.contracts import (
    ExecutionRequest, ProviderCapabilities, ProviderEvent, ProviderHealth, UsageObservation,
)
from synapse.providers.repo_config_guard import check_repo_config
from synapse.providers.sandbox_launcher import (
    SandboxUnavailable, build_bwrap_argv, find_bwrap, forbidden_home_paths, git_readonly_dirs, require_sandbox,
    sandbox_homes,
)

# Subset of `claude --permission-mode` choices (2.1.329: acceptEdits, auto, bypassPermissions,
# manual, dontAsk, plan). "auto" is deliberately NOT allowed: it delegates approval decisions
# to the CLI's own automatic classifier, so the allowlist/mode would no longer be a fixed,
# auditable guardrail owned by Synapse.
PERMISSION_MODES = frozenset({"acceptEdits", "plan", "dontAsk", "manual", "bypassPermissions"})
_TOOL_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{0,63}(\([^\x00-\x1f()]{1,200}\))?")
# `--tools` treats "" (none) and "default" (the whole default set, incl. Bash/Write) as special values,
# so they must never be reachable as a tool base name.
_SPECIAL_TOOL_NAMES = frozenset({"", "default"})
# Tools whose absence defines a read-only profile; explicitly denied (second layer) for such profiles.
_MUTATING_TOOLS = ("Write", "Edit", "MultiEdit", "Bash", "WebFetch", "NotebookEdit")
# F12: extra_args is an ALLOWLIST (flag name -> full-argument pattern), verified against
# `claude --help` 2.1.329. Everything else (permission/tool/dir/settings/MCP/session/system
# prompt/IDE/plugin flags, combined short flags, `--permission-prompt-tool`, ...) is refused.
# Only the `--flag=value` spelling is accepted so each entry is exactly one argv element.
_MODEL_RE = r"[A-Za-z0-9][A-Za-z0-9._\-]{0,63}"
_ALLOWED_EXTRA: Mapping[str, re.Pattern[str]] = {
    "--model": re.compile(rf"--model={_MODEL_RE}"),
    "--fallback-model": re.compile(rf"--fallback-model={_MODEL_RE}(,{_MODEL_RE}){{0,3}}"),
    "--effort": re.compile(r"--effort=(low|medium|high|xhigh|max)"),
    "--max-budget-usd": re.compile(r"--max-budget-usd=[0-9]{1,6}(\.[0-9]{1,4})?"),
    "--exclude-dynamic-system-prompt-sections": re.compile(r"--exclude-dynamic-system-prompt-sections"),
}
# Env passed through from the daemon. HOME and CLAUDE_CONFIG_DIR are NOT passed through:
# they are replaced by a per-run scratch dir. CLAUDE_CODE_OAUTH_TOKEN (`claude setup-token`)
# is the headless-friendly credential that needs no file copy.
_ENV_ALLOWLIST = ("PATH", "LANG", "CLAUDE_CODE_OAUTH_TOKEN")
# Home-relative secret locations denied to the file tools in the Synapse-owned settings.
_LOG = logging.getLogger(__name__)
_DENY_HOME_SUBDIRS = (
    ".ssh", ".gnupg", ".aws", ".kube", ".docker", ".claude", ".codex", ".config", ".netrc", ".local/share",
    ".password-store", ".npmrc", ".pypirc", ".git-credentials", ".mozilla", ".bash_history",
    ".config/gcloud", ".config/gh")
_RLIMIT_NOFILE = 4096
_RLIMIT_FSIZE = 1024 * 1024 * 1024
_NPROC_HEADROOM = 1024
_MAX_PROMPT_BYTES = 100_000  # below Linux MAX_ARG_STRLEN (128 KiB)


# Claude Code versions whose argv and stream-json output were checked against this adapter
# (see docs/operations/claude-cli-provider.md). Other versions are reported by probe() as
# "unverified_version"; that is advisory and does not block execution, and unknown stream
# shapes still fail closed in the parser.
VERIFIED_CLI_VERSIONS = ("2.1.329",)
_VERSION_RE = re.compile(r"^(\d{1,4}\.\d{1,4}\.\d{1,4})(?:\s|$)")
_VERSION_TIMEOUT_SECONDS = 5.0


def parse_cli_version(output: str) -> str | None:
    """Return "X.Y.Z" from `claude --version` output such as "2.1.329 (Claude Code)", else None."""
    m = _VERSION_RE.match(output.strip()[:64])
    return m.group(1) if m else None


_MAX_COUNT = 2**63 - 1  # SQLite INTEGER: a larger value raises OverflowError and would drop the whole record
_MAX_COST_USD = 1e12


def _count(value: object) -> int | None:
    """A reported token count, or None. A value SQLite cannot store is unreportable (not clamped)."""
    return value if type(value) is int and 0 <= value <= _MAX_COUNT else None


def _cost(value: object) -> float | None:
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= _MAX_COST_USD:
        return float(value)
    return None


KEEP_FINISHED = 64  # finished executions retained for usage()/cancel() after the stream ends


class ClaudeOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"  # process outcome not established: never retry blindly


@dataclass(frozen=True, slots=True)
class ClaudeCliProfile:
    """Strictly validated, immutable configuration. Disabled unless enabled=True."""

    executable: str
    allowed_tools: tuple[str, ...]
    state_dir: str
    enabled: bool = False
    permission_mode: str = "acceptEdits"
    allow_bypass_permissions: bool = False  # loud opt-in; never set casually
    # acceptEdits is documented to auto-approve file edits; whether it also auto-approves some Bash
    # filesystem commands (mkdir/touch/rm/mv/cp/sed) is UNVERIFIED here, so the conservative rule is that
    # acceptEdits never runs with any Bash tool unless the operator opts in explicitly (audited per run).
    allow_bash_with_accept_edits: bool = False
    api_key: str | None = field(default=None, repr=False)
    extra_args: tuple[str, ...] = ()
    credentials_file: str | None = None  # OAuth credentials copied into the scratch dir (0600)
    allow_repo_claude_config: bool = False  # waive the refusal for .claude/CLAUDE.md in the worktree
    memory_limit_bytes: int | None = None  # RLIMIT_AS; opt-in (JS runtimes reserve address space)
    max_timeout_seconds: float = 3600.0
    max_output_bytes_limit: int = 16 * 1024 * 1024
    max_line_bytes: int = 1024 * 1024
    term_grace_seconds: float = 5.0
    sandbox: bool = False  # opt-in bubblewrap launcher (ADR 019 C2); fail closed, never a fallback
    sandbox_bwrap: str | None = None  # absolute bwrap path override (tests); default: fixed system dirs
    git_repos: tuple[str, ...] = ()  # operator-allowlisted repos: the only source of the sandbox's read-only .git mount

    def __post_init__(self) -> None:
        if not isinstance(self.executable, str) or "\x00" in self.executable or not os.path.isabs(self.executable):
            raise ValueError("executable must be an absolute path")
        if not isinstance(self.state_dir, str) or "\x00" in self.state_dir or not os.path.isabs(self.state_dir):
            raise ValueError("state_dir must be an absolute path")
        tools = self.allowed_tools
        if not isinstance(tools, tuple) or not tools or len(tools) > 64:
            raise ValueError("allowed_tools must be a non-empty tuple of at most 64 entries")
        for tool in tools:
            if not isinstance(tool, str) or not _TOOL_RE.fullmatch(tool):
                raise ValueError("invalid allowed tool")
            if tool.split("(", 1)[0].strip().lower() in _SPECIAL_TOOL_NAMES:
                raise ValueError("invalid allowed tool: reserved --tools value")
        if self.permission_mode not in PERMISSION_MODES:
            raise ValueError("unsupported permission mode")
        if self.permission_mode == "bypassPermissions" and self.allow_bypass_permissions is not True:
            raise ValueError("bypassPermissions requires allow_bypass_permissions=True")
        if self.allow_bypass_permissions is not True and self.allow_bypass_permissions is not False:
            raise ValueError("allow_bypass_permissions must be a bool")
        if type(self.allow_bash_with_accept_edits) is not bool:
            raise ValueError("allow_bash_with_accept_edits must be a bool")
        if (self.permission_mode == "acceptEdits" and not self.allow_bash_with_accept_edits
                and any(t.split("(", 1)[0].lower() == "bash" for t in tools)):
            raise ValueError("acceptEdits must not be combined with Bash tools "
                             "(use dontAsk with an exact pattern, or set allow_bash_with_accept_edits=True)")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a bool")
        if self.api_key is not None and (not isinstance(self.api_key, str) or not self.api_key
                                         or "\x00" in self.api_key):
            raise ValueError("api_key must be a non-empty string when supplied")
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
        if not isinstance(self.git_repos, tuple) or any(
                not isinstance(r, str) or "\x00" in r or not os.path.isabs(r) for r in self.git_repos):
            raise ValueError("git_repos must be a tuple of absolute paths")
        if type(self.sandbox) is not bool:
            raise ValueError("sandbox must be a bool")
        if self.sandbox_bwrap is not None and (
                not isinstance(self.sandbox_bwrap, str) or "\x00" in self.sandbox_bwrap
                or not os.path.isabs(self.sandbox_bwrap)):
            raise ValueError("sandbox_bwrap must be an absolute path")
        if type(self.allow_repo_claude_config) is not bool:
            raise ValueError("allow_repo_claude_config must be a bool")
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
    argv: tuple[str, ...]  # fixed flags up to (not including) --settings
    tail: tuple[str, ...]  # --allowedTools ... (variadic, must come last)
    prompt: str  # delivered on stdin from a 0600 file, never argv
    cwd: str
    env: Mapping[str, str]
    timeout: float
    max_output: int
    cpu_seconds: int
    binds: tuple[tuple[str, bool], ...] = ()  # multi-repository attempt: (worktree, writable); cwd is the attempt dir


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
    journal_key: str | None = None
    returncode: int | None = None
    streaming: bool = False
    finished: bool = False
    finish_seq: int = 0  # order in which executions finished (pruning keeps the most recently finished)
    result_ok: bool | None = None
    usage: UsageObservation | None = None
    scratch: str | None = None  # per-run private HOME/CLAUDE_CONFIG_DIR root, removed after the run


def validate_worktree(path: str, state_dir: str) -> str:
    """Return the canonical worktree path or raise ValueError (fail closed)."""
    if not isinstance(path, str) or not path or "\x00" in path or not os.path.isabs(path):
        raise ValueError("worktree must be an absolute path")
    lexical = os.path.normpath(path)
    if lexical != path:
        raise ValueError("worktree path must be normalized")
    try:
        resolved = os.path.realpath(lexical, strict=True)
    except OSError as exc:
        raise ValueError("worktree does not exist") from exc
    if resolved != lexical:
        raise ValueError("worktree path must not traverse symlinks")
    st = os.stat(resolved)
    if not os.path.isdir(resolved) or st.st_uid != os.getuid():
        raise ValueError("worktree must be a directory owned by the current user")
    if resolved == os.sep:
        raise ValueError("worktree must not be the filesystem root")
    git = os.path.join(resolved, ".git")
    if os.path.islink(git) or not os.path.exists(git):
        raise ValueError("worktree must contain a real .git entry")
    state = os.path.realpath(state_dir)
    wt, sd = Path(resolved), Path(state)
    if wt == sd or sd in wt.parents or wt in sd.parents:
        raise ValueError("worktree must not overlap the state directory")
    return resolved


MAX_WORKSPACE_REPOS = 16


def validate_workspace(root: str, entries, state_dir: str) -> tuple[str, tuple[tuple[str, bool], ...]]:
    """A multi-repository attempt (ADR 026): ``root`` is the attempt directory (no .git of its own) and ``entries``
    lists ``{"path", "access"}`` per declared worktree, each a valid worktree that is a direct child of ``root``.
    Returns (root, ((path, writable), ...)) or raises ValueError (fail closed)."""
    if not isinstance(root, str) or not root or "\x00" in root or not os.path.isabs(root) or os.path.normpath(root) != root:
        raise ValueError("workspace root must be an absolute normalized path")
    try:
        resolved = os.path.realpath(root, strict=True)
    except OSError as exc:
        raise ValueError("workspace root does not exist") from exc
    st = os.lstat(root)
    if resolved != root or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or root == os.sep:
        raise ValueError("workspace root must be a symlink-free directory owned by the current user")
    state = Path(os.path.realpath(state_dir))
    if Path(root) == state or state in Path(root).parents or Path(root) in state.parents:
        raise ValueError("workspace root must not overlap the state directory")
    # ExecutionRequest freezes its input (lists become tuples, dicts read-only mappings).
    if not isinstance(entries, (list, tuple)) or not 1 <= len(entries) <= MAX_WORKSPACE_REPOS:
        raise ValueError("workspace must list 1 to %d worktrees" % MAX_WORKSPACE_REPOS)
    binds = []
    for e in entries:
        if not isinstance(e, Mapping) or set(e) != {"path", "access"} or e["access"] not in ("write", "read"):
            raise ValueError("workspace entries must be {path, access}")
        path = validate_worktree(e["path"], state_dir)
        if os.path.dirname(path) != root:
            raise ValueError("workspace worktrees must be direct children of the workspace root")
        binds.append((path, e["access"] == "write"))
    if len({b[0] for b in binds}) != len(binds):
        raise ValueError("workspace worktrees must be distinct")
    return root, tuple(binds)


class ClaudeCliProvider:
    """ProviderAdapter running the Claude Code CLI as a supervised subprocess.

    Request input keys: prompt (str), worktree (str), timeout_seconds (number,
    optional), max_output_bytes (int, optional), workspace (optional list of
    ``{path, access}``: a multi-repository attempt whose ``worktree`` is the attempt
    directory; the sandbox then mounts exactly those worktrees, read-access ones
    read-only). One execution is one process.
    """

    def __init__(self, profile: ClaudeCliProfile, *, environ: Mapping[str, str] | None = None,
                 spawn_journal: SpawnJournal | None = None) -> None:
        if not isinstance(profile, ClaudeCliProfile):
            raise TypeError("profile must be a ClaudeCliProfile")
        self._profile = profile
        self._environ = os.environ if environ is None else environ
        self._executions: dict[str, _Execution] = {}
        self._finish_counter = 0
        self._journal = spawn_journal  # durable pid record so a restart can fence orphans (ADR 023)

    # --- contract surface -------------------------------------------------
    async def probe(self, profile_id: str) -> ProviderHealth:
        """healthy (verified version), unverified_version (runs, version not verified) or unavailable."""
        validate_id(profile_id)
        p = self._profile
        ok = p.enabled and os.path.isfile(p.executable) and os.access(p.executable, os.X_OK)
        if ok and p.sandbox:
            try:
                await asyncio.to_thread(require_sandbox, p.sandbox_bwrap)
            except SandboxUnavailable:
                ok = False
        if not ok:
            return ProviderHealth(profile_id, "unavailable")
        version = await asyncio.to_thread(self._cli_version)
        if version is None:
            return ProviderHealth(profile_id, "unavailable")
        return ProviderHealth(profile_id, "healthy" if version in VERIFIED_CLI_VERSIONS else "unverified_version")

    def _cli_version(self) -> str | None:
        """Run `<executable> --version` with a scrubbed environment; None on any failure."""
        env = {k: v for k, v in self._environ.items() if k in ("PATH", "LANG")}
        try:
            done = subprocess.run([self._profile.executable, "--version"], stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=_VERSION_TIMEOUT_SECONDS, env=env,
                                  cwd="/", check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        return parse_cli_version(done.stdout[:256].decode("utf-8", "replace"))

    async def capabilities(self, profile_id: str) -> ProviderCapabilities:
        validate_id(profile_id)
        return ProviderCapabilities(True, True, True, False, False, True, False, None)

    async def usage(self, handle: str) -> UsageObservation | None:
        return self._execution(handle).usage

    async def start(self, request: ExecutionRequest) -> str:
        """Validate and plan an execution. The child is spawned when streamed."""
        if not self._profile.enabled:
            raise RuntimeError("Claude CLI provider is disabled")
        if not isinstance(request, ExecutionRequest):
            raise TypeError("ExecutionRequest required")
        plan = await asyncio.to_thread(self._plan, request)  # the repo-config scan walks the tree: off the event loop
        if self._profile.sandbox:  # fail closed: refuse the run, never run unsandboxed
            await asyncio.to_thread(require_sandbox, self._profile.sandbox_bwrap)
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
            p = self._profile
            if (p.permission_mode == "acceptEdits" and p.allow_bash_with_accept_edits
                    and any(t.split("(", 1)[0].lower() == "bash" for t in p.allowed_tools)):
                yield ProviderEvent("audit", "allow_bash_with_accept_edits: acceptEdits runs with Bash tools by operator opt-in")
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
        binds: tuple[tuple[str, bool], ...] = ()
        if data.get("workspace") is not None:
            cwd, binds = validate_workspace(data.get("worktree"), data["workspace"], p.state_dir)
            for tree, _ in binds:
                self._check_repo_config(tree)
        else:
            cwd = validate_worktree(data.get("worktree"), p.state_dir)
        self._check_repo_config(cwd)
        self._credential_source(cwd)  # refuse an unsafe credential source at start(), before any spawn
        # No positional prompt: `-p` reads it from stdin (verified, claude 2.1.329), so it never
        # appears in /proc/<pid>/cmdline. --setting-sources user + --settings: the repo's
        # .claude/settings*.json (hooks, env, apiKeyHelper, permissions) is never loaded.
        argv = (
            p.executable, "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", p.permission_mode, "--permission-prompts", "none",
            "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands",
            "--setting-sources", "user",
            # --allowedTools only PRE-APPROVES tools; it does not remove the others, and modes such as
            # acceptEdits auto-approve Write/Edit regardless (verified against claude 2.1.329: a Read-only
            # allowlist still wrote a file). --tools is what restricts the available built-in tool set.
            "--tools", ",".join(dict.fromkeys(t.split("(", 1)[0] for t in p.allowed_tools)),
            *self._disallowed_args(),
            *p.extra_args,
        )
        tail = ("--allowedTools", *p.allowed_tools)
        env = {k: self._environ[k] for k in _ENV_ALLOWLIST if k in self._environ}
        if p.api_key is not None:
            env["ANTHROPIC_API_KEY"] = p.api_key
        cpu = int(min(float(timeout) + p.term_grace_seconds + 60, 86_400 + 90))
        return _Plan(argv, tail, prompt, cwd, env, float(timeout), cap, cpu, binds)

    def _disallowed_args(self) -> tuple[str, ...]:
        """Second layer for read-only profiles: explicitly deny the mutating tools."""
        bases = {t.split("(", 1)[0].lower() for t in self._profile.allowed_tools}
        if bases.isdisjoint({m.lower() for m in _MUTATING_TOOLS}):
            return ("--disallowedTools", ",".join(_MUTATING_TOOLS))
        return ()

    def _check_repo_config(self, cwd: str) -> None:
        check_repo_config(cwd, "claude", self._profile.allow_repo_claude_config)  # list: repo_config_guard

    # --- per-run scratch dir (F3/F7) --------------------------------------
    def _make_scratch(self, ex: _Execution) -> tuple[str, str, str]:
        """Create <tmp>/synapse-agent-<uid>/<uuid>/{home,config,tmp} (0700), OUTSIDE the state dir so a
        sandbox can hide the state dir without hiding the child's HOME. Returns (root, home, config)."""
        base = os.path.join(os.path.realpath(tempfile.gettempdir()), f"synapse-agent-{os.getuid()}")
        state = os.path.realpath(self._profile.state_dir)
        if base == state or base.startswith(state + os.sep) or state.startswith(base + os.sep):
            raise OSError("scratch base must be outside the state directory")
        try:
            os.mkdir(base, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(base)  # shared sticky tmp: refuse a pre-planted symlink or foreign dir
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("scratch base is not a directory owned by the current user")
        os.chmod(base, 0o700)
        root = os.path.join(base, uuid4().hex)
        os.mkdir(root, 0o700)
        ex.scratch = root
        write_marker(root)  # credential_sweep: lets a startup sweep reclaim this after SIGKILL
        home, config, tmp = (os.path.join(root, n) for n in ("home", "config", "tmp"))
        for d in (home, config, tmp):
            os.mkdir(d, 0o700)
        self._place_credentials(config, ex.plan.cwd)
        self._write_private(os.path.join(root, "settings.json"),
                            json.dumps(self._settings(root), sort_keys=True).encode())
        self._write_private(os.path.join(root, "prompt.txt"), ex.plan.prompt.encode("utf-8"))
        return root, home, config

    @staticmethod
    def _write_private(path: str, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

    def _credential_source(self, cwd: str | None = None) -> str | None:
        """Resolve and vet the credential source (None when there is none). Raises ValueError for an
        unsafe one: a symlink, a non-regular file, group/other-accessible, owned by another user, too
        large, or located inside the worktree or the state directory (agent-readable locations)."""
        p = self._profile
        if p.api_key is not None:
            return None
        src = p.credentials_file
        if src is None:
            cfg = self._environ.get("CLAUDE_CONFIG_DIR")
            home = self._environ.get("HOME")
            candidates = [os.path.join(cfg, ".credentials.json")] if cfg else []
            if home:
                candidates.append(os.path.join(home, ".claude", ".credentials.json"))
            src = next((c for c in candidates if os.path.lexists(c)), None)
        if src is None or not os.path.lexists(src):
            return None  # CLI will report an auth failure; CLAUDE_CODE_OAUTH_TOKEN may be in env
        if cwd is not None:
            for base in (cwd, p.state_dir):
                real = os.path.realpath(base)
                if os.path.commonpath([os.path.realpath(os.path.dirname(src)), real]) == real:
                    raise ValueError("credentials source must not be inside the worktree or state directory")
        self._read_credentials(src)  # vets mode/type/owner/size; the bytes are discarded
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
            data = os.read(fd, (1 << 20) + 1)
        finally:
            os.close(fd)
        if len(data) > 1 << 20:
            raise ValueError("credentials source is too large")
        return data

    def _place_credentials(self, config: str, cwd: str | None = None) -> None:
        src = self._credential_source(cwd)
        if src is None:
            return
        self._write_private(os.path.join(config, ".credentials.json"), self._read_credentials(src))

    def _settings(self, root: str) -> dict:
        """Synapse-owned settings: no hooks, secret locations denied to the file tools."""
        real_home = self._environ.get("HOME")
        cfg = self._environ.get("CLAUDE_CONFIG_DIR")
        paths = [os.path.realpath(self._profile.state_dir), root, os.path.realpath(root)]
        if real_home:
            paths += [os.path.join(real_home, sub) for sub in _DENY_HOME_SUBDIRS]
        if cfg:
            paths.append(cfg)
        deny: list[str] = []
        for path in dict.fromkeys(paths):
            if not os.path.isabs(path) or re.search(r"[()\x00-\x1f*?\[\]]", path):
                continue
            for tool in ("Read", "Edit"):
                deny.append(f"{tool}(/{path}/**)")  # `//abs/path` is an absolute path rule
        return {"disableAllHooks": True, "permissions": {"deny": deny}}

    @staticmethod
    def _cleanup(ex: _Execution) -> None:
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
                for tree, _ in plan.binds:
                    self._check_repo_config(tree)
                root, home, config = self._make_scratch(ex)
                env = {**plan.env, "HOME": home, "CLAUDE_CONFIG_DIR": config, "TMPDIR": os.path.join(root, "tmp")}
                argv = [*plan.argv, "--settings", os.path.join(root, "settings.json"), *plan.tail]
                if self._profile.sandbox:
                    argv = self._sandboxed(argv, plan, root)
                if self._journal is not None:
                    ex.journal_key = uuid.uuid4().hex
                    self._journal.begin(ex.journal_key)  # intent first: cannot journal means cannot spawn
                with open(os.path.join(root, "prompt.txt"), "rb") as stdin:
                    process = subprocess.Popen(
                        argv, cwd=plan.cwd, env=env, shell=False,
                        stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        close_fds=True, start_new_session=True,
                        preexec_fn=self._preexec(plan.cpu_seconds),
                    )
            except (OSError, ValueError, subprocess.SubprocessError, SandboxUnavailable) as exc:
                # The run only reports "spawn_failed"; say why in the daemon log. SandboxUnavailable messages are
                # fixed strings from this package; for anything else log only the type and errno, never paths or text.
                _LOG.warning("agent spawn failed: %s", str(exc) if isinstance(exc, SandboxUnavailable)
                             else "%s%s" % (type(exc).__name__, " errno=%s" % exc.errno if isinstance(exc, OSError) and exc.errno else ""))
                self._cleanup(ex)
                self._journal_finish(ex)  # no process was created
                ex.spawn_failed = True
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

    def _sandboxed(self, argv: list[str], plan: _Plan, root: str) -> list[str]:
        """Wrap the child argv in bwrap (raises SandboxUnavailable, which fails the spawn closed)."""
        p = self._profile
        bwrap = p.sandbox_bwrap or find_bwrap()
        if not bwrap:
            raise SandboxUnavailable("sandbox requested but bwrap is not installed")
        exe = os.path.realpath(argv[0])
        if not os.path.isfile(exe):
            raise SandboxUnavailable("sandbox executable must be an existing regular file")
        homes = sandbox_homes(self._environ.get("HOME"))
        forbidden = [os.path.realpath(p.state_dir), *forbidden_home_paths(homes, _DENY_HOME_SUBDIRS)]
        binds = tuple(getattr(plan, "binds", ()))
        if binds:
            # One read-only common .git per declared worktree, each derived from the operator allowlist.
            ro: list[str] = []
            for tree, _ in binds:
                ro += [d for d in git_readonly_dirs(tree, p.git_repos) if d not in ro]
        else:
            ro = git_readonly_dirs(plan.cwd, p.git_repos)
        return build_bwrap_argv(
            bwrap, worktree=plan.cwd, scratch=os.path.realpath(root), executable=exe,
            inner=[exe, *argv[1:]], forbidden=forbidden,
            extra_ro_dirs=ro, no_ancestor=homes, worktree_binds=binds)

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
        if kind == "assistant":
            message = msg.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                raise ValueError("malformed assistant message")
            out: list[ProviderEvent] = []
            for block in content:
                if not isinstance(block, dict):
                    raise ValueError("malformed content block")
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    out.append(ProviderEvent("output", block["text"]))
                elif block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                    out.append(ProviderEvent("tool_use", block["name"][:128]))  # name only, never inputs
            return out
        if kind == "result":
            if ex.result_ok is not None:
                raise ValueError("duplicate result")
            ex.result_ok = msg.get("is_error") is False and msg.get("subtype") == "success"
            usage = msg.get("usage")
            if isinstance(usage, dict):
                # Strict: only non-negative ints / finite non-negative floats count; anything else is
                # unknown (None), never 0. A result with nothing usable records no observation at all.
                fields = (_count(usage.get("input_tokens")), _count(usage.get("output_tokens")),
                          _count(usage.get("cache_read_input_tokens")), _count(usage.get("cache_creation_input_tokens")),
                          _cost(msg.get("total_cost_usd")))
                if any(v is not None for v in fields):
                    ex.usage = UsageObservation(fields[0], fields[1], "claude-cli", fields[2], fields[3], fields[4])
        return []  # system/user/stream events and unknown future types are ignored

    # --- outcome -----------------------------------------------------------
    def _outcome(self, ex: _Execution) -> tuple[str, str | None]:
        if ex.spawn_failed:
            return ClaudeOutcome.FAILED.value, "spawn_failed"
        if ex.reason == "cancelled" and ex.process is None:
            return ClaudeOutcome.CANCELLED.value, None
        if ex.stop_status is ProcessStatus.UNKNOWN or not ex.reaped:
            return ClaudeOutcome.UNKNOWN.value, "process_outcome_unestablished"
        if ex.reason == "cancelled":
            return ClaudeOutcome.CANCELLED.value, None
        if ex.reason == "timed_out":
            return ClaudeOutcome.TIMED_OUT.value, None
        if ex.reason in ("output_limit", "protocol_error"):
            return ClaudeOutcome.FAILED.value, ex.reason
        if ex.returncode == 0 and ex.result_ok is True:
            return ClaudeOutcome.SUCCEEDED.value, None
        if ex.returncode == 0 and ex.result_ok is None:
            return ClaudeOutcome.UNKNOWN.value, "no_result_event"
        return ClaudeOutcome.FAILED.value, "exit_nonzero" if ex.returncode else "result_error"

    def _execution(self, handle: str) -> _Execution:
        validate_id(handle)
        try:
            return self._executions[handle]
        except KeyError as exc:
            raise ValueError("Unknown Claude CLI execution") from exc
