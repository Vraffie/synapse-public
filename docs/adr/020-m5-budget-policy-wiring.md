# ADR 020: Wiring the budget policy and usage ledger (M5.2)

Status: proposed (design only; no source, test or migration changes). Date: 2026-11-21. Follows ADR 017 (`docs/adr/017-m5-token-optimization-and-analytics.md`) and ADR 016 (`docs/adr/016-m4b-wiring-profiles-and-codex.md`).

Numbering: `main` has ADRs 001-017 and 019; open PR #119 adds `018-audit-hash-chain.md` and migration `0015_audit_hash_chain.sql`. This ADR takes **020**. Migrations proposed here use only the numbers reserved for this work stream, **0016-0018**; final numbers are assigned at merge time because the runner requires a contiguous sequence.

## Evidence

Read on `origin/main`: `src/synapse/application/budget_policy.py` and `routing_policy.py` (#100, pure, not wired; described in `docs/operations/budget-and-routing-policy.md`), `src/synapse/persistence/task_claim.py` (`claim_task`), `src/synapse/orchestration/agent_engine.py` (spawn path), `docs/operations/usage-ledger.md` (#92, migration `0014_usage_ledger.sql`), ADR 016 and ADR 017. Nothing was run. Anything not read is marked **unverified**.

Facts this ADR relies on:

1. `budget_policy.evaluate(policy, usage_totals, scope)` is pure and returns `allowed` plus a reason `within_budget | budget_exceeded | usage_unknown | usage_unknown_allowed`. Unknown is never zero; `on_unknown` defaults to `deny`; `per_profile` is a mapping keyed by profile id; `observed > limit` is a breach; `exhausted` means equal. There is no time window in the module: the caller decides which usage rows it passes.
2. `claim_task` runs inside the caller's `BEGIN IMMEDIATE` transaction, counts occupied slots (non-terminal attempts, plus assigned/running tasks without an attempt) against `max_running` and the profile's `max_concurrency`, and its docstring already says the application must evaluate "profile health, policy, resource locks and budgets" before it. It has no budget input today.
3. The engine's spawn step (`agent_engine.py`) already has a fail-closed pre-spawn refusal (worktree HEAD recheck, terminal `failed` with a reason, "refused before any spawn"). A provider reports usage only in its final result message, so usage lands at attempt end.
4. The ledger (`usage_observations`) is append-only, per attempt, with `NULL` for unreported fields and per-field coverage on its read API. `cost_usd` is the CLI's figure: an API-equivalent estimate on a subscription, not a charge.
5. `routing_policy.estimate_savings` takes a caller-supplied `{tier: {input_per_mtok, output_per_mtok}}` table and has no pricing source of its own.
6. Migration rules (ADR 017 facts 8 and 9): contiguous numbering, one transaction, DDL/DML only, every object created by a migration (the #75 backup fingerprint compares tables, indexes and triggers).

## D1. When the budget is checked

**Decision: authoritative check at claim time, inside the `claim_task` transaction; plus a read-only, stricter-only recheck at spawn time.**

- **Claim (authoritative).** The engine loads the run's bound policy, reads usage totals for the run, the task and the chosen profile with plain `SELECT`s in the same `BEGIN IMMEDIATE` transaction that `claim_task` uses, calls `evaluate_scopes([...])` with `attempts_used + 1` as the projected attempt count, and only then performs the claim `UPDATE`. A denial raises a distinct error (D4) and rolls the transaction back, so nothing is reserved. Because `BEGIN IMMEDIATE` serialises all writers, two engines, a restart or a gate resume cannot both pass the same check against the same totals. This is the argument ADR 016 decision 2 makes for capacity, and it is why the check is not done in memory before the claim.
- **What is reserved.** Nothing is stored as a counter (ADR 017: no mutable running total). A reservation is *derived*: every attempt in a non-terminal state (and every assigned/running task without an attempt, as the capacity CTE already counts) counts as one attempt used, and as `timeout_seconds` of projected wall-clock, in the scope totals. It is released automatically when the attempt becomes terminal, because the derivation stops counting it as in flight and its measured usage (if any) appears in the ledger instead. A claim that fails or rolls back leaves no residue.
- **Tokens cannot be reserved exactly**, because usage is known only at attempt end (fact 3). With K attempts in flight, a token cap can be overshot by up to K attempts' consumption. Recommended conservative rule: **when a scope has a token cap, refuse a claim while any attempt in that scope is in flight with usage not yet recorded**, so overshoot is bounded by exactly one attempt, the same single overshoot ADR 017 D4 already documents. This makes token-capped scopes effectively serial. Rejected as default: a configurable "reserve N tokens per in-flight attempt" (an invented number acting as enforcement); it may be offered later as an explicit, labelled policy field.
- **Interaction with the single-attempt capacity rule.** Capacity and budget are both checked in the one transaction; order is capacity first, then budget, so a capacity refusal (a normal "try next tick" `StateConflict`) is never confused with a budget refusal (terminal, D4). Budget never frees or takes a capacity slot.
- **Spawn (recheck, read-only).** Between claim and spawn the task sits in `assigned`; siblings may finish and add usage, or the run may sit parked at a gate. Immediately before `provider.execute`, next to the existing worktree recheck, re-run the same `evaluate` with the same bound policy and current totals **without** the +1 attempt (this task's attempt already counts). A denial ends the attempt `failed` with the budget reason, before any process exists, exactly like the existing pre-spawn refusals. The recheck can only be as strict as or stricter than the claim check, never looser.
- **Alternatives.**
  - *Spawn time only*: rejected as the sole check. It runs outside the claim transaction, so two engines can both pass it, and the slot is already consumed, so a refusal must undo an assignment.
  - *Claim time only*: rejected. The window between claim and spawn (a gate park or recovery can make it long) lets an exhausted budget still start a process. The recheck is cheap and read-only.
  - *Mid-flight kill on token overrun*: rejected (ADR 017); no in-flight usage source exists, and a guessed count acting as enforcement is an estimate acting as a decision.
- **Consequences.** `claim_task` gains one optional parameter (a pre-evaluated decision, or a callback that runs inside the transaction; the exact seam is **unverified** and left to the implementation PR). Existing callers and tests pass no policy and behave unchanged. Token-capped scopes lose parallelism by design.

## D2. Where policy lives

**Decision: operator-owned config file, bound immutably to each run in a new table. Not a mutable database table, not per-profile registry fields.**

- **Source of truth**: a JSON file read at daemon start (restart to change, as ADR 016 decision 7 does for the registry), with the same file-safety rules (user-owned regular file, no symlink, not group/world writable, size cap). Format is exactly `BudgetPolicy.to_json`, so `from_json` is the validator. Passed as `agentd --budget-policy FILE`. No flag means no policy, which means **no limits and no `budget.checked` events** (stated explicitly, see Q7). A malformed file stops daemon start; it never degrades to "no policy".
- **Per-profile limits** live inside the same policy under `per_profile` (the module already keys by profile id). A profile id absent from the registry is a start-up error (a typo must not silently mean "no cap").
- **Binding**: one migration (proposed **0016**) adds `run_budget_bindings(run_id PRIMARY KEY REFERENCES runs, policy_version, policy_sha256, policy_json, bound_at)`, append-only with `UPDATE`/`DELETE` triggers (pattern of `0014`/`0012`). The full `policy_json` is stored, not only a hash, so a run's decisions are reproducible after the file changes or is lost. The row is written in the transaction that creates the run's tasks; a run created while no policy is configured gets no row and is unbudgeted for life. Changing the file affects new runs only; a running run keeps its bound policy. Re-binding a live run is out of scope for the first slice (ADR 017 requires an audited operator action; that stays a later, separate command).
- **Alternatives.**
  - *Policy in a mutable database table*: rejected. It creates a second place to edit security-relevant limits that is neither reviewed as a file nor covered by file-permission checks, and a mid-run edit changes the rules for running work.
  - *Fields in the profile registry entry*: rejected for run/task scopes (they are not properties of a profile); acceptable only as a convenience for `per_profile` later. It would also couple the registry hash (`config_ref = registry:<sha256>`) to budget edits, so a cap change would look like a profile change.
  - *ADR 017's separate `budget_policies` versions table*: not needed once each run stores its own copy; dropped to keep the schema smaller. Two runs with the same `policy_sha256` have the same policy.
- **Migration implications**: one table, one migration, additive; nothing in `runs` is altered (hot table, fingerprint churn). It must apply from empty and from `0015`, pass the #75 fingerprint round trip, and contain no `PRAGMA` or transaction statements. A database restored from a backup keeps its bindings (they are rows). **0017** is reserved for `token_estimates` (D3), only if estimates are persisted in the first slice; **0018** is a spare and is released if unused. No number may be used before #119's `0015` and #92's `0014` have merged.

## D3. Pricing source, and keeping measured and estimated apart

**Decision: budgets are enforced on measured units only; pricing exists only to label estimates and is operator-declared, dated and flagged when stale.**

- **What a budget may limit**: tokens (by category as the provider reports them), attempts, wall-clock seconds and, if the owner allows it (Q2), `cost_usd` *as reported by the provider*. A cost cap therefore consumes the measured, per-attempt `cost_usd` from the ledger; Synapse never computes a price for enforcement. It is shown as "provider-reported, API-equivalent, not a charge". Coverage rules apply: a cost cap with `cost_usd` coverage below attempts is `usage_unknown` and obeys `on_unknown`.
- **Pricing table (estimates only)**: an optional `pricing` section in the budget policy file: `{"as_of": "YYYY-MM-DD", "source": "<free text>", "tiers": {tier: {"input_per_mtok": usd, "output_per_mtok": usd}}}`, the shape `estimate_savings` already accepts plus two provenance fields. It is hashed into the policy and so into `run_budget_bindings`. It is used only by `estimate_savings` and any "estimated cost" display. (Adding `pricing` means extending `BudgetPolicy`'s strict key check; that is an implementation change, noted here.)
- **Stale pricing**: an estimate is `pricing_stale = true` when `as_of` is missing, unparseable, in the future, or older than `max_age_days` (policy field, recommended default **90**). A stale estimate is still shown but carries the flag and the `as_of` date, and **nothing may make an enforcement decision from it**. Missing prices stay `estimated_usd = null` (unknown), as `estimate_savings` does already.
- **Alternatives.**
  - *Static table compiled into Synapse*: rejected. It goes stale silently on a release schedule the operator does not control and cannot be audited per run.
  - *Profile-declared prices (in the registry)*: rejected as the primary source. Prices belong to a model and a date, not to a local profile; one table avoids two profiles disagreeing about the same model. Per-profile overrides can be revisited once real use shows a need.
  - *Provider-reported cost as the only cost source*: kept, but only for measured cost. It cannot price a model Synapse did not run (the counterfactual in a savings estimate), so it cannot replace the table for estimates.
- **Measured versus estimated, rules for this wiring** (restating ADR 017 D1):
  - Measured values come only from `usage_observations` through the existing read path. Budget evaluation sees measured values only; there is no code path from an estimate into `usage_totals`.
  - Estimates are stored apart (proposed `token_estimates`, migration **0017**, schema as in ADR 017 D3 plus `pricing_sha256`, `pricing_as_of`, `pricing_stale`) or, in a first slice that persists nothing, computed on request. Either way every API field, CLI line and desktop widget that shows one carries the word "estimated", and responses use three disjoint sections `measured`, `estimated`, `unknown`.
  - Never summed, subtracted or netted: no total, view or widget combines the two. A routing decision's `estimated_usd` appears next to, never inside, measured cost.
  - Unknown stays unknown: a null is displayed as "unknown", and a lower-bound total is shown with its coverage.
- **Consequence**: a savings number can be wrong because the table is wrong; the flag, the `as_of` date and the dry-run default of routing are the mitigations, not a promise of accuracy.

## D4. Hard and soft limits; what happens on exhaustion

**Decision: every limit in the module is hard and fails closed; "soft" is only a warning surface that never changes a decision. No silent downgrade, ever.**

- **Hard**: `evaluate` returning `allowed = false` stops new work in that scope. Reasons map to distinct, stable failure reasons: `budget_exceeded` and `usage_unknown`. They are never `unknown`, never a generic provider failure, and never retried automatically (ADR 015 already says limits are not retried around). The refusal message names the scope, the limit, the limit value and the observed value (the module's `Breach` list), or for `usage_unknown` the limit whose metric is unreported, so an operator can act without reading code.
- **Where it surfaces**: a claim-time refusal leaves the task `ready` (nothing was assigned), with a `budget.exceeded` event and an `audit_entries` row (action `budget.denied`, principal = daemon, resource = scope, details = the decision JSON). The scheduler does not retry it; it stays blocked until an operator raises the cap (new run binding, D2) or acknowledges unknown usage. Whether an existing task state can express "blocked by budget" is **unverified**; if none exists, the run is failed with the budget reason rather than a new state being invented in this ADR (Q9). A spawn-time refusal ends the attempt `failed` with the same reason.
- **Soft (warning only)**: `exhausted` (observed equals limit) and a configurable warn fraction (recommended **80%**, Q6) emit `budget.warning` and show in the usage view. They change no decision and are the only soft thing in M5.
- **No silent downgrade**: budget exhaustion must not route to a cheaper model or tier, a smaller context, a lighter review or a different profile. `routing_policy` is a separate, dry-run-by-default input and must not read a `BudgetDecision` to pick a tier. If an operator wants cheaper work they change the policy or the task; Synapse does not do it for them. Budgets never skip verification, review or approval of already-produced work (ADR 017 D4).
- **Unknown usage**: `on_unknown = deny` (the module default) blocks new attempts in a scope when a limited metric is unreported. A per-attempt acknowledgement (`audit_entries` action `budget.unknown_ack`, principal and reason) clears that attempt's unknown status for budget purposes only; the ledger is never edited and the total stays labelled a lower bound.
- **Running work is never killed by a budget breach**; siblings finish and count. Wall-clock is bounded by the existing timeout only.
- **Alternatives rejected**: warn-and-continue as a default (a cap that stops nothing is not a cap); automatic fallback to another profile on breach (hides spend and may cross a boundary the operator did not choose); treating `usage_unknown` as allowed by default (understates spend exactly when something went wrong).
- **Consequences**: a single attempt with no reported usage can stall a capped scope until the operator acknowledges it. That is the intended conservative behaviour and its cost is operator attention.

## Migration summary (proposed; numbers final only at merge)

| number | content | needed in first slice |
| --- | --- | --- |
| 0016 | `run_budget_bindings` (append-only) | yes |
| 0017 | `token_estimates` (append-only) | only if estimates are persisted |
| 0018 | spare | no |

Prerequisites: #119 (`0015`) and #92 (`0014`) merged. At the time of writing the file lists of open PRs showed `migrations_sql/0015` only in #119 and no other claim on 0016 or later.

## Implementation slices (later, not part of this PR)

1. Policy loader and `--budget-policy`, binding table, test that a malformed file refuses start.
2. Claim-time check with unknown, lower-bound and serial-token-cap tests; spawn recheck; distinct failure reason, event and audit row.
3. Usage endpoint additions (limits and remaining, measured only) and an `agentctl usage` limits column.
4. Pricing section, stale flag, estimates endpoint with three disjoint sections.

Exit tests: a missing usage row never counts as zero; a breach cannot skip a gate or verifier; two concurrent claims cannot both pass a one-attempt cap; a token-capped scope never has two attempts in flight; an estimate never appears in measured totals; backup, verify and restore round trip with the new table.

## Non-goals

Mid-flight enforcement, quota inference, billing or chargeback, rolling per-profile time windows (the module has none, see Q5), LLM routing, wiring `routing_policy` (separate ADR, dry-run only), cache and output filtering (ADR 017), and any change to approval, gate or audit semantics.

## Unverified

- That `claim_task` can accept the budget check without restructuring (a parameter or callback inside the transaction is assumed).
- Which task state, if any, represents "blocked by budget".
- That `cost_usd` is reported on every attempt by every provider (the Codex usage shape is unverified per ADR 017).
- Whether Claude `input_tokens` includes cache categories (ADR 017 left this open); a `max_total_tokens` limit is only as meaningful as the provider's own total.
- Behaviour under real load; nothing was run for this document.

## Decisions needed from the owner

Reply with one line, for example: "1 ok, 2 ok, 3 ok, 4 ok, 5 ok, 6 ok, 7 ok, 8 ok, 9 ok". "ok" accepts the recommended default. Items 1, 2, 5 and 8 correspond to ADR 017 open questions 1, 2, 7 and 8; the rest are new. ADR 017 questions 3, 4, 5 and 6 are not answered here and stay open there.

| # | Question | Recommended conservative default |
| --- | --- | --- |
| 1 | `on_unknown` when a capped metric is unreported (ADR 017 Q1) | `deny` (fail closed), with per-attempt audited acknowledgement |
| 2 | Allow `cost_usd` caps at all (ADR 017 Q2) | No in v1: tokens, attempts and wall-clock only; revisit when cost coverage is complete |
| 3 | Token caps imply one attempt in flight per capped scope (D1) | Yes; accept reduced parallelism for an exact overshoot bound |
| 4 | Pricing staleness threshold for estimate labels (D3) | 90 days |
| 5 | Per-profile rolling windows before quota is knowable (ADR 017 Q7) | Not in v1; per-profile caps are totals per run only |
| 6 | Warn fraction for `budget.warning` (D4) | 80%, display only |
| 7 | No policy file means no limits (D2) rather than a built-in default cap | No limits; the alternative is a built-in safety net, for example 10 attempts per run |
| 8 | Ordering (ADR 017 Q8): M5 migrations wait for #92 and #119 and take the next contiguous numbers | Yes, number at merge time |
| 9 | A budget-blocked task: stays blocked until operator action, or fails the run (D4) | Blocked if an existing state can express it, otherwise fail the run with the budget reason |

Still open in ADR 017 with these defaults if you do not answer: Q3 agent-visible filtering (view-only), Q4 cacheable classes (none in v1), Q5 savings A/B baseline and pass-rate threshold (no savings claim without an owner-approved baseline), Q6 retention of raw outputs and cache entries (keep everything until M7.3).
