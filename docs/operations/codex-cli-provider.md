# Codex CLI provider adapter (disabled by default, not wired)

`synapse.providers.codex_cli.CodexCliProvider` runs `codex exec --json` as a supervised subprocess in a
caller-supplied git worktree. It implements the `ProviderAdapter` protocol and mirrors the hardening of the
[Claude adapter](claude-cli-provider.md). It is **not** wired into the engine, the API, `agent_config` or
`AgentRuntime`; wiring (profile registry, routing) is separate work (ADR 015, B3).

## Profile

`CodexCliProfile` is frozen and strictly validated; `enabled` defaults to `False`.

| field | rule |
| --- | --- |
| `executable`, `state_dir` | absolute paths |
| `sandbox` | `read-only` (default) or `workspace-write`. `danger-full-access` is rejected |
| `extra_args` | allowlist, `--model=NAME` only (one argv element, at most once). `-c/--config`, `-s`, `-C`, `--add-dir`, `--dangerously-*`, `--approve-for-me`, `--oss`, `-p`, `--enable/--disable`, `-i`, `-o`, `--output-schema`, `--skip-git-repo-check` and every other flag are refused |
| `credentials_file` | optional absolute path to an `auth.json`; default `$CODEX_HOME/auth.json`, then `~/.codex/auth.json` |
| `allow_repo_codex_config` | default `False`: a worktree containing a hard entry of the repo-config list (`.codex`, `AGENTS.md`, `AGENTS.override.md`, `.agents/` at any depth, or a root `.mcp.json`) is refused (checked at `start()` and again at spawn) |
| limits | `max_timeout_seconds`, `max_output_bytes_limit`, `max_line_bytes`, `term_grace_seconds`, optional `memory_limit_bytes` (same ranges as the Claude profile) |

## What one run does

argv: `codex exec --json -s <sandbox> [tightening] --ephemeral --ignore-user-config --ignore-rules -C <worktree>
[--model=X] -`. The prompt goes to stdin from a `0600` file (never argv). The child gets its own session and
process group, rlimits (CPU, NOFILE, FSIZE, CORE, NPROC), `shell=False` and an environment of only `PATH`, `LANG`
plus a per-run scratch `HOME`, `CODEX_HOME` and `TMPDIR` under `<state_dir>/codex-run/<uuid>/` (`0700`). Nothing
from the daemon's environment (OpenAI/Codex variables, operator tokens, ...) is passed through. The scratch dir,
including the credential copy, is removed after the run. Output is read line by line with a per-line and a total
cap; cancel and timeout send TERM to the group, then KILL after the grace period; leftover descendants are killed.

Given a `spawn_journal` (ADR 023), the adapter journals each spawn exactly like the Claude adapter: intent before
`Popen` (if it cannot be written, nothing is spawned), pid, start ticks and boot id right after, removed once reaped
unless the stop status was `unknown`. Entries carry no prompt, argv, path or credential. Because the adapter is not
wired into the daemon, no daemon run passes a journal to it today; the behaviour is covered by
`tests/test_codex_spawn_journal.py`.

Event mapping (schema observed on 0.170.0, see Verified): `item.completed` of an `agent_message` -> `output`;
`item.started` of any non-message item -> `tool_use` with **the item type only** (for example
`command_execution`; never the command line, output or paths); `turn.completed` -> success and usage
(`input_tokens`, `output_tokens`, source `codex-cli`); `turn.failed` -> failure. Unknown event types and the
non-fatal `error` item are ignored. A malformed or non-object line, a duplicate terminal event, or an oversized
line fails the run closed (`protocol_error` / `output_limit`). Exit 0 without a terminal turn event is
`unknown` (`no_turn_completed`), never success.

### Credentials

