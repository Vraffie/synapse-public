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
| `provider` | required, `claude` only. `codex` is refused with "disabled until W3" |
| `allowed_tools` | required, 1 to 64 unique tool names, a **subset** of the base allowlist (the ceiling) |
| `permission_mode` | required, one of `plan`, `manual`, `acceptEdits`, `dontAsk`. The most privileged mode a task may ask for (ranked in that order) and the default when the request gives none. `bypassPermissions` is never allowed |
| `enabled` | required boolean; a disabled profile is refused at validation |
| `model` | optional alias, `[A-Za-z0-9._:-]{1,64}`. Recorded only for now (the router will pass it in A7b) |
| `max_timeout_seconds` | optional, above 0 and at most the base profile's value (default: the base value) |
| `max_concurrency` | optional integer 1 to 6 (default 1); enforced by `claim_task` through the profile row |

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
(`provider = 'claude'` and `config_ref` starting `registry:`), otherwise the task fails with `profile_not_registry`, so the
built-in agent row or a mock profile id cannot satisfy a named `profile_id`; any other checker result is refused as
`profile_check_invalid`. A gated task refused this way ends `failed` with its approval consumed and nothing spawned
(the same as a worktree refusal). In `__main__.py`, `recover_interrupted()` runs before `sync_profiles` but never spawns,
and `resume_parked()` runs after the sync; that ordering is load-bearing. Specs without `profile_id`
(the default profile) skip the check and behave exactly as before. The task ends `failed` (not `blocked`):
`blocked` means an attempt may have started, and none did. Limits: the table is synced from the registry at
daemon start, so a profile disabled in the file takes effect for queued runs after a restart; a `default`
run is never affected.

Not yet done: the per-profile `max_concurrency` is **not** enforced for API-started runs, deliberately.
`claim_task` enforces it against the profile row the task is *assigned* to, but every agent run is claimed
under the single built-in `synapse.builtin.agent.claude.v1` row (the engine takes one `profile_id` per run,
`mock_run_config`, the attempts, usage observations, recovery and `resume_parked` all key on it, and the
built-in row's cap is the runtime-wide `max_concurrency`). Honouring a named profile's cap would mean
assigning tasks to the named row, which changes what those stores and recovery filters record and match;
that is a change to authoritative state semantics (likely a migration or an ADR amendment), not a small safe
slice. `profile_id` also remains a recorded label for execution: it is rechecked at spawn, but execution
still goes through the single `--claude-cli-config` profile (every declared profile is a narrower view of it,
so a run can never exceed the base profile), and `"default"` is never stored (the default is represented by
absence).

## Not in this part

Codex (stays disabled; W3), fallback selection,
per-attempt `agent.profile_selected` events and router construction per profile (A7b-2).
