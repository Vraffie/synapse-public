# Agent profiles (`--agent-config`)

ADR 016, wave W1, part 1 (config and registry). This adds the file and the registry plumbing. There is no API
or router change yet: a run still executes through the single `--claude-cli-config` profile. The registry is
used for validation (`AgentRunService.resolve_profile` / `validate(req, profile_id)`); A7b exposes `profile`
on the API and routes execution.

## Backward compatibility

Nothing changes without `--agent-config`. The `--claude-cli-config` profile is the implicit profile
**`default`** (the registry primary). Requests that name no profile, or `default`, are validated exactly as
before. Existing receipts, specs and the built-in `synapse.builtin.agent.claude.v1` row are untouched.

## Format

`--agent-config PATH` requires `--claude-cli-config` (startup fails otherwise). JSON, like the base file, with
the same file checks: a user-owned regular file, not a symlink, **not group/world writable**, at most 64 KiB,
no duplicate keys, no `NaN`/`Infinity`. The file holds narrower views of the base profile; the executable,
sandbox, repos, worktree root and approval policy always come from the base file, so the file contains no
paths and no secrets.

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

`resolve_profile(id)` returns the registry profile; an unknown or disabled id is a 422 (`unknown profile`,
`profile is disabled`). `allowed_tools`, `timeout_seconds` and `permission_mode` of every task are then
checked against that profile's ceilings.

## `profile` on `POST /api/v1/agent-runs` (A7b-1)

The body accepts an optional `profile` (`[A-Za-z0-9._-]{1,64}`). Absent, `null` and `"default"` behave exactly
as before and keep the old idempotency fingerprint, so earlier receipts still replay. A named profile is
validated against its ceilings (tools, permission mode, timeout; 422 for unknown or disabled) and is part of the
idempotency fingerprint, so reusing a key with a different profile is a 409.

A named profile's id is recorded in the task spec as `profile_id` (A7b-2) and, only when set, is part of the
spec hash an approval binds, so default-profile specs and their hashes are unchanged. The approver subject and
`GET /api/v1/runs/{id}/agent` show it as `profile`. Spawn-time recheck (A7b-3): before spawning a task whose spec carries a `profile_id`, the engine asks the
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
  settings still come from the base profile; the registry does not model them, so profiles do not (yet) isolate
  accounts from each other. `--agent-config` currently sets every declared profile's executable to the base
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
responses, the desktop profiles panel, and Codex as a primary or fallback profile (refused by registry validation).
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
- **Base profile still required.** Agent runs, repos, worktrees, the approval policy and the same-user
  acknowledgement come from the enabled `--claude-cli-config` base, so a Codex task needs it enabled too.
- **Same trust domain.** The Codex child runs as the daemon's OS user. `read-only` is not known to confine reads,
  and the scratch `auth.json` is readable by the agent for the run's duration (see the adapter doc).
- **No fallback after a refusal.** A spawn refusal (for example a worktree with `AGENTS.md`) fails the attempt with
  `start_rejected`; an `unknown` outcome blocks the task. Neither re-runs the task on Claude or on another profile.
- **Health.** `AgentRuntime.probe_profile(id)` runs only `codex --version` (no agent, no worktree, no credential
  copy): `healthy` for a verified version, `unverified_version` for another parseable one, `unavailable` when
  disabled, missing or unparseable. It is advisory; no API surface calls it yet.
- **Provider of an id is fixed.** `sync_profiles` refuses to turn an existing claude id into a codex one (or back);
  attempts and usage rows keep pointing at the provider they ran under.

## Not in this part

Fallback selection and `allow_fallback`; Codex as primary/fallback; a probe API surface.

## Effective profile in run views

`GET /api/v1/runs/{id}/agent` reports `profile_id` on each task (the profile it was claimed under, `null` before the
claim) and on its latest `attempt`. `spec.profile` is only what was requested; the two can differ for a task that named
no profile (it runs under the built-in `agent` profile). Usage per profile is already recorded on
`usage_observations.profile_id`; the `/usage` view is not changed here.
