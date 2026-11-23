# Budget enforcement in agent runs (M5.2 wiring)

This page covers what the agent engine (`AgentWorkflowEngine`, real-provider runs) does with an operator budget
policy. The pure policy is described in `budget-and-routing-policy.md`. The binding table and the claim-time
evaluation are ADR 020's first slice (`persistence/budget_binding.py`). This page covers the agent-engine rules
added on top (`orchestration/budget_enforcement.py`).

## Where the policy comes from

The policy comes from operator config only. `agentd --budget-policy FILE` (already wired in
`daemon/__main__.py`) passes it to `AgentRuntime(budget_policy=...)`, and it is copied into
`run_budget_bindings` when the run starts. Later edits to the file affect new runs only.

A run request cannot carry a budget. `AgentRunRequest` forbids unknown fields, and a task spec with any field
outside the fixed set is refused with `AgentSpecError` before any state changes. The engine never reads budgets
from a spec, a prompt or an agent.

No flag means no binding row. An unbudgeted run behaves exactly as before: no budget events, no audit rows, no
extra refusals (`test_no_policy_means_no_budget_rows_and_unchanged_behaviour`; `tests/test_agent_runtime.py`
is unchanged and passes).

## When the check runs

The check runs before a task is scheduled, in three places:

- inside the `claim_task` `BEGIN IMMEDIATE` transaction, after the capacity checks and before the claim and
  before an attempt is reserved;
- at an approval-gate park;
- at a gate resume, before the real attempt is reserved.

The spawn happens later, from the attempt that this transaction reserved. There is no second evaluation at
spawn. That is safe only because of these facts:

- The binding is immutable.
- Attempt limits count every attempt in the scope, including the ones still running.
- A token-capped scope admits no claim while any attempt in it is unresolved. Once a claim passes, no other
  attempt in that scope can finish and add usage before this one does.
- An attempt's usage row is written in the same transaction that makes the attempt terminal. Before this change
  the usage row was written later, in its own transaction. A claim that ran between the two saw a finished
  attempt with no usage, which counts as "unknown". With `on_unknown: allow` it could then spawn although the cap
  was already exceeded. `test_finished_attempt_usage_is_visible_to_the_next_claim_*` covers that window.

If any of these facts changes (for example cost or wall-clock limits, which have other sources), a spawn-time
recheck is needed. ADR 020 D1 asks for one; the ADR owner should amend D1 or ask for it to be added.

Usage is measured only: totals come from `usage_observations` (provider-reported). Cost estimates and pricing
tables are never used. `max_cost_usd` and `max_wall_seconds` are refused when the policy file is loaded.

## Rules

| Situation | Decision | Reason | Task ends |
| --- | --- | --- | --- |
| every limited metric known and `observed <= limit` | spawn | | |
| any `observed > limit` (attempts are checked as `used + 1`) | refuse | `budget_exceeded` | `failed` |
| a limited token metric was not reported by some finished attempt in the scope, `on_unknown: deny` (default) | refuse | `usage_unknown` | `blocked` |
| same, with `on_unknown: allow` in the operator file | spawn | | |
| the binding or the usage ledger cannot be read, or the evaluation raises | refuse | `budget_check_failed` | `blocked` |
| a token-capped scope has an unresolved attempt | wait, re-check on the next pass | | stays `ready` |

Unknown is never zero. A missing usage row is not added as 0, and an unknown metric matters only when a limit
applies to it. The scopes are `per_run`, `per_task` (the task's own earlier attempts) and `per_profile` (keyed
by profile id). The most severe decision wins.

Only a breach is terminal. Unknown usage and an unreadable budget keep ADR 020's `blocked` (ADR 020 Q9: blocked
when an existing state can express it). The run then goes to `recovering` for an operator, and the planned
operator acknowledgement path (`budget.unknown_ack`) stays open. This choice and the absence of a spawn-time
recheck need ADR 020 to be amended by its owner.

**Fail closed on read errors.** `budget_binding.bound_policy` maps a failed read of the binding table to "no
policy" (meant for databases older than migration 0016). The agent engine does not use that path for the
decision. It reads the binding strictly, and any error while reading the binding or the usage rows refuses the
task with `budget_check_failed` instead of running it unbudgeted. A database without the `run_budget_bindings`
table therefore refuses every claim of the agent engine, budgeted or not. The daemon always migrates first, so a
missing table means something removed it.

If the database is too broken to record the refusal either, the whole refusal transaction rolls back. The task
stays `ready` with no attempt and no event, nothing is spawned, the engine stops with the error and the run is
routed to recovery (`test_refusal_that_cannot_be_recorded_rolls_back_and_spawns_nothing`).

## What a refusal does

