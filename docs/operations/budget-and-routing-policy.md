# Budget and routing policy (pure modules; budget wired at claim time, routing not wired)

Two small, pure modules for M5.2 (budgets) and M5.5 (routing). Neither touches the database, the
clock, a provider or the engine; a later change wires them in. Nothing in the running system calls
them today.

- `synapse.application.budget_policy`
- `synapse.application.routing_policy`

## Budget policy

`BudgetPolicy(version, per_run, per_task, per_profile, on_unknown)` holds optional `BudgetLimits`:
`max_input_tokens`, `max_output_tokens`, `max_total_tokens`, `max_cost_usd`, `max_wall_seconds`,
`max_attempts`. `None` means no limit; `0` is a real limit ("nothing may be used"). Everything is
validated strictly (bools, NaN/infinity, negatives, wrong types and out-of-range values are
refused) and the policy round-trips through canonical JSON (`to_json` / `from_json`; duplicate keys
and `NaN`/`Infinity` constants are refused). `version` (`[A-Za-z0-9._-]`, at most 64 characters) is
recorded in every decision.

`evaluate(policy, usage_totals, scope=Scope("run" | "task" | "profile", id))` returns a
`BudgetDecision(allowed, reason, breached, unknown, exhausted, policy_version, scope)`.
`evaluate_scopes` combines several scopes: the most severe decision wins and an empty list is an
error (it never means "allowed").

| situation | `allowed` | `reason` |
| --- | --- | --- |
| every limited metric known and within its limit | yes | `within_budget` |
| any `observed > limit` | **no** | `budget_exceeded` (each `Breach(limit, limit_value, observed)` listed, sorted by limit) |
| a limited metric is unknown, `on_unknown="deny"` (default) | **no** | `usage_unknown` |
| a limited metric is unknown, `on_unknown="allow"` | yes | `usage_unknown_allowed` |

- A breach wins over an unknown. Breaches **fail closed**; nothing here retries anything.
- **Unknown usage is never zero.** `None` or a missing key is "not reported". A limit of `0` on a
  metric that was never reported is `usage_unknown`, not `within_budget`. `total_tokens` is the
  reported total, else input + output **only when both are known**; otherwise unknown. Garbage values
  (bool, negative, NaN, string) are a caller bug and raise `ValueError`; they are not "unknown".
- `wall_seconds` and `attempts` are always known to the daemon: a limit on them with no observed
  value raises `ValueError`.
- `observed == limit` is `exhausted` (listed, still allowed); only `observed > limit` is a breach. To
  ask whether another attempt may start, pass the projected count (`attempts_used + 1`).
- `usage_totals` keys: `input_tokens`, `output_tokens`, `total_tokens`, `cost_usd`, `wall_seconds`,
  `attempts`. `cost_usd` from a subscription CLI is an API-equivalent estimate, not a charge; a cost
  limit is only as good as that figure.

## Routing policy

`decide(RoutingInput(...), allowed_tiers=..., min_downgrade_confidence=0.6, min_upgrade_confidence=0.3,
dry_run=True)` picks a tier (`fast` < `balanced` < `deep`) and returns a `RoutingDecision(tier,
confidence, rules_fired, downgraded_from, reason, baseline_tier, applied, protected)`. Mapping a tier
to a concrete model is the profile's job. `confidence` is the confidence of the rule that moved the
tier (`0.0` when nothing fired); every rule that fired is named in `rules_fired`.

Rules (all deterministic; kinds are matched exactly, lower-case):

| rule | effect | confidence |
| --- | --- | --- |
| `hard_kind` (`architecture`, `security-review`, `migration`, `schema-design`) | up to `deep` | 0.9 |
| `large_context` (prompt >= 24576 bytes or >= 20 files) | up one tier | 0.7 |
| `retry_escalation` (`retry_no >= 1`) | up one tier | 0.6 |
| `small_mechanical` (`format`, `rename`, `summarize`, `lint-fix`, `docs`, `read-only-search`; prompt <= 4096 bytes, <= 2 files) | down to `fast` | 0.8 |
| `tiny_task` (not a hard kind; prompt <= 2048 bytes, <= 1 file) | down one tier | 0.5 (below the default threshold, so off unless the operator lowers it) |
| `review_or_gate_floor` (explicit review or approval gate) | at least `balanced` | n/a |

Guarantees (checked exhaustively over a grid of more than 100,000 inputs and over 40,000 seeded random
inputs including junk values):

1. The chosen tier is always in `allowed_tiers`. A requested tier outside it is clamped to the nearest
   allowed one (`clamped_to_allowed`; ties go higher).
