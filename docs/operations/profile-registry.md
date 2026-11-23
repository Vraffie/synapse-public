# Profile registry (data model, loader, selection) — not wired yet

`synapse.daemon.profile_registry` is the M4-B3 building block from ADR 015: a validated multi-profile registry, a
pure profile-selection function and a cooldown model. **Nothing in the daemon uses it yet.** `agentd
--claude-cli-config` still builds the single operator profile through `agent_config`, and the engine, runtime and
API are unchanged.

## File format

`load_registry(path, state_dir)` reads either shape:

```json
{
  "profiles": [
    {"id": "claude-main", "provider": "claude", "executable": "/usr/bin/claude",
     "allowed_tools": ["Read", "Glob", "Grep", "Edit"], "max_concurrency": 2,
     "max_timeout_seconds": 1800, "model": "opus-5", "enabled": true},
    {"id": "claude-alt", "provider": "claude", "executable": "/usr/bin/claude",
     "allowed_tools": ["Read", "Glob", "Grep"], "max_concurrency": 1, "enabled": true},
    {"id": "codex-1", "provider": "codex", "executable": "/usr/bin/codex",
     "allowed_tools": [], "max_concurrency": 1, "enabled": false}
  ],
  "primary": "claude-main",
  "fallback": ["claude-alt"],
  "max_concurrency": 3,
  "repos": ["/home/me/src/project"],
  "worktree_root": "/home/me/.local/share/synapse/worktrees"
}
```

Per profile (exactly these keys): `id` (1-64 of `[A-Za-z0-9._-]`), `provider` (`claude` or `codex`), `executable`
(absolute, normalized), `allowed_tools` (the profile's **ceiling**: up to 64 names or `Name(pattern)` entries, no
duplicates, at least one for `claude`; a `codex` profile may use an empty list because its restriction is a sandbox
mode, not tool names), `max_concurrency` (integer 1 to 6), `enabled` (boolean); optional `max_timeout_seconds` (above
0, at most 86400, default 3600) and `model` (1-64 of `[A-Za-z0-9._:-]`).

Top level: `profiles` (1 to 4), `primary` (a profile id), optional `fallback` (ordered profile ids), optional
`max_concurrency` (the **global** cap, 1 to 6, default 2) and the existing `repos`/`worktree_root` pair (given
together, validated exactly as in [agent-runtime](agent-runtime.md)).

Rules: unknown or duplicate keys, non-finite numbers, wrong types and bools-as-integers are refused; the file must
be a user-owned regular file, not group/world writable, not a symlink, at most 64 KiB. Fallback profiles must be
known, distinct from each other and from the primary, and **enabled**; an enabled profile's executable must exist and
be executable (a disabled one need not). **Credentials are never part of the registry**: keys such as `api_key`,
`token`, `credentials_file` or `env` are rejected like any unknown key. Profiles use the user's own CLI login, as
today.

Legacy files (the single-profile shape with a top-level `executable`) keep working: they are judged by the existing
`load_agent_config` unchanged and read as a one-profile registry with id `claude-default`, `max_concurrency` 2 (the
engine's current cap), no fallback. `RegistryError` subclasses `AgentConfigError`, so existing startup handling
applies.

## Selection

`select_profile(registry, requested_id, load, unavailable, *, allow_fallback=None) -> Decision` is pure: it reads
its arguments and returns a `Decision(profile_id, reason, fell_back_from, skipped)`.

- `load` is the running-task count per profile id; `unavailable` is any collection of ids not to use now (for
  example `Cooldowns.unavailable()`).
- No `requested_id`: the primary is wanted and fallback is allowed. A task that names a profile gets fallback only
  when the caller passes `allow_fallback=True` (and `allow_fallback=False` switches it off for the default too).
- Candidates are the wanted profile, then the chain (primary, then `fallback` in order). A candidate is skipped when
  it is `disabled`, `unavailable` or `at_profile_cap`; the first usable one wins.
- If the sum of all `load` values already reaches the registry's global cap the answer is `at_global_cap` and no
  profile (fallback cannot help). Unknown ids in `load` count toward that sum.
- A disabled profile is never selected; neither the per-profile nor the global cap is ever exceeded by starting the
  chosen task. Unknown requested ids give `unknown_profile`; nothing usable gives `no_profile_available` with the
  per-candidate reasons in `skipped`.
- Fallback is only for a task whose process has **not started**. After a process may exist (`unknown`), the caller
  must not select again: nothing moves or retries.

## Cooldowns

`Cooldowns(clock=time.monotonic)` records "do not use until" per profile. After a rate-limit or quota failure the
caller does `cooldowns.set(profile_id, seconds)` (above 0, at most 86400; a shorter report never shortens an
existing cooldown). `unavailable()` lists the profiles still cooling down and feeds `select_profile`. A cooldown
only keeps a profile out of consideration; it never retries the failed attempt, and a task that named the profile
explicitly does not rotate to another one by default. This is not rate-limit evasion by design: the limited profile
is left alone until it recovers.

## Not implemented

- Wiring: nothing reads a registry at startup, passes a profile to a task, tracks `load`, or sets cooldowns from
  provider failures; `AgentRuntime` and `AgentWorkflowEngine` still use one profile and one engine-wide cap.
- Per-profile concurrency inside the engine (today `claim_task` caps one built-in profile row).
- A `profile` field on task specs/API requests, and recording the chosen profile on attempts.
- The Codex adapter (`provider: "codex"` is accepted in the registry only so the data model is ready; no Codex
  executable is ever launched by this module).
- Hot reload, per-profile credentials, quota/usage-based selection.
