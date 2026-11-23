"""Single source of truth for agent-controlling files a repository can carry.

A worktree is a checkout of an untrusted repository. Several agent CLIs load instructions or
configuration from it (hooks, permissions, MCP servers, standing directives). Both adapters
call :func:`check_repo_config`; the list lives only here.

Decision per entry ("hard" refuses the run for the named adapter, "warn" only logs):

* The adapter's own config (``.claude``/``CLAUDE*.md`` for Claude, ``.codex``/``AGENTS*.md`` for
  Codex) and ``.mcp.json`` are hard for the adapter that loads them (hooks, env, MCP, directives).
* ``AGENTS.md`` and ``.agents/`` are read by Codex as instructions/skills: hard for Codex. Claude
  Code does not load them (it reads CLAUDE.md), so they are warn-only there: the agent may still
  read the file as ordinary content, which is a prompt-injection surface but not a config load.
* Other tools' files (.cursor, .cursorrules, .windsurfrules, .aider*, .continue, .gemini, GEMINI.md,
  .clinerules, copilot-instructions, ...) are not loaded by either adapter: warn only.

Matching is case-insensitive (case-insensitive filesystems resolve the variants), counts symlinks
(including dangling ones) as present, never follows symlinked directories, and never looks inside
``.git``. ``anywhere`` entries match at any depth (Claude and Codex both load nested instruction
files); the others only at the worktree root. The scan is bounded and fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os

_log = logging.getLogger(__name__)


class RepoConfigRefused(ValueError):
    """The worktree is refused: repo-controlled config, or it could not be scanned completely."""

CLAUDE = "claude"
CODEX = "codex"
ADAPTERS = frozenset({CLAUDE, CODEX})

MAX_ENTRIES = 200_000
MAX_DEPTH = 12


@dataclass(frozen=True)
class RepoConfigEntry:
    pattern: str  # lowercase; relative path ("a/b") or name; trailing "*" = prefix match on the name
    hard_for: frozenset[str]  # adapters that refuse the run; every other adapter only warns
    anywhere: bool = False  # match at any depth, not only the worktree root
    why: str = ""


def _e(pattern: str, hard: tuple[str, ...] = (), anywhere: bool = False, why: str = "") -> RepoConfigEntry:
    return RepoConfigEntry(pattern.lower(), frozenset(hard), anywhere, why)


REPO_CONFIG_ENTRIES: tuple[RepoConfigEntry, ...] = (
    _e(".claude", (CLAUDE,), True, "Claude settings, hooks, commands, skills"),
    _e("CLAUDE.md", (CLAUDE,), True, "Claude standing instructions"),
    _e("CLAUDE.local.md", (CLAUDE,), True, "Claude standing instructions"),
    _e(".mcp.json", (CLAUDE, CODEX), False, "project MCP servers (arbitrary commands)"),
    _e(".codex", (CODEX,), True, "Codex config, hooks, prompts"),
    _e("AGENTS.md", (CODEX,), True, "Codex reads as instructions; notable for Claude"),
    _e("AGENTS.override.md", (CODEX,), True, "Codex override instructions"),
    _e(".agents", (CODEX,), True, "Codex skills directory; notable for Claude"),
    _e(".cursor", (), False, "Cursor rules/MCP"),
    _e(".cursorrules", (), False, "Cursor rules"),
    _e(".windsurf", (), False, "Windsurf rules"),
    _e(".windsurfrules", (), False, "Windsurf rules"),
    _e(".aider*", (), False, "Aider config/conventions"),
    _e(".continue", (), False, "Continue config"),
    _e(".gemini", (), False, "Gemini CLI settings"),
    _e("GEMINI.md", (), True, "Gemini CLI instructions"),
    _e(".clinerules", (), False, "Cline rules (file or dir)"),
    _e(".roo", (), False, "Roo config"),
    _e(".roorules", (), False, "Roo rules"),
    _e(".kiro", (), False, "Kiro steering"),
    _e(".github/copilot-instructions.md", (), False, "Copilot instructions"),
    _e(".github/instructions", (), False, "Copilot path instructions"),
    _e(".github/agents", (), False, "Copilot agents"),
)


@dataclass(frozen=True)
class RepoConfigHit:
    path: str  # relative, as found on disk
    entry: RepoConfigEntry
    hard: bool


def _matches(entry: RepoConfigEntry, name_lower: str) -> bool:
    if entry.pattern.endswith("*"):
        return name_lower.startswith(entry.pattern[:-1])
    return name_lower == entry.pattern


def scan_repo_config(cwd: str, adapter: str) -> list[RepoConfigHit]:
    """Return every hit under ``cwd``. Raises ``ValueError`` if the tree is too large to scan."""
    if adapter not in ADAPTERS:
        raise ValueError(f"unknown adapter {adapter!r}")
    hits: list[RepoConfigHit] = []
    seen = 0
    stack: list[tuple[str, str, int]] = [(cwd, "", 0)]
    while stack:
        directory, rel_dir, depth = stack.pop()
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError as exc:
            raise RepoConfigRefused(f"worktree directory {rel_dir or '.'} is unreadable: cannot verify it holds no repo-controlled agent config") from exc
        for de in entries:
            seen += 1
            if seen > MAX_ENTRIES:
                raise RepoConfigRefused("worktree too large to scan for repo-controlled agent config")
            name = de.name
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if name.lower() == ".git":
                continue  # git internals are never agent config; not scanned
            for entry in REPO_CONFIG_ENTRIES:
                if "/" in entry.pattern:
                    found = rel.lower() == entry.pattern
                else:
                    found = (depth == 0 or entry.anywhere) and _matches(entry, name.lower())
                if found:
                    hits.append(RepoConfigHit(rel, entry, adapter in entry.hard_for))
                    break
            try:
                is_real_dir = de.is_dir(follow_symlinks=False)
            except OSError:
                is_real_dir = False
            if is_real_dir:
                if depth >= MAX_DEPTH:
                    raise RepoConfigRefused(f"worktree nested deeper than {MAX_DEPTH} levels at {rel}: cannot verify it holds no repo-controlled agent config")
                stack.append((de.path, rel, depth + 1))
    return hits


def check_repo_config(cwd: str, adapter: str, allow: bool = False) -> None:
    """Refuse (``ValueError``) if a hard entry for ``adapter`` is present; log warn-only ones.

    ``allow`` waives the refusal only; warnings are still logged.
    """
    hits = scan_repo_config(cwd, adapter)
    for h in hits:
        if not h.hard:
            _log.warning("worktree contains %s (%s): not loaded by the %s adapter", h.path, h.entry.why, adapter)
    if allow:
        return
    for h in sorted(hits, key=lambda h: h.path):
        if h.hard:
            raise RepoConfigRefused(f"worktree contains {h.path}: repo-controlled {adapter.capitalize()} config is refused")
