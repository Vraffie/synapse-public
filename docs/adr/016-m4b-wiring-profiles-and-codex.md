# ADR 016: Wiring the profile registry, usage ledger and Codex adapter into agent runs

Status: accepted in part. W0 (finished runs release capacity) and W1 are implemented; decision 4 (Codex router
wiring, sandbox mapping, eligibility, recovery set) and the codex part of decision 5 (never primary/fallback, no
fallback after a spawn refusal or `unknown`) are implemented and tested with fake executables only (2026-11-23). Not
done: automatic fallback/`allow_fallback`, the decision 9 gates (no independent review of the wiring and no real
`SYNAPSE_REAL_CODEX=1` run through the daemon yet), a probe API surface. Security review of PR #33: Synapse runs
Codex without bwrap, so a codex profile is refused when the base profile has `"sandbox": true` (a fake-executable test
proved a codex child could otherwise read `approver.token`); with the sandbox off it runs unsandboxed under the
`--allow-same-user-agents` acknowledgement, which records `codex_unsandboxed`. Re-review: `workspace-write` is refused at
spawn on any Codex version outside `VERIFIED_CLI_VERSIONS` (the `/tmp` tightening keys are verified only on 0.170.0;
a 0.171.0 install, as the re-review reported on the owner's machine, can therefore only run `read-only` tasks, each
logged). Date: 2026-11-20. Follows ADR 015 (`docs/adr/015-m4b-parallel-agents-and-profiles.md`, PR #85).

This ADR decides how the open M4-B pieces are connected. It was written against `origin/main` plus the **unmerged**
PRs it names; every statement about those PRs describes their heads at the time of writing and must be re-checked
when they merge. **Unverified** assumptions are marked `[UNVERIFIED]` and collected in section 11.

| PR | Piece | Used here as |
| --- | --- | --- |
| #79 | Claude `probe()` version check | `healthy` / `unverified_version` / `unavailable` |
| #81 / #82 | worktree removal / abandon | operator exits; abandon leaves attempts `unknown` |
| #84 (+ #90) | Claude `--tools` restriction | allowlist semantics for Claude |
| #88 | multi-task runs (`tasks`, `depends_on`) | request shape the `profile` field extends |
| #91 | `daemon/profile_registry.py`: `load_registry`, `select_profile`, `Cooldowns` | pure selection logic |
| #92 | usage ledger (migration 0014) | `usage_observations.profile_id` |
| #93 | `providers/codex_cli.py` (`CodexCliProvider`, `CodexCliProfile`) | second adapter, disabled by default |

## What the code already gives us (read before deciding)

- **Per-profile and global caps are already transactional.** `persistence/task_claim.claim_task(profile_id=...,
  max_running=...)` runs inside `BEGIN IMMEDIATE`, reads `profiles.max_concurrency` for `profile_id`, counts occupied
  slots from `task_attempts` (state not in completed/failed/cancelled) plus assigned/running tasks without an
  attempt, and refuses (`StateConflict`) when the profile count reaches its row's `max_concurrency` or the global
  count reaches `max_running`. The count is **global across runs and engines**, and **`unknown` attempts keep
  occupying their slot** (deliberate: the process may still be running).
- **The schema already has the columns.** `profiles(id, provider CHECK IN ('claude','codex','mock'), config_ref,
  enabled, max_concurrency, health_state, version)`, `tasks.assigned_profile_id`, `task_attempts.profile_id` and
  `usage_observations.profile_id` all exist since migration 0001. `profiles` has no triggers, so rows may be updated.
- **`mock_run_config.profile_id = AGENT_PROFILE` is the run-kind marker, not the task's profile.** `persistence/
  agent_tasks.is_agent_run` requires that row to equal `AGENT_PROFILE` (`synapse.builtin.agent.claude.v1`),
  fixture `fixed-v1` and the profile's provider to be `claude`; mock exclusion, `recover_interrupted` and cancel all
  key off it. It must stay as it is.
- **The engine takes one `profile_id` per run.** `MockWorkflowEngine.run(run_id, profile_id, scenarios,
  approval_steps)` rejects a profile whose provider is not `_profile_provider` (`"claude"` for
  `AgentWorkflowEngine`), passes the same id to `claim_task`, `gates.park` and `_execute`, and
  `AgentProviderRouter` is built from one `ClaudeCliProfile`.
- **Recovery is provider-pinned.** `persistence/mock_recovery.recover_mock_run(..., provider_kind="claude")` refuses
  (`StateConflict`) any task or attempt whose profile's provider differs from `provider_kind`.
- **Specs are a closed shape.** `agent_tasks._check_shape` requires exactly eight keys and the whole dict is hashed
  into the `agent.tasks_configured` event; adding a key changes the hash of new runs and must not invalidate old ones.

## Decisions

### 1. How a task names a profile

- Add an **optional `profile` string** (a registry profile id, `[A-Za-z0-9._-]{1,64}`) in two places in
  `POST /api/v1/agent-runs`: on each `tasks[]` entry (#88's `TaskRequest`) and, for the single-task body and as the
  default for tasks that omit it, at request level. No `profile` means "the registry primary" and allows fallback;
  an explicit `profile` pins the task (no fallback unless `allow_fallback: true` is also given, matching
  `select_profile(..., allow_fallback=...)`).
- **Spec storage**: `agent_tasks.parse_task_specs` / `_check_shape` accept an optional `profile` key (and optional
  `allow_fallback` boolean). Absent keys mean `None`/`False`; the key set check becomes "required eight keys,
  optionally plus these two", and `_sha(tasks)` hashes whatever is present. Specs written before this change stay
  valid and keep their hash. The spec records what the operator **asked** for; what was actually **selected** is
  decided at claim time (decision 5) and recorded separately.
- **Validation (422 before any run or worktree exists)**: unknown id, disabled profile, `allowed_tools` not a subset
  of that profile's ceiling, `permission_mode` or timeout outside the profile's limits, and the Codex eligibility rule
  (decision 4).
- **Idempotency**: `AgentRunService._fingerprint` includes `profile` and `allow_fallback` per task whenever they are
  present. A request that omits both keeps today's fingerprint byte-for-byte (the legacy single-task shape already
  does this in #88), so existing receipts still replay.
- Rejected: naming profiles by provider (`"claude"`/`"codex"`): several profiles per provider is the point of the
  registry. Rejected: a run-level profile only: it would forbid mixing providers in one DAG, which is the main use.

### 2. Where per-profile concurrency is enforced

**Decision: one `profiles` row per registry profile, enforced by the existing `claim_task`.** At daemon start, sync
the registry into `profiles` (insert missing rows; update `enabled`, `max_concurrency`, `provider`, `display_name`,
`config_ref = 'registry:<sha256 of the canonical profile spec>'`; bump `version`; never delete rows, because
attempts and usage reference them). The global registry cap becomes the engine's `max_concurrency` (and therefore
`claim_task`'s `max_running`, 1..6 already enforced).

- **Guarantee**: the check and the claim are one SQLite write transaction, so no interleaving (two engines, a
  restart, a gate resume) can exceed a profile's or the global cap. It also counts `unknown` attempts, so a profile
  whose process may still exist keeps its slot until an operator resolves it.
- **Rejected: registry-side check before the claim.** Pure and in-memory, it cannot see attempts left `unknown` by a
  crash or claims made through another path, and gives only best-effort exclusion. It is still used, but only to
  *choose* a profile (decision 5); `claim_task` remains authoritative. If it loses a race the claim raises
  `StateConflict`, which the engine already treats as "skip this task this tick" (`except StateConflict: continue`).
- **Legacy files keep `AGENT_PROFILE` as their only row** (their registry id `claude-default` is an in-memory alias
  that maps to that row). Registry-format files create rows named by their ids; reserve the prefix `synapse.builtin.`
  (registry ids may not start with it).
- **Migration needs: none for enforcement.** Required code changes: `MockWorkflowEngine` gains an overridable hook
  `_profile_for(run_id, task_id, default_profile_id)` used at the claim, `gates.park` and `_execute` call sites
  (default returns the argument, so mock runs are untouched); `AgentWorkflowEngine` overrides it to apply
  decision 5. The run-level `profile_id` check in `run()` becomes "marker profile exists", not "provider ==
  `_profile_provider`".
- **Finding to fix in the same PR (likely pre-existing, `[UNVERIFIED]` by test, derived from the SQL in
  `claim_task`)**: the occupancy CTE counts attempts in any non-terminal state **regardless of the run's state**,
  and `abandon` (#82) deliberately leaves attempts `unknown`. An abandoned run's `unknown` attempt would therefore
  occupy a profile and a global slot forever, and enough abandons would block every future claim. Decision:
  `claim_task` excludes attempts and tasks whose run is `failed`, `cancelled` or `succeeded` (the operator's
  `no-agent-process-remains` assertion is what justifies releasing the slot; the honest `unknown` record stays).
  Exit test: abandon a run holding the only slot, then a new run's task claims that profile.

### 3. Recording profile id, provider and model

- **Profile id**: already persisted by `claim_task` (`tasks.assigned_profile_id`) and `reserve_attempt`
  (`task_attempts.profile_id`), and by #92 on `usage_observations.profile_id`. No schema change.
- **Provider and model**: not columns. Record one event per attempt, `agent.profile_selected`
  (aggregate `task_attempt`), written in the same transaction as the claim, with payload
  `{task_id, profile_id, provider, model, profile_sha256, requested, fell_back_from, reason, skipped}` where
  `profile_sha256` is the same digest as `profiles.config_ref`. Events are immutable and replayable, so the exact
  executable-independent profile used for an attempt can be reconstructed even after the registry file changes.
  `model` comes from the registry profile; for Claude it is passed through the adapter's allowlisted `--model`,
  for Codex see decision 4 `[UNVERIFIED]`.
- **Rejected: `task_attempts.provider/model` columns.** A migration for data the event stream already holds, and the
  attempt table is not append-only today, so it would invite drift. Revisit if reporting needs SQL joins; M5's usage
  views can join `usage_observations.profile_id` to `profiles.provider` today.
- The per-run response of `GET /api/v1/runs/{id}/agent` and `/usage` gains `profile_id` per task/attempt (read from
  `task_attempts.profile_id`).

### 4. Per-provider adapter construction (`AgentProviderRouter`)

`AgentProviderRouter.__init__` takes the **registry** (profiles by id) instead of one `ClaudeCliProfile`; the cache key
becomes `(profile_id, narrowed-key)`; `start(request)` reads `request.profile_id` (the selected profile).

- **Claude profile**: unchanged mechanism: `replace(base_profile, allowed_tools=<task tools>, permission_mode=<mode>)`
  re-validates; the adapter adds `--tools <base names>` (#84/#90), so the allowlist restricts the tool set.
- **Codex profile**: build `CodexCliProfile(executable, state_dir, enabled, sandbox=<derived>, ...)` from the registry
  profile plus the daemon's state dir. Differences that the design must respect (from #93's code and docs):
  - **No tool names.** Codex restricts by sandbox (`-s read-only|workspace-write`), not by tool; the agent can still
    run shell commands inside the sandbox. A Claude `allowed_tools` list cannot be honoured, only mapped to a
    sandbox class. `[UNVERIFIED]` for `read-only`: whether the sandbox confines **reads** to the worktree (e.g.
    `~/.ssh`); #93 verified write refusal only.
  - **Sandbox from `permission_mode`** (a fixed table, anything else is refused at validation with 422):
    `plan` -> `read-only`; `acceptEdits` -> `workspace-write`; `dontAsk` and `manual` -> **not supported** (Claude
    semantics with no Codex equivalent).
  - **Eligibility rule for a task on a Codex profile**: every entry of `allowed_tools` must be one of `Read`, `Glob`,
    `Grep`, `Edit`, `Write`; any `Bash(...)` pattern (or other tool) makes the task ineligible, because the Codex
    sandbox cannot express it and mapping it would silently widen it. The sandbox is `workspace-write` iff
    `Edit` or `Write` is requested **and** the mode is `acceptEdits`, else `read-only`; a request for `Edit`/`Write`
    with `plan` is a 422. The registry's Codex `allowed_tools` stays an empty ceiling; the *sandbox class* is the
    ceiling. The spec still stores the task's `allowed_tools` verbatim (it is what the operator asked for and what
    the approval binding hashes); the docs must say plainly that for Codex it selects a sandbox class and is **not**
    a tool restriction.
  - **Credentials and homes**: the adapter copies `auth.json` at `0600` into a per-run scratch `CODEX_HOME`
    (removed afterwards) and replaces `HOME`/`TMPDIR`; nothing about credentials goes into the registry (already
    enforced by #91's key rejection).
  - **`workspace-write` overrides**: the fixed `-c sandbox_workspace_write.exclude_slash_tmp=true`,
    `exclude_tmpdir_env_var=true`, `network_access=false` are part of the adapter and must not be removable via
    `extra_args` (already an allowlist). They are mandatory because of the `/tmp` finding (decision 9).
  - **Model**: `CodexCliProfile` has no `model` field in #93; a registry `model` for Codex needs an allowlisted
    `-m`/`--model` `extra_args` entry or a new field `[UNVERIFIED]`. First wiring ignores `model` for Codex and
    records `model: null` in `agent.profile_selected` rather than claiming one.
- **Usage**: both adapters already return `UsageObservation`; #93 records only input/output tokens for Codex
  (cached/cache-write/reasoning present in the stream but unrecorded). The ledger shows missing fields as `null`;
  extending Codex parsing is a follow-up, not a prerequisite.
- **Recovery**: `recover_mock_run(provider_kind=...)` must accept a set (`{"claude","codex"}`) instead of one string,
  and `AgentRuntime._reconcile` passes the providers present in the registry (plus `claude` for legacy history).
  A Codex attempt left `unknown` is reconciled exactly like a Claude one.

### 5. Fallback

- **When**: only while choosing the profile for a task that has **no process and no attempt yet** (the moment it
  leaves `ready`, at the claim or at `gates.park`). The engine calls `select_profile(registry, spec.profile,
  load, cooldowns.unavailable(), allow_fallback=spec.allow_fallback)`; `load` is computed by one read of the same
  occupancy query `claim_task` uses. Reasons that permit moving on are exactly #91's: `disabled`, `unavailable`
  (cooldown), `at_profile_cap`. A **spawn refusal** (adapter `start` raising before any process, already tracked as
  `router.start_failed` -> attempt `failed`) does **not** trigger fallback in the first version: that attempt is
  already recorded and consumed; a later task attempt is a new decision under the existing "no automatic retry"
  policy (`AgentWorkflowEngine._retryable -> False`). `[UNVERIFIED]` whether operators will want
  retry-on-other-profile after a clean spawn failure; leave it out until asked.
- **Never after `unknown`**: once an attempt exists in any state other than a clean pre-spawn refusal, the task is
  never reselected; `unknown` blocks the task and sends the run to `recovering` exactly as today.
- **Recorded as**: the `agent.profile_selected` event (decision 3) with `requested`, `profile_id`, `fell_back_from`,
  `reason` and the `skipped` list `select_profile` returns, so every move away from the wanted profile is visible
  and auditable. A claim that fails with `StateConflict` writes nothing and is simply re-evaluated next tick.
- **Cross-provider fallback is OFF initially**: until the Codex gates (decision 9) pass, registry validation rejects a
  `codex` profile as `primary` or in `fallback` (a temporary rule in `profile_registry.validate_registry`, one line
  to lift). Codex then runs only when a task names it explicitly. This keeps a claude->codex move from silently
  changing the restriction model (tools vs sandbox).
- **Not evasion**: fallback never retries a rate-limited attempt and never rotates credentials; a limited profile is
  left alone (ARCHITECTURE.md §9).

### 6. Cooldown triggers

What is actually known (checked in this repo):

- **Claude** (verified 2026-11-20 against claude 2.1.329 by earlier real runs): the stream contains a
  `rate_limit_event` message type (listed in `claude-cli-provider.md`), and the final `result` carries
  `is_error`, `subtype`, `terminal_reason` and `api_error_status` (observed `null` on a success). The adapter
  **ignores** unknown event types, so no payload of `rate_limit_event` is stored anywhere in the repo, and no real
  rate-limited `result` has been captured. **Unknown**: the shape of `rate_limit_event`, the values of
  `api_error_status`/`subtype` on a limit, whether subscription limits surface this way at all.
- **Codex** (0.170.0, fixtures in #93): the only failure captured is `turn.failed` with an `error.message` for an
  unsupported model (exit 1), preceded by a top-level `{"type":"error"}`. **No rate-limit or quota event has been
  observed**; there is no structured error code in anything captured.

Decision: **no automatic cooldown triggers in the first wiring.** Inventing a match on message text would
mis-cooldown profiles or miss real limits. The safe default is an operator command:

- `POST /api/v1/profiles/{id}/cooldown` with `{"seconds": N}` (0 < N <= 86400, matching `Cooldowns.set`) and
  `DELETE /api/v1/profiles/{id}/cooldown`; `agentctl profile-cooldown PROFILE --seconds N | --clear`. Each writes an
  audit event (`profile.cooldown_set` / `profile.cooldown_cleared`, with the operator principal). The `Cooldowns`
  object is owned by `AgentRuntime`; it is **in memory**, so a daemon restart forgets it (acceptable: an attempt
  against a still-limited profile just fails, and nothing retries); the audit events remain.
- **Procedure to add automatic detection later**, one provider at a time: when a real rate limit or quota failure is
  encountered, capture the raw terminal events (scrubbed) as a fixture, add a detector keyed on the structured
  fields seen, with a test that replays the fixture and a negative test on ordinary failures, then call
  `Cooldowns.set` with a conservative fixed duration. A detector only ever *removes the profile from selection*; it
  never retries the failed attempt.

### 7. Operator config format and migration

- **One loader entry point** in `daemon/agent_config.py`: `load_agent_config` stays for legacy; a new
  `load_operator_config(path, state_dir)` sniffs the top-level JSON: `profiles` present -> `profile_registry.
  load_registry`; `executable` present -> legacy via `load_agent_config` wrapped by `registry_from_single_profile`;
  both or neither -> `AgentConfigError`. Same file-safety rules (user-owned regular file, no symlink, not group/world
  writable, 64 KiB) in both paths.
- **Flag**: add `agentd --agent-config FILE` as the canonical name; keep `--claude-cli-config FILE` as a deprecated
  alias for the same loader (so existing systemd units and docs keep working). Giving both is an error. Rejected:
  a second flag for registry files only: it would leave two ways to configure the same repos/worktree root.
- **Legacy behaviour must not change**: a legacy file yields one profile, cap 2, no fallback, DB row
  `AGENT_PROFILE`; every existing agent test must pass unmodified (exit test).
- **Switching a deployment from legacy to registry**: history keeps pointing at `AGENT_PROFILE` (rows are never
  deleted); the registry creates new rows. A run in `running`/`recovering` across the switch is recovered with the
  provider set from decision 4, so it still reconciles.
- **Reload**: registry changes take effect on daemon restart only (no hot reload) in v1.

### 8. CLI and desktop surface

- API/CLI (operator Bearer): `GET /api/v1/profiles` (id, provider, model, enabled, `max_concurrency`, current load
  from the occupancy query, cooldown seconds remaining, `primary`/`fallback` position, and `probe()` status
  `healthy`/`unverified_version`/`unavailable` from #79/#93; no credentials, the executable path is shown to the
  operator only), `POST/DELETE .../cooldown` (decision 6), `agentctl profiles` and `agentctl profile-cooldown`.
  `agentctl agent-start --tasks-file` already carries per-task fields, so `profile` needs no new flag; add
  `--profile ID` and `--allow-fallback` for the single-task form.
- `GET /api/v1/runs/{id}/agent` and `/usage`: add `profile_id` (and provider via the profiles join) to each task and
  attempt.
- Desktop: show the selected profile on each task row, a small "profiles" panel (cap/load/cooldown/health) fed by
  `GET /api/v1/profiles` through the existing poller, and a profile selector in the run form. The desktop never sets
  cooldowns in v1 (CLI only) to keep the operator action explicit and auditable.

### 9. Security gates before Codex can be primary, in `fallback`, or enabled by default

Codex stays `enabled: false` unless the operator turns it on, and may be used only by a task that names it
(decision 5). Lifting the "not primary/fallback" rule requires **all** of:

1. **Independent security review of #93** (it is new, unreviewed code that spawns a process with the user's login).
2. **The `/tmp` finding is a permanent regression test**: `workspace-write` must refuse writes to a sibling `/tmp`
   directory and to `$HOME` (the opt-in real test exists in #93; run it on every Codex version bump and before
   release).
3. **Network claim verified**: `network_access=false` is stated, not demonstrated (`[UNVERIFIED]` in #93). Prove it
   with a real attempt to open a connection from inside `workspace-write`; if it does not hold, the docs and the
   registry must say so and the task class must be treated as having network.
4. **Read confinement of `read-only`** demonstrated or documented as absent (can the agent read `~/.ssh`, the real
   `~/.codex/auth.json`, other repos?). If it can, `read-only` is not equivalent to Claude's deny-list.
5. **Sandbox-unavailable behaviour**: show that Codex fails closed (no unsandboxed fallback) when the OS sandbox
   cannot start; the adapter must treat any such message as `failed`.
6. **Credential handling**: confirm a token refresh in the scratch `auth.json` cannot invalidate the real login
   (`[UNVERIFIED]`, #93 only warns); until then document "re-login may be needed".
7. **Hostile-prompt tests** (repo file or tool output instructing a write outside the worktree) on the real binary.
8. **Version policy**: only `VERIFIED_CLI_VERSIONS` (`0.170.0`) is `healthy`; an `unverified_version` profile is
   refused as primary/fallback. Implemented now: a spawn-time version gate refuses `workspace-write` on an unverified
   version (`codex_ineligible`) and logs each `read-only` spawn on one. Still a future gate: showing the probe result
   in `GET /api/v1/profiles` (there is no probe API surface yet).
9. **Approval binding**: decide whether the profile/provider must be part of an approval's binding (today the
   approval binds the prompt hash, operation, target and policy version, not the provider). Until decided, a gated
   task runs on the profile recorded when it was parked and is **never moved** on resume; if that profile is
   unavailable at resume the task stays parked or blocks, it does not switch provider.

### 10. Implementation plan: small PRs with exit tests

Merge order of the open PRs first (smallest risk and fewest conflicts first; each step re-runs the suite):

1. Docs/independent: #80, #83, #85, #94 (any order). #78 and #86 both edit `IMPLEMENTATION_BACKLOG.md`: merge one,
   rebase the other (#86 is evidence-based progress; fold #78's M4-M7 plan into it, don't overwrite either).
2. **#84, then #90** (security: tool restriction; #90 is stacked on #84's branch and edits the same adapter).
3. #79 (probe), then **#81, then #82** (both edit `api/agent_commands.py`, `api/app.py`, `cli/__main__.py`,
   `daemon/__main__.py`; rebase the later one).
4. **#88** (multi-task). It changes `AgentRunService._cleanup`, `validate`, `_queue_run` and `agent_commands`; rebase
   over #81/#82 and re-run the API tests. #92 then (migration 0014; the migration-count tests touch the same
   files as #82's tests, so rebase them too).
5. #91 and #93 (new modules, no overlap). **Not** before #84/#90 for #93, so the sandbox review sits on a settled base.
6. The security PRs #75-#77, #87, #89 are owned elsewhere and independent of this work; do not hold M4-B on them,
   but wiring item W1 touches `daemon/__main__.py`, which #75 to #77 also touch: rebase, don't force.

Then the wiring, in this order (each is a reviewable PR):

- **W0, capacity fix** (decision 2 finding): `claim_task` ignores attempts/tasks of finished runs. Exit: abandoned
  run frees its slot; existing `test_task_claim.py` cases unchanged; a new race test shows profile and global caps
  still hold.
- **W1, registry plumbing, Claude only**: `load_operator_config` + `--agent-config` (alias kept); registry -> `profiles`
  sync; optional `profile`/`allow_fallback` in specs and API (decision 1); engine `_profile_for` hook; router keyed by
  profile; `agent.profile_selected` event; `provider_kind` as a set. Exit: two Claude profiles with distinct fake
  executables; a 3-task DAG on caps 1 and 1 never exceeds either cap (assert max concurrent per profile from fake
  CLI timestamps); explicit profile honoured, default -> primary; unknown/disabled profile and tool outside the
  profile ceiling -> 422 with no run or worktree; legacy file: the entire existing agent test set passes unmodified;
  idempotent replay with and without `profile`; restart mid-run reconciles.
- **W2, fallback + cooldown + profiles API**: `select_profile` at claim/park with live load; `Cooldowns` in
  `AgentRuntime`; `GET /profiles`, cooldown POST/DELETE, `agentctl profiles|profile-cooldown`; audit events. Exit: a
  task falls back only when its profile is disabled/in cooldown/at cap and only before an attempt exists; an
  explicit profile does not fall back by default; after `unknown` no reselection ever; every fallback has an event;
  the cooldown command excludes a profile and clearing restores it.
- **W3, Codex behind the gates**: router builds `CodexCliProvider`; permission-mode and eligibility mapping
  (decision 4); registry rule "codex not primary/fallback"; usage pass-through; recovery with `{"claude","codex"}`.
  Exit: fake-executable tests for mapping, eligibility 422s and a mixed Claude+Codex DAG; opt-in real run
  (`SYNAPSE_REAL_CODEX=1`) of a mixed DAG through `scripts/demo_agent.py` with the `/tmp` regression included.
  Do not merge W3 without the independent review in gate 1.
  *Status 2026-11-23:* code and fake-executable exit tests (mapping, eligibility 422s, mixed Claude+Codex DAG,
  recovery, journal, probe) are in review (`tests/test_codex_agent_runs.py`); the opt-in real mixed-DAG run and the
  gate 1 review are **not** done.
- **W4, surfaces**: desktop profile panel/selector, run-view profile column, docs (`agent-runs.md`,
  `profile-registry.md`, new `agent-profiles.md`).
- **W5, later**: automatic cooldown detectors from captured fixtures (decision 6), Codex usage fields, lifting the
  cross-provider fallback rule after the gates in decision 9.

## Consequences

- Capacity stays enforced in one place (`claim_task`), so concurrency safety does not depend on new code paths.
- No migration is required for the wiring itself; the only schema-adjacent change is excluding finished runs from the
  occupancy count (a query change).
- Legacy deployments are unaffected; registry deployments get per-profile caps, auditable selection and an
  operator-controlled cooldown.
- Cross-provider behaviour is deliberately conservative: Codex cannot be selected implicitly until reviewed.

## Non-goals

Model/effort routing (M5.5), agent-to-agent messaging, budgets and caps by tokens (M5.2), automatic rate-limit
detection (W5), remote profiles, hot reload of the registry, and any retry after an attempt exists.

## Unverified assumptions and open questions

- `[UNVERIFIED]` the occupancy leak after `abandon` (derived from `claim_task`'s SQL, not run); W0 starts with a
  failing test.
- `[UNVERIFIED]` Claude `rate_limit_event` payload and `result.api_error_status` values on a real limit.
- `[UNVERIFIED]` any Codex rate-limit/quota event shape (none captured).
- `[UNVERIFIED]` Codex `read-only` read confinement, `network_access=false` effect, sandbox-unavailable behaviour,
  token refresh side effects, and how to pass a model.
- `[UNVERIFIED]` the legacy cap of 2 (#91 assumed it to match `AgentRuntime`'s default `max_concurrency`; it is the
  default of `AgentRuntime.__init__`, confirmed in code, not a configured value).
- Open: should a profile/provider be part of an approval's binding (gate 9)? Should a clean pre-spawn refusal fall
  back to another profile within the same task attempt count? Should cooldowns persist across restarts?