Codex's file credential backend stores a ChatGPT login in `~/.codex/auth.json`. The adapter copies that file
(max 1 MiB) into the scratch `CODEX_HOME` at `0600` and never writes anything back. Codex was run only with that
copy; the daemon's own `~/.codex` is never used as `CODEX_HOME`. If Codex rewrites the scratch copy during a run
(for example a token refresh), the adapter logs a warning (never contents) and the refreshed tokens are
discarded with the scratch dir; whether the provider rotates refresh tokens such that the source login would then
need `codex login` is **unknown**. API-key login (`codex login --with-api-key`) and `--with-access-token` were not
tried and are not supported by this adapter.

Credential source vetting (A2): `start()` refuses (ValueError, nothing spawned) a source that is a symlink, not a
regular file, larger than 1 MiB, owned by another user, group/other-accessible (mode must be `0600` or stricter),
or located inside the worktree or `state_dir` (`credentials_file` inside `state_dir` is also rejected at profile
construction). The file is opened with `O_NOFOLLOW` and vetted via `fstat` on the same descriptor. Only that one
file is copied; the scratch `HOME` and `TMPDIR` start empty. Scratch removal runs on every spawn-failure path.

Residual exposure (not fixable inside the adapter): Codex's `read-only` sandbox may still permit reading the
whole filesystem, so the agent can read any same-user file by absolute path (the real `~/.codex`, ssh keys, the
daemon's `/proc/<pid>/environ`). The scratch HOME and scrubbed env only stop discovery through `HOME`/env, not a
hostile agent that guesses paths; the same-user trust domain applies. The scratch `auth.json` is necessarily readable
by the agent for the run's duration.

### Sandbox: what it does and does not restrict

Codex has no tool allowlist; the sandbox is the only capability restriction, and inside it the agent can run any
shell command. Network access is **not** disabled by the adapter for `read-only`.

`workspace-write` is **not confined to the worktree by default**. Observed on 0.170.0: a `workspace-write` run
created a file in a sibling directory under `/tmp`, because that sandbox also allows `/tmp` and `$TMPDIR`. The
adapter therefore adds fixed overrides for `workspace-write` only: `sandbox_workspace_write.exclude_slash_tmp=true`,
`sandbox_workspace_write.exclude_tmpdir_env_var=true` and `sandbox_workspace_write.network_access=false`
(callers still cannot pass `-c`). With them the same real test refused both a `/tmp` path and a path under `$HOME`.
The effect of `network_access=false` was **not** checked.

## Verified against codex-cli 0.170.0 (2026-11-20, Linux)

`VERIFIED_CLI_VERSIONS = ("0.170.0",)`. `probe()` runs `codex --version` (scrubbed env, no stdin, 10 s timeout) and
returns `healthy` for a verified version, `unverified_version` for another parseable one, `unavailable` otherwise;
it is advisory and does not block execution.

Recorded event fixtures (scrubbed of paths): `tests/fixtures/codex_cli_0_170_0/` (`read_only_command`,
`read_only_write_refused`, `turn_failed_bad_model`, `workspace_write_command`). The fake-executable tests replay them.

`tests/test_codex_cli_real.py` (opt-in `SYNAPSE_REAL_CODEX=1`) passed 7/7:

- a `read-only` run reads a file, succeeds, reports a `command_execution` tool use and leaves no process behind;
- a `read-only` run asked to create a file **cannot** (the file does not exist; the exit is still 0 and no error
  event is emitted, so success does not imply the write happened);
- a `workspace-write` run creates a file in its worktree and **cannot** create files under `/tmp` or `$HOME`
  (with the tightening above; without it the `/tmp` write succeeded);
- cancel after 1 s -> `cancelled`, and a 1 s timeout -> `timed_out`, both with no leftover process in the worktree;
- a worktree with `.codex` or `AGENTS.md` is refused by default; with the opt-in, a `.codex/config.toml` naming a
  nonexistent model and an `AGENTS.md` ordering the reply "PWNED" changed nothing (observed; not shown to be
  guaranteed, and the project may simply have been untrusted);
- the login tokens and account id from `auth.json` do not appear in any event or in the usage observation, and the
  scratch credential copy is gone after the run.

Process facts seen in manual runs: success is exit 0 with `turn.completed`; a failed turn is exit 1 with
`turn.failed` (a model unsupported by the account); a bad `-C` directory exits 1 with an `Error:` line on stderr and
no events; stderr is separate from the JSONL on stdout (the adapter discards stderr); a scratch `CODEX_HOME` under
`/tmp` makes Codex print a harmless PATH-alias warning on stderr (the scratch lives under `state_dir`).

## Still unverified

- Other Codex versions, other login methods (API key, access token, keyring backend), and token-refresh behaviour
  of the copied `auth.json`.
- Whether `read-only`/`workspace-write` can reach the network (`network_access=false` is set but its effect was
  not tested), and whether the sandbox holds against anything beyond the file writes above (no hostile-prompt or
  escape testing was done).
- Event types not seen: file-change, MCP, web-search and multi-turn items (they are ignored or surface as a
  `tool_use` item type; their JSON shapes are unverified), `resume`, `--output-schema`, `-o`, reasoning items.
- Usage beyond `input_tokens`/`output_tokens` (the stream also carries `cached_input_tokens`,
  `cache_write_input_tokens` and `reasoning_output_tokens`; not recorded by `UsageObservation`), quota and rate-limit
  signals (none were observed), and cost.
- Behaviour with a trusted project (the repo-config observation above is for an untrusted/fresh scratch setup),
  and resource-limit behaviour under `memory_limit_bytes`.
- Engine integration: not wired, so no approval-gate, worktree, scheduling or desktop behaviour is claimed.

## Repo-config refusal list (security review F3)

One data module, `src/synapse/providers/repo_config_guard.py`, is imported by both adapters. Each
entry is "hard" (refuse the run) for the adapter that loads it and "warn" (log only) otherwise.
Matching is case-insensitive, counts symlinks (also dangling), does not follow symlinked
directories, skips `.git`, and fails closed if the tree has more than 200,000 entries.

| Entry | Claude | Codex | Why |
|---|---|---|---|
| `.claude/`, `CLAUDE.md`, `CLAUDE.local.md` (any depth) | hard | warn | Claude settings, hooks, skills, standing instructions |
| `.codex/`, `AGENTS.override.md` (any depth) | warn | hard | Codex config, hooks, override instructions |
| `AGENTS.md` (any depth) | warn | hard | Codex follows its directives; Claude does not load it, the agent can only read it as content |
| `.agents/` (any depth) | warn | hard | Codex skills location |
| `.mcp.json` (root) | hard | hard | project MCP servers run arbitrary commands |
| `.cursor/`, `.cursorrules`, `.windsurf/`, `.windsurfrules`, `.aider*`, `.continue/`, `.gemini/`, `GEMINI.md`, `.clinerules`, `.roo/`, `.roorules`, `.kiro/`, `.github/copilot-instructions.md`, `.github/instructions/`, `.github/agents/` | warn | warn | Neither adapter loads them; prompt-injection surface only if the agent reads them |

`allow_repo_*_config` waives the refusal only; warnings are still logged. Unverified: the list
follows documented tool behaviour; no real CLI was run.

## Repo-config guard: operational effects

The guard fails closed on scan failures, not only on hits: a worktree nested deeper than 12 levels, or with a
subdirectory it cannot read, is refused at `start()` and again at spawn (previously such directories were silently
skipped, which was a bypass). There is no per-repo override: `allow_repo_codex_config` waives hits, not scan
failures. A repository with a very deep `node_modules` or build tree must keep those trees out of the worktree.
The refusal reason shown in the failure event is escaped and capped at 200 characters because it can contain a
repo-controlled relative path. The profile `model` alias no longer accepts `:` (for example `opus:4`).
