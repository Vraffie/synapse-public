# Codex CLI provider adapter (disabled by default; runs only for tasks that name a Codex profile)

Codex is optional and off by default: Synapse never needs the `codex` binary to start or to run Claude tasks, it is only used for a task that names a Codex profile, and a Codex profile is refused when the daemon sandbox is on (the `synapse_up.py` default), so a sandboxed Claude-only setup never sees it.

`synapse.providers.codex_cli.CodexCliProvider` runs `codex exec --json` as a supervised subprocess in a
caller-supplied git worktree. It implements the `ProviderAdapter` protocol and mirrors the hardening of the
[Claude adapter](claude-cli-provider.md). Since ADR 016 decision 4 it is wired into agent runs through a `codex`
entry in `--agent-config` ([agent profiles](agent-profiles.md#codex-profiles)): `AgentProviderRouter` builds a
`CodexCliProvider` for a task that **names** an enabled Codex profile, and nothing else. Codex is never the default
profile or a fallback (registry validation refuses both), there is no Claude<->Codex fallback, and a spawn refusal or
an `unknown` outcome is never retried on another profile. No real Codex run has been made through the daemon path
(see Still unverified).

## How agent runs use it

**Codex runs unsandboxed by Synapse.** Synapse does not wrap the Codex child in bwrap; the only confinement is Codex's own `-s read-only` / `-s workspace-write`, and `read-only` does not confine reads. A Codex child can therefore read anything the daemon's user can: `approver.token` and `operator.token` in the state dir (so it can approve its own gated requests), the SQLite database, `agentd.sock`, and the user's other credentials. For that reason a codex profile is **refused** (startup error, and `codex_ineligible`/422 as backstops) whenever the `--claude-cli-config` base has `"sandbox": true`: it would silently get less isolation than every Claude child. Codex is only available with the base sandbox off and `--allow-same-user-agents` acknowledged; the startup warning and the `agent_runtime.same_user_acknowledged` audit row then record `codex_unsandboxed: true`.

| task field | Codex |
| --- | --- |
| `permission_mode` `plan` | `-s read-only` |
| `permission_mode` `acceptEdits` | `-s workspace-write` (plus the fixed tightening below) when `Edit` or `Write` is requested, else `read-only` |
| `permission_mode` `dontAsk`, `manual` | refused: 422 at validation, `codex_ineligible` at spawn |
| `allowed_tools` | must be a subset of `Read`, `Glob`, `Grep`, `Edit`, `Write`; anything else (any `Bash(...)`) is refused. It selects the sandbox only and is **not a tool restriction**: inside the sandbox the agent can run any shell command |
| `Edit`/`Write` with `plan` | refused |

The router builds `CodexCliProfile(executable=<registry entry>, state_dir=<daemon state dir>, enabled=True,
sandbox=<derived>, extra_args=(), max_timeout_seconds=<entry>)` plus the base profile's `memory_limit_bytes`,
`max_output_bytes_limit`, `max_line_bytes` and `term_grace_seconds` (the same rlimits as a Claude child): no
`credentials_file` (the default lookup below
applies), no model (`agent.profile_selected` records `model: null`; the `--model=` allowlist entry is not used by the
daemon), and the daemon's spawn journal, exactly like Claude spawns. Providers are cached per
`(profile_id, allowed_tools, permission_mode)`. Recovery reconciles Codex attempts like Claude ones
(`recover_mock_run(provider_kind={"claude","codex"})`). `AgentRuntime.probe_profile(id)` runs only `codex --version`
(no agent, no worktree, no credential copy) and returns `healthy`, `unverified_version` or `unavailable`; it has no
API surface yet. The version child runs in its own session with a scrubbed environment and stderr discarded; its
stdout is read from a non-blocking descriptor with `poll` (no 1024-descriptor limit, unlike `select`) in the calling
thread against one 10 s deadline, capped at 4 KiB, and
once the child has exited only what is already in the pipe is read, so a descendant that escaped with `setsid` and
keeps the pipe open cannot delay the result. The whole call returns within about 10 s; the child's process group is
then killed and the child reaped with a bounded wait (SIGKILL to the group, then to the child, at most 2 s each, then
an error is logged instead of hanging); a `setsid` descendant is outside that group and is not killed. The probe child
gets the same rlimits as an agent spawn (CPU 11 s, NOFILE, FSIZE, no core, NPROC, optional address space).

**Version gate at spawn.** Before every Codex spawn the router checks `codex --version` (run off the event loop and cached per executable realpath, device, inode, size, mtime and ctime, at most 16 entries; a successful result is kept while that identity holds, a failed one for 30 s, so a transient failure is retried. The key is the configured executable: if it is a wrapper script, a change to what the wrapper execs is not noticed until the wrapper itself changes). The `/tmp`/`$TMPDIR` exclusion of `workspace-write` relies on `-c sandbox_workspace_write.*` keys verified only on `VERIFIED_CLI_VERSIONS` (`0.170.0`), and a newer Codex could silently ignore a renamed key, so a task that would run `workspace-write` on any other (or unparseable) version is refused before spawning with detail `codex_ineligible`. A `read-only` task still runs on an unverified version, and each such spawn logs a warning naming the task, the profile and the parsed version (digits and dots only, otherwise `unknown`). Updating Codex therefore disables Codex edits until the new version is verified and added to `VERIFIED_CLI_VERSIONS`. **Probe failure** (the binary is missing, exits non-zero, prints no parseable `codex-cli X.Y.Z`, times out after 10 s, or the check itself errors) counts as *no version*: exactly like an unverified version, a `workspace-write` task is refused with `codex_ineligible` and nothing is spawned, while a `read-only` task still runs and its spawn logs the version as `unknown`. A probe failure never fails a task as `start_rejected` by itself and never affects Claude tasks.

Executable checks: like the Claude base executable, the codex `executable` must be an absolute, normalized path and
(when enabled) an existing executable file; neither adapter refuses a symlink or a group/world-writable binary.
Keep the binary owned by the daemon's user or root and not writable by others.

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
unless the stop status was `unknown`. Entries carry no prompt, argv, path or credential. The daemon passes its journal to
every Codex provider it builds; covered by `tests/test_codex_spawn_journal.py` and, through the engine,
`tests/test_codex_agent_runs.py`.

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
returns `healthy` for a verified version, `unverified_version` for another parseable one, `unavailable` otherwise.
On its own it does not block execution; through the daemon, the spawn-time version gate above refuses
`workspace-write` on any unverified version.

Recorded event fixtures (scrubbed of paths): `tests/fixtures/codex_cli_0_170_0/` (`read_only_command`,
`read_only_write_refused`, `turn_failed_bad_model`, `workspace_write_command`). The fake-executable tests replay them.

Test isolation: `tests/conftest.py` points `CODEX_HOME`/`CLAUDE_CONFIG_DIR` at fake credential files and guards both
adapters' credential readers, so a fake-executable test that reaches the real `~/.codex` or `~/.claude` login (even
through an explicit environment naming the real `HOME`) is refused before the file is opened and fails. Only tests
marked `real_cli("SYNAPSE_REAL_CODEX")`/`real_cli("SYNAPSE_REAL_CLAUDE")` see the real environment when their flag is
`1`; run them in a separate invocation (`SYNAPSE_REAL_CODEX=1 pytest tests/test_codex_cli_real.py`).

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
- Engine integration is covered **only with the fake executable** (`tests/test_codex_agent_runs.py`: lifecycle, sandbox
  derivation, ineligible tools/modes, disabled profile, spawn refusal without fallback, unknown outcome, recovery,
  journal, probe). No real `codex` was run through the daemon, so a real daemon run (and ADR 016 gate 1, the
  independent review, and the opt-in `SYNAPSE_REAL_CODEX=1` run through the engine) is still outstanding.

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
