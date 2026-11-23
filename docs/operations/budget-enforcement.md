# Budget enforcement in agent runs (M5.2 wiring)

What the agent engine (`AgentWorkflowEngine`, real-provider runs) does with an operator budget policy. The pure
policy is described in `budget-and-routing-policy.md`; the binding table and the claim-time evaluation are
ADR 020's first slice (`persistence/budget_binding.py`). This page covers the agent-engine rules added on top
(`orchestration/budget_enforcement.py`).

## Where the policy comes from

Operator config only: `agentd --budget-policy FILE` (already wired in `daemon/__main__.py`), passed to
`AgentRuntime(budget_policy=...)`, copied into `run_budget_bindings` when the run starts. Later edits to the file
affect new runs only. A run request cannot carry a budget: `AgentRunRequest` forbids unknown fields and a task
spec with any field outside the fixed set is refused with `AgentSpecError` before any state change. The engine
never reads budgets from a spec, a prompt or an agent.

No flag means no binding row, and an unbudgeted run behaves exactly as before: no budget events, no audit rows,
no extra refusals (`test_no_policy_means_no_budget_rows_and_unchanged_behaviour`; `tests/test_agent_runtime.py`
is unchanged and passes).

## When the check runs

Before a task is scheduled: inside the `claim_task` `BEGIN IMMEDIATE` transaction (after the capacity checks,
before the claim and before an attempt is reserved), at an approval-gate park, and at a gate resume before the
real attempt is reserved. Spawn happens later in the same scheduling pass, from the attempt that transaction
reserved. There is no second evaluation at spawn: the binding is immutable, usage arrives only when an attempt
ends, attempt caps count in-flight attempts, and a token-capped scope admits no claim while any attempt in it
is unresolved, so nothing that the check reads can change between the claim and the spawn.

Usage is measured only: totals come from `usage_observations` (provider-reported). Cost estimates and pricing
tables are never used. `max_cost_usd` and `max_wall_seconds` are refused when the policy file is loaded.

## Rules

| Situation | Decision | Reason |
| --- | --- | --- |
| every limited metric known and `observed <= limit` | spawn | (none; equal to the limit is allowed) |
| any `observed > limit` (attempts are checked as `used + 1`) | refuse | `budget_exceeded` |
| a limited token metric was not reported by some finished attempt in the scope | refuse (`on_unknown: deny`, the default) | `usage_unknown` |
| same, with `on_unknown: allow` in the operator file | spawn | |
| the binding or the usage ledger cannot be read, or the evaluation raises | refuse | `budget_check_failed` |
| a token-capped scope has an unresolved attempt | wait, re-check next pass | (no refusal) |

Unknown is never zero: a missing usage row is not added as 0, and an unknown metric only matters when a limit
applies to it. Scopes are `per_run`, `per_task` (the task's own earlier attempts) and `per_profile` (keyed by
profile id); the most severe decision wins.

**Fail closed on read errors.** `budget_binding.bound_policy` maps a failed read of the binding table to "no
policy" (meant for databases older than migration 0016). The agent engine does not use that path for the
decision: it reads the binding strictly, and any error while reading the binding or the usage rows refuses the
task with `budget_check_failed` instead of running it unbudgeted. If the database is too broken to record the
refusal either, the claim is rolled back and the engine stops with the error; nothing is spawned.

## What a refusal does

- The task is not spawned and no attempt is reserved.
- The task ends `failed` (agent engine; the mock engine keeps ADR 020's `blocked`). The state machine has no
  `ready -> failed` edge, so a task refused at claim or park is claimed without the budget hook and failed in the
  same transaction (`ready -> assigned -> failed`); no capacity is held after commit. A refused gate resume goes
  `running -> failed` after the gate is consumed, with no `approval.executed` audit row.
- One `budget.exceeded` event on the task (`task_id`, `reason`, `message`, `policy_version`, `outcome: "failed"`)
  and one `budget.denied` audit row whose details carry the decision: scope, limit name, limit value, observed
  value, unknown metrics, policy version. No prompt, tool input, path or spec field is recorded.
- Never retried: the agent engine does not retry, and `failed` is terminal.
- Dependents are cancelled and the run fails, by the engine's existing stranded-dependents logic.

## Policy version on spawn

Every attempt that is spawned in a budgeted run gets one `budget.policy_applied` event on its attempt
(`task_id`, `policy_version`, `policy_sha256` of the bound copy). It is written after the provider returns, with
the routing and manifest logs, on the same short-lock-wait best-effort transaction: a lost row is logged and
never changes the run. The decision itself was already taken, fail closed, at claim time. Unbudgeted runs write
nothing. No migration: it is an ordinary event row.

## Not covered

- In-flight overspend: usage is reported only when an attempt ends, so one running attempt can exceed a token
  limit; it is bounded by its timeout, not by the budget. A breach never kills running work.
- Cost and wall-clock limits, rolling windows, soft warnings (`budget.warning`) and `budget.unknown_ack`.
- An operator path to raise a limit for a running run: the binding is immutable; start a new run.
- Refused attempts of other kinds (for example a worktree recheck refusal) are failed attempts without usage,
  so they make token totals in their scopes unknown; with `on_unknown: deny` later claims in those scopes are
  refused (`usage_unknown`). This is fail closed but strict.

Tests: `tests/test_budget_enforcement.py` (under, at and over a limit, unknown with and without a limit and with
`on_unknown: allow`, run, task and profile scopes, park and resume refusals, never retried, dependents
cancelled, no config, usage read failure, unreadable binding, request payload, and a mutation check that the
over-budget scenario spawns when the check is removed).
