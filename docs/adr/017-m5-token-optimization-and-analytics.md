# ADR 017: M5 token optimization and analytics

Status: proposed (design only; no source changes). Date: 2026-11-20. Follows ADR 015 (`docs/adr/015-m4b-parallel-agents-and-profiles.md`, PR #85) and ADR 016 (`docs/adr/016-m4b-wiring-profiles-and-codex.md`, PR #95).

Evidence base: `origin/main` at the time of writing, plus the **unmerged** PRs named below, described as of their heads and to be re-checked on merge. Anything I did not read is marked **unverified**. The M5 item list (M5.1-M5.7) comes from the plan in PR #78 (`IMPLEMENTATION_BACKLOG.md`, still open); this ADR does not edit the backlog.

| PR | Piece | Used here as |
| --- | --- | --- |
| #75 | backup schema fingerprint | restore/verify rules new tables must satisfy |
| #78 | M4-M7 plan | defines M5.1-M5.7 and the exit criterion |
| #88 | multi-task runs | many attempts per run to budget |
| #91 | profile registry, cooldowns | profile ids for per-profile caps |
| #92 | usage ledger (migration `0014_usage_ledger.sql`) | the measured ledger (M5.1 and ADR 015 B2 are the same work) |
| #95 | ADR 016 | profile wiring; ledger row = `usage_observations` |

## Context: what exists today (read before deciding)

1. **Claude usage surfaces only in the final `result` message.** `ClaudeCliProvider._parse` (`src/synapse/providers/claude_cli.py`, ~line 610) handles `assistant` (text and `tool_use` names, never tool inputs) and `result`; every other type (`system`, `user`, `rate_limit_event`, ...) is ignored. On `main` it reads only `usage.input_tokens` and `usage.output_tokens` into `UsageObservation(input, output, "claude-cli")` (`src/synapse/providers/contracts.py`). The real binary also reports `usage.cache_read_input_tokens`, `usage.cache_creation_input_tokens` and top-level `total_cost_usd` in that `result` (`docs/operations/claude-cli-provider.md` ~line 112, claude 2.1.329); `main` drops them. **PR #92** adds them to `UsageObservation` with strict parsing (non-negative ints, finite non-negative float, anything else is `None`, nothing recorded if no field is usable). There is exactly one `result` per attempt (a second is a parse error), so usage is **per attempt, end of attempt only**: a killed, timed-out or `unknown` attempt reports nothing. No per-message or in-flight usage is consumed.
2. **`cost_usd` is not spend.** On a subscription the CLI figure is an API-equivalent number. #92's `docs/operations/usage-ledger.md` already says so; this ADR keeps that.
3. **Recording on `main`**: `AgentWorkflowEngine._usage_event` (`src/synapse/orchestration/agent_engine.py`, ~line 124) writes only an `agent.usage_observed` event plus its outbox row, in one transaction. The table `usage_observations` (`src/synapse/persistence/migrations_sql/0001_initial.sql`, line 115; columns `attempt_id`, `profile_id`, `source`, token columns, `reported_total_tokens`, `confidence`, `observed_at`, `source_event_id`, `UNIQUE(attempt_id, source_event_id)`) exists but is never written on `main`. #92 adds `cost_usd`, append-only triggers, the same-transaction insert, `GET /api/v1/runs/{run_id}/usage` and `agentctl usage`. `quota_observations` (0001, line 123) also exists, unused; quota stays unknown.
4. **Tables reserved for M5 already exist, unused** (0001, lines 128-135): `context_manifests(id, task_attempt_id, manifest_json, manifest_sha256, created_at)` and `optimization_decisions(id, task_attempt_id, policy_version, decision_json, created_at)`. Neither has immutability triggers. Neither is referenced from `src/` (checked with grep).
5. **"Context CAS" in M2 is compare-and-swap, not content addressing.** `write_context` (`src/synapse/persistence/context_store.py`) is a versioned compare-and-swap over `context_documents`/`context_revisions` (0001, lines 75-85) for scopes `task` and `run`. The content-addressed machinery is the **artifact blob store**: `ArtifactBlobStore.put/read` (`src/synapse/infrastructure/artifact_blobs.py`) names blobs by SHA-256, re-verifies the hash on read and bounds size; `artifacts(id, sha256, media_type, byte_size, storage_path, provenance_json, created_at)` (0001, line 86; immutable by 0005) and `publish_artifact` (`src/synapse/persistence/artifact_store.py`) store metadata, event and idempotent response atomically; messages reference artifacts through immutable `message_artifacts` (0004). Agent task prompts live in private blobs referenced from a hashed `agent.tasks_configured` event (`src/synapse/persistence/agent_tasks.py`); per-attempt summaries are published as artifacts (`agent_engine.py`, `MAX_SUMMARY_TEXT_BYTES = 16 KiB`).
6. **Approval and audit model.** Task-level gates park a running task in `awaiting_approval` (`src/synapse/persistence/task_approval_gate.py`, migration 0012); approvals bind operation, target, candidate manifest hash, policy version and expiry (`src/synapse/persistence/approval_binding.py`, `approval_requests` in 0001); decisions and denials are written to `audit_entries(principal, action, resource, decision, correlation_id, details_json)` (`src/synapse/application/approval_service.py`). Full-spec binding and approver-visible subject are in open PR #77 (**unverified** here). Invariants in `docs/architecture/ARCHITECTURE.md` section 13 that bind this ADR: 5 (an approval is invalid after any relevant candidate or policy change), 7 (unknown quota is displayed as unknown), 9 (verification identifies its exact candidate), 10 (token optimization cannot bypass mandatory checks).
7. **Worktree head** is obtainable from `WorktreeManager` (`src/synapse/infrastructure/worktrees.py`, a `head: str` field around line 47); exactly which call returns it for an existing worktree is **unverified**.
8. **Migration runner rules** (`src/synapse/persistence/migrations.py`): one `BEGIN IMMEDIATE` transaction for all pending files; the sequence must be **contiguous from 1** (a gap or duplicate is an error); a database containing unknown versions is refused; DDL/DML only (an authorizer denies transactions, savepoints, `ATTACH`, `PRAGMA`). So new M5 migrations cannot pick a number until #92 (`0014`) and any other open migration PR have merged; number at merge time.
9. **Backup fingerprint (PR #75, open, not on `main`)**: `schema_fingerprint` applies migrations `1..version` to a scratch in-memory DB and compares every table, index, trigger and view (whitespace-normalised SQL) with the backup (`src/synapse/persistence/backup.py` on branch `fix/security-f8-f9-f10-f11-f13`). Consequences: every M5 object must be created by a migration file (nothing created at runtime), triggers are part of the fingerprint, and `ALTER TABLE ... ADD COLUMN` changes the stored SQL text (fine, since the scratch DB gets the same). Backups cover the database only (`docs/operations/backup-restore.md`); artifact blobs are not included until M7.3, so a restored database can reference missing blobs. **Unverified**: how #75 treats `limit=` for migrations newer than the backup.

## Decision

### D1. Two ledgers that never meet: measured and estimated

- **Measured** = a number the provider reported for one attempt. It is provider-reported, not independently verified (`confidence = 'reported'`, as in #92). Stored only in `usage_observations`. Unknown is `NULL`/absent, never 0. Totals sum known values only, return `null` when no row knows the field, and always ship `attempts_with_usage` / `attempts_total` so a total is visibly a lower bound (all as in #92's `usage_query.py`).
- **Estimated** = anything Synapse computes about tokens it believes it avoided or would have used. Stored only in a separate table (`token_estimates`, D3) with `method`, `baseline_ref`, `assumptions_json` and `confidence IN ('heuristic','ab_measured')`. Estimates carry a `basis` count in bytes or characters unless a provider tokenizer is proven (none is today; **unverified**), so the unit is `est_tokens` with `chars/4`-style heuristics explicitly named in `method`.
- **Hard separation rules**: no SQL view, API field, `agentctl` command or desktop widget may add an estimate to a measured number, subtract one from the other, or label an estimate "saved" without the word "estimated". The usage endpoint stays measured-only; a new endpoint carries estimates and always returns `{"measured": ..., "estimated": ..., "unknown": ...}` as three disjoint sections. A savings figure as a *claim* needs an A/B baseline (M5 exit criterion); heuristic-only figures are displayed as "estimated, unvalidated".
- **Cache token categories are not merged.** `input`, `output`, `cache_read` and `cache_write` are kept as the provider reports them; no derived "total tokens" (same stance as #92, which leaves `reported_total_tokens` unset). Whether Claude's `input_tokens` includes or excludes cache categories is **unverified**; any "tokens per task" metric must define its formula in the policy version and never double-count.
- **Provenance**: each observation records `source` (`claude-cli`, later `codex-cli`), `profile_id`, the `agent.usage_observed` event id, and (new, D3) the adapter/CLI version string if the probe from #79 supplies one (**unverified** that it does). Rows from different CLI versions are not comparable until fixtures exist per version.
- **Unknown quota** (invariant 7): no code in M5 derives quota or remaining budget from usage. The only quota-shaped data is `rate_limit_event` messages, currently ignored by the parser. M5 may record them to `quota_observations` with `confidence = 'reported'` only after a fixture proves their shape on the real binary; until then quota is `unknown` everywhere, including in budget decisions (D4).

### D2. Event types

Events are append-only facts inside the same transaction as the row they describe (ARCHITECTURE invariant 2). New types (payloads follow the existing `EventEnvelope`/`encode_payload` rules and never contain prompt text, tool inputs or file contents, only ids, hashes and counts):

- `agent.usage_observed` (exists; #92 extends the payload with cache fields and `cost_usd`). Not renamed.
- `budget.checked` (decision and the numbers compared), `budget.exceeded` (distinct failure reason, D4).
- `context.manifest_recorded` (manifest hash, entry count).
- `cache.lookup` (`hit|miss|bypass`, key hash, reason) and `cache.stored`. A hit must name the originating attempt id.
- `output.filtered` (raw artifact id, reduced artifact id, rule ids, counts of dropped lines by class).
- `optimization.estimated` (estimate row id, method, est_tokens). Never emitted into a stream consumers use for usage totals.

Analytics must be **reproducible from the event log** (M5.7): every analytic number is a fold over these events plus `usage_observations`; derived tables are caches that can be rebuilt (D3).

### D3. Data model and migration approach

Use the reserved tables where they fit; add new ones only where they do not. All through numbered migration files (D-rule from fact 9), written to the next free contiguous numbers after #92 merges, each starting with a comment explaining purpose, in the style of `0012_task_approval_gates.sql`.

- `context_manifests` (exists, 0001): populate as-is, add append-only triggers (the pattern in `0014`/`0012`). `manifest_json` lists entries `{kind, ref, sha256, byte_size, range?}`; `manifest_sha256` is the hash of its canonical encoding (reuse `canonical_request_hash` from `src/synapse/persistence/idempotency.py`, **unverified** that it is suitable for this payload). Entries reference artifacts by artifact id plus sha256, never inline content.
- `optimization_decisions` (exists, 0001): populate as-is, append-only, `policy_version` mandatory. Records every dedup/filter/cache/route decision with its inputs hash, whether applied or dry-run.
- `budget_policies` (new): `version`, `policy_json`, `sha256`, `created_by`, `created_at`, immutable. A run records the policy version it started under (a column on a new `run_budget_bindings(run_id PRIMARY KEY, policy_version, policy_sha256)` table, not `ALTER TABLE runs`, to avoid touching hot tables).
- `token_estimates` (new, D1): append-only, FK to attempt (nullable for run-level), `kind IN ('dedup','filter','cache_hit','routing')`, `method`, `est_tokens`, `baseline_ref`, `assumptions_json`, `confidence`.
- `result_cache` (new, D6): `key_sha256 PRIMARY KEY`, `entry_json`, `result_artifact_id`, `origin_attempt_id`, `created_at`, `invalidated_at`; the only permitted update is the one-way set of `invalidated_at` (the pattern in `task_gate_no_update`).
- Do **not** add a mutable "running total" column anywhere. Derived views over append-only rows only.
- **Compatibility checklist** for each migration PR: contiguous number; applies from empty and from the previous schema; passes the #75 fingerprint round trip (backup, verify, restore) in a test; no `PRAGMA`/transaction statements; existing rows untouched (`NULL` for new columns means unknown); triggers named consistently so fingerprint diffs are readable. A schema change to `usage_observations` after #92 is avoided; add siblings instead.
- Retention: usage and estimate rows are small and kept; raw filtered outputs live in the blob store (D7) and are subject to the M7.3 artifact-retention decision (open question Q6).

### D4. Budget policy semantics

- **Scopes**: per attempt (wall-clock, already `timeout_seconds` in the task spec), per task (attempt count), per run (tokens, attempts, wall-clock) and per profile (concurrent already enforced by `claim_task`; add a rolling window of tokens or attempts, window length in the policy). Budget limits are expressed only in units the provider reports (tokens by category, attempts, seconds, and optionally `cost_usd` as an *API-equivalent* figure, labelled so). No budget is expressed in "quota", which is unknown.
- **When checked**: before claim/spawn (a check in the same `BEGIN IMMEDIATE` transaction family as `claim_task`, **unverified** that adding a check there is the cleanest seam) and after each attempt ends (usage lands, then the next claim sees it). Usage arrives only at attempt end, so a single running attempt can overshoot a token cap by up to its own consumption; the design says so plainly. The only in-flight enforcement is the wall-clock timeout that already exists. M5 does not kill attempts for token overrun mid-flight (no in-flight usage source).
- **Fail-closed on unknown**: when a cap is configured and the relevant counter is **not knowable** (an attempt in the scope ended `unknown`, crashed, or reported no usage, so `attempts_with_usage < attempts_total`), the check does **not** treat the missing usage as zero. Policy default `on_unknown = 'block_new'`: no new attempt is started in that scope until an operator explicitly acknowledges (an audited decision, `audit_entries` action `budget.unknown_ack`, with principal and reason) or raises the cap. `on_unknown = 'allow'` can be set per policy, recorded in its hash, and shown wherever totals are shown. Rationale: the cost of a stuck run is an operator click; the cost of silently unlimited spend is the thing budgets exist to prevent. An attempt count and wall-clock cap are always knowable, so they stay enforceable even when tokens are not.
- **Breach**: `budget.exceeded` with reason `budget_tokens|budget_attempts|budget_wallclock|budget_unknown`, task or run to a distinct failed state/reason (not `unknown`, not retried; ADR 015 already says rate/quota responses are never retried to evade limits, and this follows it). Running siblings are not killed by a breach; they finish and count.
- **Budgets never reduce safety work.** A breach may stop new work; it must not skip verification, reviews or approval gates of already-produced work (invariant 10). A policy cannot name approval steps, verifiers or reviewers as skippable.
- **Policy changes** apply to new claims only; a run keeps its bound `policy_version`, and changing a policy mid-run requires an explicit operator action recorded in `audit_entries`.

### D5. Context manifests and deduplication (M5.4, first half)

- A manifest is computed by the daemon at claim time from sources the daemon already owns: the immutable spec hash (`agent.tasks_configured`), the repo base SHA and worktree head, requirement refs, and artifact ids with their sha256. It is stored in `context_manifests` and its hash goes into the attempt's event stream. The agent is **not** trusted to declare its own manifest.
- **Deduplication** means the daemon avoiding re-sending identical content *it controls* (the prompt it builds, the artifacts it attaches), by referencing a blob once. It does not mean editing what the agent sees of the repository, and it does not rewrite or summarise requirement text, approver-visible subjects, or policy text. Anything that an approval binds to (candidate manifest hash, spec) is never deduplicated away from the approver's view.
- On the Claude adapter, the prompt goes to stdin (hardening in `claude_cli.py`); manifests therefore change what the daemon *writes* into that prompt, not what the CLI loads itself. The CLI's own prompt caching and context loading are not under Synapse control and are not claimed as savings (they appear, if at all, only as measured `cache_read_tokens`).

### D6. Exact-cache safety (M5.4, second half)

What **may** be cached: results of **deterministic, side-effect-free derived work whose inputs Synapse fully controls and hashes**: the reduced form of a given raw output (D7), manifests, and read-only analysis answers explicitly marked `cacheable` by an operator-owned policy. Default cacheability for a task is **no**.

What **must never** be cached or served from cache:
- any task that wrote, or was allowed to write, files, ran `Bash` or any tool with side effects (the `allowed_tools` and `permission_mode` in the spec decide; unknown tool = not cacheable);
- reviews, verifications and anything whose purpose is an independent check (invariants 9, 10; M5.5 in the plan says reviews never downgrade and no semantic reuse for edits or reviews);
- anything that passed through, or was gated by, an approval; and anything whose answer depends on live state (network, clock, secrets);
- results whose original attempt ended `unknown`, `failed`, `cancelled` or was budget-blocked.

Key (all components are hashed into `key_sha256`; any change is a different key, never a partial match):
`policy_version` + `cache_schema_version` + provider/profile identity and **CLI version** + model (if known) + effective argv shape (tools, permission mode, settings hash) + **exact spec hash** (the sha256 over the task spec, `agent_tasks._sha`-style, including the prompt blob hash) + manifest hash (D5) + **worktree head SHA and a clean-tree marker** (dirty or unknown head = no cache) + hashes of validators in play. Keying on the prompt or spec alone is insufficient (identical prompt on a different repo state must miss).

**Approval boundary (invariant 5)**: a cache hit is itself a result that stands in for an attempt, so it is subject to the same gates. Rules: (a) if the spec has an `approval_step` (task gate) the cache is bypassed (`cache.lookup` reason `approval_gated`); (b) an approval can never be satisfied, extended or inherited by a cache hit; the approval binding recheck at execution time (`approval_binding.py`) is untouched and runs on whatever is about to take effect; (c) a result served from cache is stamped `origin_attempt_id` and is **not** a verification result for the new candidate: verifiers re-run on it as for any other output (invariant 9); (d) any change to policy version, approval policy or candidate hash changes the key, so an entry created before an approval-relevant change cannot match afterwards.

Invalidation: policy version bump, CLI version change, worktree head change (part of the key, so automatic), explicit operator invalidate (audited), and a TTL ceiling in the policy. Entries are invalidated, never deleted or rewritten. Tests required before enabling: a changed head, changed spec, changed tool list, changed policy and a gated task each produce a miss; a hit never occurs for a write-capable task; restored-from-backup databases start with all cache entries invalidated (blobs may be missing, fact 9).

Semantic or fuzzy caching is out of scope (M8 in the plan, only after an evaluation).

### D7. Output filtering and its risks (M5.3)

- The reducer is deterministic and non-LLM. It operates on **Synapse-held** output (the bounded stream the adapter already collects and the 16 KiB summary), never on what the approver is shown.
- **Raw output is always preserved** as an artifact (blob store) and referenced from the reduced form; filtering is lossy only in the *view*, never in storage. `output.filtered` records the raw artifact id so any reduction is reversible and auditable.
- **Never hidden from approvers**: approver-visible subject material (commands, targets, diffs, candidate manifests, paths, `permission_denials` from the `result` message), denials, errors, non-zero exits, and any line matching the security-relevant classes below are exempt from dropping, truncation and dedupe-collapse, and are shown verbatim in approval views. The filter has an **allow-never-drop** list owned by policy, not by the agent.
- Security-relevant classes that must pass through: permission denials, tool errors, stderr, anything mentioning credential paths or env names, writes outside the worktree, network access, and messages about hooks or settings.
- Dedupe collapses only byte-identical consecutive or repeated lines and always prints the count (`xN`); it keeps the first and last occurrence and every distinct failure. A reducer that fails or meets unparseable input returns the raw output untouched (fail open for visibility, the inverse of the budget rule, deliberately).
- Risks to design against: collapsing distinct secrets-adjacent lines; truncating the tail where a failure usually is; reducing before the secret redactor runs (reducer runs after redaction, and `ARCHITECTURE.md` section 13 lists secret redaction tests); a prompt-injected agent crafting output so a malicious line looks like a repeat. A golden corpus including adversarial near-duplicates is mandatory (M5.3 in the plan). Because the reducer touches only the *view* and stored raw data is complete, a reducer bug degrades readability, not evidence.
- Filtering that changes what an *agent* receives as input in a later task is a different, riskier feature; it is out of scope for the first slice (Q3).

### D8. Quality guardrails

- The headline metric is **tokens per independently verified task**, computed only from measured usage joined with `verification_results` (0001, line 102) for the same candidate; estimated savings are shown beside it, never inside it.
- A guardrail comparison: for any optimization (filter, cache, manifest trimming), compare verified-pass rate and retry count with and without it over a minimum sample; an optimization whose verified-pass rate drops beyond a policy threshold is auto-disabled (`optimization_decisions` record, audited) and cannot re-enable itself.
- Optimizations run in **dry-run** first: they compute what they would have done and record an estimate, but do not alter any agent-visible input, until the owner enables them (M5.5 in the plan already specifies dry-run routing).
- Mandatory checks are not parameters of any optimization: no policy field can skip a verifier, reviewer, approval gate or binding recheck.
- Model/tier routing (M5.5) and compaction (M5.6) are out of the first slice and need their own ADR; this ADR only requires that they log through `optimization_decisions` and never downgrade reviews or approval-gated steps.

## First vertical slice (build this first)

M5.1 + the read side of M5.2 + analytics-lite, because #92 already provides the ledger, the event and the read API:

1. Land #92 and make the Claude usage parse and the ledger the single source of measured usage. Record `rate_limit_event` shape as a fixture only (no behaviour).
2. Add `budget_policies` + `run_budget_bindings` (one migration), a **pure** `budget.evaluate(policy, usage_rows, attempt_counts) -> allow|block(reason)` with explicit `unknown` handling and unit tests for the unknown/lower-bound cases, and wire it into claim time for **attempt-count and wall-clock caps and token caps with `on_unknown = 'block_new'`**. Distinct failure reason, audit entry, never retried.
3. Add `token_estimates` (one migration) and a **dry-run** exact-output reducer for the stored 16 KiB summary view only, writing `output.filtered` + estimates, with raw preserved, and a `GET .../savings` endpoint returning the three disjoint sections. No cache, no manifest-driven prompt changes yet.
4. `agentctl usage` shows measured, plus a separate `agentctl savings` clearly labelled estimated.
- Exit: tests prove (a) missing usage is never summed as zero and blocks new attempts under `block_new`; (b) estimates never appear in measured totals or endpoints; (c) backup, verify and restore round trip with the new schema (needs #75); (d) filter never drops approver-visible classes (golden corpus); (e) a budget breach cannot skip a gate or verifier.

Then, in order: M5.4 manifests (record only), exact cache for the narrow cacheable class (D6) behind an operator flag, analytics views (M5.7), and only with data, routing (M5.5) and compaction (M5.6) under their own ADRs.

## Alternatives rejected

- **One table with a `measured|estimated` flag.** Rejected: one wrong `SUM()` mixes them, and it would break the append-only measured ledger and #92's contract. Separate tables with disjoint API sections make mixing a visible code change.
- **Treat unknown usage as zero (or as the average).** Rejected: it understates spend exactly when something went wrong, and contradicts invariant 7 and ADR 015 B2.
- **Mid-flight token cutoffs from stream parsing.** Rejected for now: the verified stream gives usage only in the final `result`; cutting off on guessed counts would be an estimate acting as enforcement.
- **Content-addressed cache keyed on the prompt only.** Rejected: ignores repo state, tools, policy and approval boundary.
- **LLM-based output summarisation or routing judge in M5.** Rejected for the first slice: non-deterministic, un-auditable against the allow-never-drop rule, and spends the tokens it is meant to save.
- **Mutating existing tables (`runs`, `usage_observations`) for M5 state.** Rejected: hot tables, fingerprint churn and conflicts with open migration PRs; sibling tables instead.

## Risks and consequences

- Totals are lower bounds whenever coverage is partial; the UI/CLI must show coverage or it will mislead.
- Token caps can overshoot by one attempt; documented, not hidden.
- Heuristic estimates drift from real tokenisation; they are labelled and never validated as savings without A/B data.
- A fail-closed budget can stall a run on a single `unknown` attempt; mitigated by the audited operator acknowledgement and `on_unknown` policy field.
- New migrations serialise behind #92 and any other open migration PR (contiguous-number rule, fact 8); backups from before the migration verify only through the #75 fingerprint (fact 9).
- The cache is a new path for serving stale or cross-boundary answers if its class rules are loosened; hence default-off, narrow class and the required miss tests.

## Non-goals

Semantic caching, LLM routing or judging, context compaction/handoff (M5.6, own ADR), changing what the CLI loads or caches itself, quota estimation, monetary billing/chargeback, per-message streaming usage, Codex usage parsing (gated on ADR 015 B4 evidence; **unverified** schema), blob-store backup and retention (M7.3), and any change to approval, gate or audit semantics.

## Open questions for the owner

1. **Fail-closed default**: confirm `on_unknown = 'block_new'` for token caps, or prefer `allow` with a visible warning for a single-user local setup.
2. **Cost caps**: allow budgets in `cost_usd` at all, given it is an API-equivalent estimate on a subscription, or limit caps to tokens, attempts and wall-clock?
3. **Agent-visible filtering**: is filtering what a later agent receives (not just what humans see) ever acceptable, or must it stay view-only?
4. **Cacheable classes**: is any task class (for example read-only analysis with a pinned head) worth caching at all in v1, or should the cache stay off until M5.7 shows repeats?
5. **Savings claims**: what A/B baseline is acceptable (same task run twice, historical runs, a fixed benchmark set), and who sets the pass-rate threshold in D8?
6. **Retention**: how long to keep raw outputs and cache entries, and should the M7.3 blob-backup decision be pulled forward since a restored database can reference missing blobs?
7. **Profile windows**: should per-profile token windows exist before real quota is knowable, or wait for ADR 016's cooldown data?
8. **Numbering and ordering**: confirm M5 migrations wait for #92 and the ADR 016 wiring rather than renumbering around them.
