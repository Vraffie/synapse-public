# Analytics views (M5.7, slice 1: library)

`synapse.application.analytics_views` is a read-only read model for one run: attempts, retries, outcome mix, messages
and token/cost totals. This slice is library only; there is no API route, CLI or desktop view yet. Authorization is the
caller's job, as for `usage_query.py`.

## The three kinds

Every number is a `Quantity(kind, value, unit, basis, known, of, lower_bound)`.

| kind | meaning | `value` |
| --- | --- | --- |
| `measured` | `basis="provider_reported"`: tokens and cost a provider reported (ledger `confidence='reported'`, not independently verified; `cost_usd` is API-equivalent, not spend). `basis="daemon_record"`: an exact count of the daemon's own rows or events. | number |
| `estimated` | Derived by Synapse from `optimization.estimated` events; `basis` is the method name and the unit is `est_tokens`. | number |
| `unknown` | Nothing reported. | `None`, never 0 |

Estimates live only in the `estimated` section (`method -> est_tokens`). They are never added to, subtracted from or
folded into `usage`; an estimate for an attempt with no measured usage leaves that usage unknown. No estimate table or
emitter exists yet (ADR 017 D3), so the event type is the only source and both paths read it the same way; events with a
missing method, a negative or non-integer `est_tokens`, or an attempt not in the run are counted in
`ignored_estimate_events` and not summed.

## Coverage rule

As in `usage_query.py`, coverage is per field: `known` = distinct attempts that reported the field, `of` = attempts in
scope (run, task or profile). `lower_bound` is true exactly when a value exists and `known < of`: the total omits the
unknown attempts. An unknown total is not a lower bound (`lower_bound=False`). Counts of daemon records are exact
(`known == of`). Cost is summed with `math.fsum` so the result does not depend on row order.

## What is in a view

Run level: `attempts`, `retries` (attempts beyond the first per task), `outcome_mix` (final attempt state),
`usage` (five ledger fields), `attempts_with_any_usage`, `estimated`, and `messages`: `sent`, `deliveries_created`,
`claimed` ("delivered": leased to a recipient at least once), `redeliveries` (claims beyond the first), `acknowledged`,
`dead_lettered`. Per task (paged): attempts, retries, outcome mix, usage. Per profile (tables only): attempts and usage.

## Reproducibility rule

Two paths share one pure builder and differ only in their source:

* `read_run_analytics(connection, run_id, limit=, cursor=)` reads the tables.
* `replay_run_analytics(...)` folds the run's events in `sequence` order (`task.attempt_reserved`,
  `task_attempt.*` state events, `agent.usage_observed`, `optimization.estimated`, `message.created`, `delivery.*`).

`reproducibility_diff(tables, replay)` lists differing fields and must be empty on a healthy database (tested on a
populated one). Not reproducible from events, declared in `RunAnalytics.not_reproducible` and typed rather than hidden:

* `per_profile`: neither attempt nor usage events carry a profile id (replay returns `None`).
* `messages.deliveries_created`: delivery rows are created without an event (replay returns `unknown`).

A replay also reports `replay_ignored_events`: events it could not attribute to a reserved attempt (never counted).
The run's existence is checked in the `runs` table on both paths (identity, not a metric); an unknown run raises
`RunNotFound`.

## Bounds and safety

Each call opens its own read transaction (`RuntimeError` if one is already open) and never writes. The task list is
keyset-paged by task id (`limit` 1-200, default 50, opaque cursor, `InvalidCursor` on a bad one); totals always cover
the whole run, not the page. Hard caps (`MAX_ATTEMPTS` 20 000, `MAX_OBSERVATIONS` 100 000, `MAX_EVENTS` 200 000) raise
`AnalyticsTooLarge` rather than truncating a total; per profile is capped at 200 with `per_profile_truncated`.

## Not covered

No API, CLI or desktop surface; no cross-run or per-profile-over-time aggregation; no tokens-per-verified-task metric
(needs `verification_results` joins); no estimate producers; no quota (stays unknown, ADR 017 D1); no cache-hit or
manifest analytics; time-based filtering.