- The task is not spawned and no attempt is reserved.
- One `budget.exceeded` event is written on the task, with `task_id`, `reason`, `message`, `policy_version` and
  `outcome` (`failed` or `blocked`).
- One `budget.denied` audit row is written. Its details carry the decision: scope, limit name, limit value,
  observed value, unknown metrics, policy version. No prompt, tool input, path or spec field is recorded.
- **Breach (`budget_exceeded`):** the task ends `failed`. The agent engine never retries, and `failed` is
  terminal. Dependents are cancelled and the run fails, through the engine's existing stranded-dependents logic.
  - The state machine has no `ready -> failed` edge. A task refused at claim or park is therefore claimed
    without the budget hook and failed in the same transaction (`ready -> assigned -> failed`).
  - Both of those `task.state_changed` events carry the principal `agent-engine.budget-refusal`, so the
    `assigned` step is never mistaken for a real assignment.
  - No capacity is held after commit (`test_a_refused_claim_holds_no_capacity`).
  - A refused gate resume goes `running -> failed` after the gate is consumed, with no `approval.executed`
    audit row for that task.
- **Unknown usage or an unreadable budget:** the task goes to `blocked` (`ready -> blocked` or
  `running -> blocked`), as in ADR 020. The run goes to `recovering`.
- The mock engine keeps ADR 020's behaviour for every reason (`blocked`).

Gate refusals are tested end to end through `AgentRuntime` for the resume path
(`test_gate_resume_refusal_end_to_end_through_the_runtime`: two gated tasks, the second approved after the first
spent the cap) and for the park path. The mock gate harness covers the resume path again.

## Policy version on the attempt

In a budgeted run, the engine writes one `budget.policy_applied` event for each attempt it handed to the provider
(`task_id`, `policy_version`, `policy_sha256` of the bound copy). It is written best-effort after the provider
returns, together with the routing and manifest logs, under the same short lock wait.

- An attempt whose provider start was rejected also gets one.
- An attempt interrupted by a daemon kill gets none.
- A lost row is logged and never changes the run (`test_policy_version_log_failure_never_changes_the_run`).
- The authority is the immutable run binding (`run_budget_bindings`, one row per run). The event is a
  convenience for readers of the attempt.
- Unbudgeted runs write nothing. No migration is needed: it is an ordinary event row.

## Not covered

- **In-flight overspend:** usage is reported only when an attempt ends, so one running attempt can exceed a
  token limit. It is bounded by its timeout, not by the budget. A breach never kills running work.
- **Other limit types:** cost and wall-clock limits, rolling windows, soft warnings (`budget.warning`) and the
  `budget.unknown_ack` operator path.
- **Raising a limit for a running run:** the binding is immutable; start a new run.
- **Other pre-spawn refusals are strict:** an attempt refused for another reason (for example the worktree
  recheck) is a failed attempt without usage. It makes token totals in its scopes unknown, so later claims there
  are blocked with `usage_unknown` (with `on_unknown: deny`). This fails closed, but it is strict.
- **A usage row that cannot be written:** an ordinary failure (constraint, oversized value) is rolled back alone
  (savepoint) and logged; the attempt still reaches its outcome and its usage is unknown, never zero
  (`test_usage_write_failure_keeps_the_outcome_and_records_nothing`). If SQLite itself has already aborted the
  transaction (I/O error, disk full), the terminal write fails too and the engine's error path records the attempt
  `unknown` and blocks the task, which also fails closed.
- **`on_unknown: allow` can hide a known overspend:** for each scope, a token metric is unknown as soon as any
  finished attempt in it lacks that metric, and the known part of the sum is then discarded
  (`budget_binding._scope_totals`, existing ADR 020 code, not changed here). Example: one attempt reported 5000
  input tokens against a 1000 limit, another failed without usage; with `allow` the next claim passes as
  `usage_unknown_allowed`. With the default `deny` it is blocked. Until the evaluation keeps a known lower bound,
  treat `allow` as "do not enforce token limits once any usage is missing".
- **`max_total_tokens` is derived:** the agent engine never writes `reported_total_tokens`, so the total is
  input + output when both are known for every finished attempt in the scope, and unknown otherwise.

Tests are in `tests/test_budget_enforcement.py`. They cover:

- under, at and over a limit;
- unknown usage with and without a limit, and with `on_unknown: allow`;
- run, task and profile scopes;
- park and resume refusals (resume end to end);
- never retried, and dependents cancelled;
- no config;
- usage read failure and an unreadable binding;
- a refusal that cannot be recorded;
- capacity after a refusal;
- the record-only log failing;
- request payload;
- the usage-visibility regression (deny and allow, with and without a slow raw-output store);
- a mutation check: the over-budget scenario spawns when the check is removed.