2. A review, an approval-gated step or a retry is **never downgraded** by a rule. A missing or
   unparseable safety flag (`is_review`, `has_approval_gate`, `retry_no`) counts as protected, so it
   can only ever block a downgrade; only an explicit `True` review/gate raises a tier to the floor,
   and only when an allowed tier at or above `balanced` exists (`floor_unavailable` otherwise).
3. A downgrade needs `confidence >= min_downgrade_confidence`; an upgrade needs
   `confidence >= min_upgrade_confidence`. Upgrade evidence always beats downgrade evidence.
4. Missing or unparseable task signals (`None`, wrong types, negatives, bools) mean no change.
5. `dry_run=True` (the default) returns the decision with `applied=False`; the caller must not act on
   it. The decision is the same either way.
6. Operator configuration (`allowed_tiers`, thresholds) is validated strictly and raises `ValueError`;
   task signals are never trusted to be well-formed.

`estimate_savings(decision, pricing, expected_input_tokens=..., expected_output_tokens=...)` returns a
`SavingsEstimate` labelled `ESTIMATE (not measured usage)` with `measured=False`: the price difference
between the baseline and chosen tier for the caller's expected token counts. Missing or invalid prices
give `estimated_usd=None` (unknown), never `0`. Positive means cheaper than baseline, negative costlier.
It has no field compatible with the usage ledger and must never be added to measured totals.

## Budget wiring (ADR 020, first slice)

`agentd --budget-policy FILE` loads a `BudgetPolicy` JSON file at start (user-owned regular file, no symlink,
not group/world writable, 64 KiB cap). A bad file, an unknown key, a `per_profile` key that is not the built-in
agent profile, or a `max_cost_usd` / `max_wall_seconds` limit (not supported yet) stops the daemon with exit 2.
No flag means no limits. The file is copied into `run_budget_bindings` (migration 0016, insert-only) when the
run starts; later edits affect new runs only.

At claim time, inside the `claim_task` transaction and after the capacity checks, the bound policy is evaluated
against measured usage only. Every attempt row in the scope counts as one attempt used (in flight included) and
the claim is checked as `used + 1`. A terminal attempt that did not report a limited token metric makes that
metric unknown for its scope, and `on_unknown` (default `deny`) decides. A scope with a token cap refuses a
claim (retry next tick) while any attempt in it is unresolved. A denial leaves no claim, moves the task
`ready -> blocked`, and writes a `budget.exceeded` event and a `budget.denied` audit row with reason
`budget_exceeded` or `usage_unknown`. Nothing is downgraded or retried.

Approval-gated steps are checked twice, because an approved step is a real provider run:

- **At park**: `ApprovalGateCoordinator.park` passes the same check into its claim. A denial rolls the park
  back (no gate, no bookkeeping attempt) and the engine blocks the task `ready -> blocked` as above. (The
  approval request itself is created before the claim and is left `requested`; it has no gate and expires.)
- **At resume**: the check runs inside the resume reservation transaction, after the gate is consumed and
  before the real attempt is reserved. A denial commits the gate resolution, a `budget.exceeded` event, a
  `budget.denied` audit row and `running -> blocked` together: no attempt is reserved, no `approval.executed`
  audit row is written, no provider starts, and nothing retries. A serial-token-cap `StateConflict` leaves the
  task parked for the next pass.
- A resume denial emits `task.approval_resolved` (outcome `approved`, `awaiting_approval -> running`)
  followed by `budget.exceeded` and `running -> blocked`, all in one transaction, and **no**
  `approval.executed` audit row (the approval was never used). The consumed gate is not reusable; an operator
  unblock (`blocked -> ready`) goes through the normal claim path and is budgeted again.
- The gate's own bookkeeping attempt (the wait attempt park created, completed with no provider run) is left out
  of every scope: it is neither an attempt used (it does not count toward `max_attempts`) nor unknown usage
  (no provider ran, so none can exist). It is identified exactly by its id, recorded as `bookkeeping_attempt_id`
  in the immutable `task.approval_gated` event, never by attempt number. A real attempt that ran before the
  gate existed always counts, and a gate recorded without the id (older database) leaves its wait attempt
  counted (fail closed). The engine's own `max_attempts` still counts the wait attempt, as before.

## Not implemented

- Wiring (beyond the claim-time budget check above): nothing rechecks at spawn, and nothing routes a task. The
  intended points are the scheduler's claim step (budget) and spawn-time profile/model choice (routing).
- Persisting policies or decisions (no table, no event), operator config for them, an API or CLI.
- Mid-run enforcement: a provider reports usage only when it ends, so a running attempt is bounded by
  its timeout, not by a token budget.
- Per-model pricing sources; `estimate_savings` only uses a table the caller supplies.
- An LLM judge, intentionally. A judge call costs tokens, needs its own budget and is hard to test;
  revisit only if dry-run logs show these rules are not enough.
