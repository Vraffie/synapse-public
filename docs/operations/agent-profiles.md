# Agent profiles (`--agent-config`)

ADR 016. The file declares named profiles; a run picks one with `profile` on `POST /api/v1/agent-runs` (it
applies to every task of the run unless a task names its own), and a claude profile can point at its own Claude
config directory so each profile runs as a different Claude account. See
[Three Claude accounts, one per run](#three-claude-accounts-one-per-run) for a worked setup.

## Backward compatibility

Nothing changes without `--agent-config`. The `--claude-cli-config` profile is the implicit profile
**`default`** (the registry primary). Requests that name no profile, or `default`, are validated exactly as
before. Existing receipts, specs and the built-in `synapse.builtin.agent.claude.v1` row are untouched.

## Format

`--agent-config PATH` requires `--claude-cli-config` (startup fails otherwise). JSON, like the base file, with
the same file checks: a user-owned regular file, not a symlink, **not group/world writable**, at most 64 KiB,
no duplicate keys, no `NaN`/`Infinity`. The file holds narrower views of the base profile; the executable,
sandbox, repos, worktree root and approval policy always come from the base file, so the file contains no
secrets and no paths other than a codex `executable` and an optional `claude_config_dir`.

```json
{
  "profiles": [
    {"id": "reader", "provider": "claude", "model": "sonnet", "allowed_tools": ["Read", "Grep"],
     "permission_mode": "plan", "max_timeout_seconds": 600, "max_concurrency": 2, "enabled": true},
    {"id": "writer", "provider": "claude", "allowed_tools": ["Read", "Edit"],
     "permission_mode": "acceptEdits", "enabled": false}
  ]
}
```

| key | rule |
| --- | --- |
| `id` | required, `[A-Za-z0-9._-]{1,64}`, unique; `default` and the `synapse.builtin.` prefix are reserved |
| `provider` | required, `claude` or `codex`. A `codex` profile is described in [Codex profiles](#codex-profiles) |
| `allowed_tools` | required, 1 to 64 unique tool names, a **subset** of the base allowlist (the ceiling) |
| `permission_mode` | required, one of `plan`, `manual`, `acceptEdits`, `dontAsk`. The most privileged mode a task may ask for (ranked in that order) and the default when the request gives none. `bypassPermissions` is never allowed |
| `enabled` | required boolean; a disabled profile is refused at validation |
| `model` | optional alias, `[A-Za-z0-9._:-]{1,64}`. Recorded only for now (the router will pass it in A7b) |
| `max_timeout_seconds` | optional, above 0 and at most the base profile's value (default: the base value) |
| `max_concurrency` | optional integer 1 to 6 (default 1); enforced by `claim_task` through the profile row |
| `executable` | **codex only, required**: absolute, normalized path of the `codex` binary (checked to exist and be executable only when `enabled`). Refused on a claude entry |
| `claude_config_dir` | **claude only, optional**: the Claude config directory of the account this profile runs as (the directory you would pass as `CLAUDE_CONFIG_DIR` to log in). Path only; see [Per-profile Claude config directories](#per-profile-claude-config-directories). Refused on a codex entry |

Unknown keys are rejected; keys that look like credentials (`key`, `token`, `secret`, `password`,
`credential`, `auth`) get a specific "credentials do not belong" error. At most 3 profiles can be declared
(4 in the registry including `default`).

## Registry and sync

At startup the daemon builds the registry (`default` plus the declared profiles, `default` primary) and
syncs the declared profiles into the existing `profiles` table (no migration): insert missing rows;
update `enabled`, `max_concurrency` and `config_ref = 'registry:<sha256 of the canonical profile>'` and bump
`version` only when something changed; never delete. A row from an earlier file that is no longer declared
is disabled. A declared id that collides with a row not created by the registry is a startup error. The
`default` profile has no new row (its row is the built-in agent profile).

## Validation

`resolve_profile(id)` returns the registry profile; an unknown, disabled or cooling-down id is a 422 (`unknown
profile`, `profile is disabled`, `profile is cooling down`), before any run, worktree or process exists. `allowed_tools`, `timeout_seconds` and `permission_mode` of every task are then
checked against that profile's ceilings.

## `profile` on `POST /api/v1/agent-runs` (A7b-1)

The body accepts an optional `profile` (`[A-Za-z0-9._-]{1,64}`). Absent, `null` and `"default"` behave exactly
as before and keep the old idempotency fingerprint, so earlier receipts still replay. A named profile is
validated against its ceilings (tools, permission mode, timeout; 422 for unknown or disabled) and is part of the
idempotency fingerprint, so reusing a key with a different profile is a 409.

A named profile's id is recorded in the task spec as `profile_id` (A7b-2) and, only when set, is part of the
spec hash an approval binds, so default-profile specs and their hashes are unchanged. The approver subject and
`GET /api/v1/runs/{id}/agent` show it as `profile` on each task's `spec`. The run-level choice is recorded when
the run starts, in the same transaction, as an `agent.run_profile_selected` event (aggregate `run`, ids only):
`{run_id, profile_id, requested, task_profiles: {node: profile id}, fallback: null, principal}`. `profile_id` is
`default` when none was named, `requested` is the named id or `null`, and `task_profiles` gives each task's
requested profile after per-task overrides. `GET /api/v1/runs/{id}/agent` (and so `agentctl agent-show`) shows it
as the top-level `profile` (`null` for runs started before this event existed). Spawn-time recheck (A7b-3): before spawning a task whose spec carries a `profile_id`, the engine asks the
runtime to recheck that id against the `profiles` table (the table the registry syncs into). A missing row
fails the task closed with detail `profile_unknown`, a row with `enabled = 0` with `profile_disabled`; no process
is spawned, the attempt is recorded as failed and the reason appears in the agent summary. A failing or absent
check fails closed too (`profile_check_failed`, `profile_check_unavailable`). The row must also be registry-owned
(`provider` `claude` or `codex` and `config_ref` starting `registry:`; the row's provider must also match the
registry entry, else `profile_provider_mismatch`), otherwise the task fails with `profile_not_registry`, so the
built-in agent row or a mock profile id cannot satisfy a named `profile_id`; any other checker result is refused as
`profile_check_invalid`. A gated task refused this way ends `failed` with its approval consumed and nothing spawned
(the same as a worktree refusal). In `__main__.py`, `recover_interrupted()` runs before `sync_profiles` but never spawns,
and `resume_parked()` runs after the sync; that ordering is load-bearing. Specs without `profile_id`
(the default profile) skip the check and behave exactly as before. The task ends `failed` (not `blocked`):
`blocked` means an attempt may have started, and none did. Limits: the table is synced from the registry at
daemon start, so a profile disabled in the file takes effect for queued runs after a restart; a `default`
run is never affected.

### Per-task profiles and execution (A7b, ADR 016 W1)

Each entry of `tasks` may carry its own `profile` (same pattern as the request-level one, which stays the default
for tasks that omit it; an explicit `"default"` on a task overrides a request-level profile). Tools, permission
mode and timeout of a task are checked against *its* profile's ceilings (422 for unknown or disabled). A task's
`profile` is part of the idempotency fingerprint only when named, so earlier receipts still replay.

A task that names a registry profile now:

- **claims capacity under that profile's row** (`claim_task` enforces its `max_concurrency` and the budget check
  uses its id); `tasks.assigned_profile_id`, `task_attempts.profile_id` and the usage ledger carry it. The run itself
  stays keyed on the built-in row (`mock_run_config`, `recover_interrupted`, `resume_parked` are unchanged, and
  recovery filters on `provider`, which registry rows share with the built-in row). The engine hook is
  `MockWorkflowEngine._profile_for`. If the named profile is unknown or disabled at claim time, the task claims under
  the run's profile instead and the spawn-time recheck above fails it closed, so it can never be stuck unclaimable;
- **spawns with that profile's `executable`.** `AgentProviderRouter` resolves a named profile through
  `AgentRuntime.registry` (`_resolve_profile`: the operator `--claude-cli-config` profile with the registry
  executable substituted). A named profile that cannot be resolved fails the attempt with `start_rejected` and
  **never** falls back to the default executable. Credentials, `state_dir`, sandbox and the other operator
  settings still come from the base profile, except the account: a profile with `claude_config_dir` runs with
  that directory's login and nothing else (see below); a profile without one uses the base credential handling. `--agent-config` currently sets every declared profile's executable to the base
  executable, so in the daemon the substitution is a no-op today and only the cap, budget scope, audit and
  limits above differ per profile; the registry `model` is not passed to the adapter. A `per_profile` budget written
  for the built-in profile id does not cover tasks that name a registry profile (their ledger rows carry the
  registry id);
- **is rechecked against the registry as it is now** just before spawn: the recorded tools, permission mode and
  timeout must still fit the named profile, otherwise the task fails closed with `profile_narrowed` (a profile
  narrowed in the file and restarted cannot run work accepted under wider limits, also for tasks parked behind an
  approval). A task also executes only under the row it was claimed under (`assigned_profile_id`, authoritative
  after a park/resume); a mismatch (for example the profile was disabled at claim time and is enabled again) fails
  closed with `profile_assignment_mismatch`;
- **records the selection** before the spawn as an `agent.profile_selected` event on the attempt:
  `{task_id, profile_id, provider, model, profile_sha256, requested, fell_back_from, reason}`. `model` is always
  `null` (the adapter does not report one), `requested` is `null` for the default profile, `fell_back_from` is
  always `null` until fallback exists, and `reason` is `requested` or `default`. If the event cannot be written the
  task fails closed (`profile_selection_not_recorded`) and nothing is spawned.

### Profile list and operator cooldowns (A7b W2)

- `GET /api/v1/profiles` (operator bearer) returns `{profiles: [{id, provider, enabled, max_concurrency, load,
  cooldown_remaining_seconds, position}]}`. `load` is the worker slots the profile occupies now (same rule as
  `claim_task`; `null` for `default`, which is capped by the run row), `position` is `primary`, `fallback` or `null`.
  No executable path, credentials or base-profile settings are returned. `probe()` health is not included yet.
- `POST /api/v1/profiles/{id}/cooldown` with `{"seconds": N}` (0 < N <= 86400) and
  `DELETE /api/v1/profiles/{id}/cooldown`; `agentctl profiles` and `agentctl profile-cooldown PROFILE --seconds N | --clear`.
  Only declared profiles qualify (`default` and unknown ids are a 422). They carry no Idempotency-Key: repeating one
  is harmless (a cooldown only extends, clearing is idempotent) and every call writes its own audit event.
- Each change writes a `profile.cooldown_set` / `profile.cooldown_cleared` event (aggregate `profile`, with the
  operator principal) **before** it takes effect; if the event cannot be written the change is refused with a 503,
  so a cooldown can never be cleared (the permissive direction) without a record.
- Effect: a task that names a profile in cooldown fails closed before any spawn with `profile_cooling_down`
  (same as a disabled profile). Nothing waits, retries or reroutes, so a cooldown cannot be used to evade a limit.
  Cooldowns are in memory: a daemon restart forgets them (the audit events remain).

Not done (ADR 016 later waves): `allow_fallback` and automatic fallback, `profile_id` in the run and usage
responses, the desktop profiles panel and profile picker, an `agentctl agent-start --profile` flag (today the
profile goes in the request body or in `--tasks-file` entries), and Codex as a primary or fallback profile (refused
by registry validation).

## Per-profile Claude config directories

`claude_config_dir` names the Claude config directory of the account a claude profile runs as. It is a path only:
**the daemon never opens, reads or copies a file in it.**

Checks at daemon start (startup fails on any of them) and again before every spawn (the spawn is refused):

- an absolute, normalized path with no symlink in it and none of `( ) * ? [ ]` or control characters (a path no
  deny rule can express is refused rather than left unprotected);
- a directory owned by the daemon's user with mode `0700` or stricter (no group or other bits), checked with
  `lstat`;
- not the same as, inside, or containing the state dir (and its `codex-run` scratch), the Claude scratch base
  (`<tmp>/synapse-agent-<uid>`), the `worktree_root`, any allowlisted repo, or the task's worktree;
- distinct per profile: two profiles may not name the same directory or nested directories (one account each);
- if `.credentials.json` exists it must be a regular file (not a symlink, and with a single hard link, so it
  cannot be a second name for another file such as a state-dir token) owned by the user with mode `0600` or
  stricter (`lstat` only). `claude_config_dir` cannot be combined with an `api_key`. At spawn it must exist; there is **no fallback** to `$CLAUDE_CONFIG_DIR` or `~/.claude`,
  and the daemon's `CLAUDE_CODE_OAUTH_TOKEN` is not passed to such a child (it would override the account): it runs
  as its account or not at all.

How the child gets the login: the child still gets the private per-run scratch `HOME`/`CLAUDE_CONFIG_DIR` (so the
account's own `settings.json`, hooks, `CLAUDE.md`, history and MCP config are never loaded). In that scratch
config dir, `.credentials.json` is

- unsandboxed: a **symlink** to `<claude_config_dir>/.credentials.json` (the CLI follows it);
- sandboxed (`"sandbox": true`): a **read-only single-file bind** of that file on top of a scratch placeholder.
  The daemon opens the file with `O_PATH | O_NOFOLLOW` (a handle with no read access; the contents are never
  read), re-checks it with `fstat` (regular file, owner, mode, one link) and binds that descriptor with
  `bwrap --ro-bind-fd`, so a file swapped after the path checks is refused, not bound. This needs a bwrap with
  `--ro-bind-fd` (checked with 0.13.1); an older bwrap fails the spawn. Nothing else of the directory is mounted,
  and every other profile's directory is unmounted.

Residual race (unsandboxed only): the symlink is resolved by the child when the CLI opens it, so a process running
as the same user that replaces `.credentials.json` between the daemon's checks and that open decides what the
child reads. Such a process can already read and write the whole directory, so this adds no capability; use the
sandbox when that matters.

Protection from agents: every registry profile's directory (for every task, including `default` ones) is added to
the Synapse-owned deny rules and to the sandbox's forbidden paths. The deny rules name the `Read` and `Edit` tools
only; whether `Glob`, `Grep` or other tools honour them depends on the Claude CLI and is not verified here (the
sandbox is the boundary that does not depend on the CLI). The forbidden paths mean no worktree, git dir, scratch or
executable mount can expose one. As ADR 019 notes, a child must hold its own model credential to work, so this
protects other profiles' accounts and the rest of the directory, not the running profile's own token from its own
agent. Unsandboxed children run as the same OS user and can read anything that user can; use the sandbox for the
kernel boundary.

The path is never returned by the API, never written to events or audit (the `profile_sha256` in
`agent.profile_selected` covers it), and `GET /api/v1/profiles` does not show it.

Unverified against a real Claude CLI (tests use fake CLIs only): how the CLI refreshes an expiring OAuth token
through the symlink (it may write through to the account's file, or replace the symlink in scratch, which would
leave the account's stored refresh token stale) and under the read-only bind (a refresh cannot be written back, so
a long run may fail once the access token expires). Log in again with that config dir if a profile stops
authenticating.

## Three Claude accounts, one per run

Three accounts (`claude-work`, `claude-personal`, `claude-personal-ios`), each with its own config directory, chosen
per run, with no automatic fallback. Paths below are placeholders.

1. Log each account in once, interactively, with its own directory (outside the state dir, the worktree root and
   the repos), and keep it private:

   ```sh
   CLAUDE_CONFIG_DIR=/path/to/claude-work-config claude          # then /login
   chmod 700 /path/to/claude-work-config
   chmod 600 /path/to/claude-work-config/.credentials.json
   ```

   Repeat for `/path/to/claude-personal-config` and `/path/to/claude-personal-ios-config`.

2. Declare the profiles in the `--agent-config` file (three declared profiles plus the implicit `default` is the
   registry maximum of four):

   ```json
   {
     "profiles": [
       {"id": "claude-work", "provider": "claude", "allowed_tools": ["Read", "Edit"],
        "permission_mode": "acceptEdits", "enabled": true,
        "claude_config_dir": "/path/to/claude-work-config"},
       {"id": "claude-personal", "provider": "claude", "allowed_tools": ["Read", "Edit"],
        "permission_mode": "acceptEdits", "enabled": true,
        "claude_config_dir": "/path/to/claude-personal-config"},
       {"id": "claude-personal-ios", "provider": "claude", "allowed_tools": ["Read", "Edit"],
        "permission_mode": "acceptEdits", "enabled": true,
        "claude_config_dir": "/path/to/claude-personal-ios-config"}
     ]
   }
   ```

   `allowed_tools` must be a subset of the base `--claude-cli-config` allowlist. Start `agentd` with
   `--claude-cli-config BASE.json --agent-config PROFILES.json`; a bad directory stops startup with a fixed message
   (the path is not echoed).

3. Pick the profile per run with `"profile": "claude-personal"` in the `POST /api/v1/agent-runs` body (or per task
   in `tasks[].profile`; `"default"` on a task opts it back to the base login). An unknown, disabled or cooling-down
   profile is a 422 and nothing is created. A task that fails is never retried under another profile.
   `agentctl profiles` lists them; `agentctl agent-show RUN` shows the run's `profile` and each task's `profile_id`.
Unless every task names a profile, one run still uses the runtime-wide `max_concurrency` as the global cap.

## Codex profiles

A `codex` entry (ADR 016 decision 4) makes the [Codex adapter](codex-cli-provider.md) available to tasks that
**name** it; it is never the primary or a fallback (registry validation refuses both, and `default` is always the
primary), so nothing moves a task from Claude to Codex or back. Codex stays off unless the operator declares an entry
with `"enabled": true`.

```json
{"profiles": [
  {"id": "codex-edit", "provider": "codex", "executable": "/usr/local/bin/codex",
   "allowed_tools": ["Read", "Glob", "Grep", "Edit", "Write"], "permission_mode": "acceptEdits", "enabled": true}
]}
```

- **Restriction model.** Codex has no tool allowlist. A task's `allowed_tools` and `permission_mode` only select a
  sandbox through a fixed table: `plan` -> `-s read-only`; `acceptEdits` -> `-s workspace-write` when `Edit` or
  `Write` is requested, else `read-only`. Inside either sandbox the agent can run **any shell command**; the list is
  not a tool restriction, it is stored verbatim (and bound by an approval) only because it is what was asked for.
- **Eligibility (422 at validation, `codex_ineligible` at spawn).** Every tool must be one of `Read`, `Glob`,
  `Grep`, `Edit`, `Write` (any `Bash(...)` pattern or other tool is refused, never widened); `dontAsk` and `manual`
  are refused (no Codex equivalent); `Edit`/`Write` with `plan` is refused. The entry's own `allowed_tools` /
  `permission_mode` ceiling must pass the same rule at startup. The tools must also be in the base allowlist.
- **What the entry cannot set.** No `model` (refused: `CodexCliProfile` has none, so `agent.profile_selected`
  records `model: null`), no extra arguments, no state dir (the daemon's own state dir is used, so the startup
  credential sweep covers `codex-run/` scratch dirs), no credentials. The adapter copies `$CODEX_HOME/auth.json`, else
  `~/.codex/auth.json` of the daemon's user, into a per-run scratch `CODEX_HOME` at `0600` and removes it afterwards.
- **Base profile still required.** Agent runs, repos, worktrees, the approval policy, the resource limits
  (`memory_limit_bytes` and output caps) and the same-user acknowledgement come from the enabled
  `--claude-cli-config` base, so a Codex task needs it enabled (and with `"sandbox": false`, see above).
- **Unsandboxed.** **Codex runs unsandboxed by Synapse.** Synapse does not wrap the Codex child in bwrap; the only confinement is Codex's own `-s read-only` / `-s workspace-write`, and `read-only` does not confine reads. A Codex child can therefore read anything the daemon's user can: `approver.token` and `operator.token` in the state dir (so it can approve its own gated requests), the SQLite database, `agentd.sock`, and the user's other credentials. For that reason a codex profile is **refused** (startup error, and `codex_ineligible`/422 as backstops) whenever the `--claude-cli-config` base has `"sandbox": true`: it would silently get less isolation than every Claude child. Codex is only available with the base sandbox off and `--allow-same-user-agents` acknowledged; the startup warning and the `agent_runtime.same_user_acknowledged` audit row then record `codex_unsandboxed: true`.
- **Same trust domain.** The Codex child runs as the daemon's OS user. `read-only` is not known to confine reads,
  and the scratch `auth.json` is readable by the agent for the run's duration (see the adapter doc).
- **No fallback after a refusal.** A spawn refusal (for example a worktree with `AGENTS.md`) fails the attempt with
  `start_rejected`; an `unknown` outcome blocks the task. Neither re-runs the task on Claude or on another profile.
- **Health.** `AgentRuntime.probe_profile(id)` runs only `codex --version` (no agent, no worktree, no credential
  copy): `healthy` for a verified version, `unverified_version` for another parseable one, `unavailable` when
  disabled, missing or unparseable. No API surface calls it yet.
- **Unverified Codex version.** **Version gate at spawn.** Before every Codex spawn the router checks `codex --version` (run off the event loop and cached per executable realpath, device, inode, size, mtime and ctime, at most 16 entries; a successful result is kept while that identity holds, a failed one for 30 s, so a transient failure is retried. The key is the configured executable: if it is a wrapper script, a change to what the wrapper execs is not noticed until the wrapper itself changes). The `/tmp`/`$TMPDIR` exclusion of `workspace-write` relies on `-c sandbox_workspace_write.*` keys verified only on `VERIFIED_CLI_VERSIONS` (`0.170.0`), and a newer Codex could silently ignore a renamed key, so a task that would run `workspace-write` on any other (or unparseable) version is refused before spawning with detail `codex_ineligible`. A `read-only` task still runs on an unverified version, and each such spawn logs a warning naming the task, the profile and the parsed version (digits and dots only, otherwise `unknown`). Updating Codex therefore disables Codex edits until the new version is verified and added to `VERIFIED_CLI_VERSIONS`. **Probe failure** (the binary is missing, exits non-zero, prints no parseable `codex-cli X.Y.Z`, times out after 10 s, or the check itself errors) counts as *no version*: exactly like an unverified version, a `workspace-write` task is refused with `codex_ineligible` and nothing is spawned, while a `read-only` task still runs and its spawn logs the version as `unknown`. A probe failure never fails a task as `start_rejected` by itself and never affects Claude tasks.
- **Provider of an id is fixed.** `sync_profiles` refuses to turn an existing claude id into a codex one (or back);
  attempts and usage rows keep pointing at the provider they ran under.

## Not in this part

Fallback selection and `allow_fallback`; Codex as primary/fallback; a probe API surface.

## Effective profile in run views

`GET /api/v1/runs/{id}/agent` reports `profile_id` on each task (the profile it was claimed under, `null` before the
claim) and on its latest `attempt`. `spec.profile` is only what was requested; the two can differ for a task that named
no profile (it runs under the built-in `agent` profile). Usage per profile is already recorded on
`usage_observations.profile_id`; the `/usage` view is not changed here.

## Operator default account (client side)

`scripts/synapse_up.py up` can declare one account profile from a private one-line file and record its id in
`<state dir>/default-profile` (see [daily use](daily-use.md)). The daemon never reads that file and its meaning of an
unnamed profile (`default`) is unchanged; `agentctl agent-start` and the desktop picker use it only to *name* the profile
when the operator did not (`src/synapse/default_profile.py`). The id must match the profile id pattern, may not be
`default` or start with `synapse.builtin.`, and the file must be a regular file owned by you and not writable by others.
