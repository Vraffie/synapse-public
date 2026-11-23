# Usage ledger

Provider-reported usage is recorded durably, per attempt, and can be read back per run. It is **provider-reported,
not independently measured**, and **unknown is never stored or shown as zero**.

## What is recorded

For every real agent attempt whose provider result carried usable usage, the engine writes, in one transaction:

- an `agent.usage_observed` event (payload: `task_id`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_write_tokens`, `cost_usd`, `source`), plus its outbox row, and
- one row in `usage_observations` (`attempt_id`, `profile_id`, `source`, the five numeric fields,
  `confidence = 'reported'`, `observed_at`, `source_event_id` = the event id).

Migration `0014_usage_ledger.sql` adds `cost_usd` to the existing (previously unused) table and makes it
append-only with triggers (no `UPDATE`, no `DELETE`, `cost_usd >= 0`). Rows written before the migration keep
`cost_usd = NULL`.

The Claude CLI adapter reads the final `result` message (verified against claude 2.1.329):

| ledger field | stream-json source |
| --- | --- |
| `input_tokens` | `usage.input_tokens` |
| `output_tokens` | `usage.output_tokens` |
| `cache_read_tokens` | `usage.cache_read_input_tokens` |
| `cache_write_tokens` | `usage.cache_creation_input_tokens` |
| `cost_usd` | top-level `total_cost_usd` |

Parsing is strict: token fields must be non-negative integers no larger than 2**63-1 (what SQLite stores;
booleans are rejected), `cost_usd` a finite number between 0 and 1e12. Anything else, including an oversized value,
is `None` for that field only: the other fields of the same record are still recorded. If **no** field is usable
(no `usage` object, or all fields invalid), nothing is recorded at all: no event, no row. Losing usage never fails
or blocks a task.

`cost_usd` is the figure the CLI reports. On a subscription it is an API-equivalent **estimate**, not a charge, and
should not be presented as spend. Quota is not recorded (it stays unknown; no reliable source exists).

## Reading it

`GET /api/v1/runs/{run_id}/usage` (operator Bearer) or `agentctl usage RUN [--limit N] [--cursor C]`; 404 for an
unknown run. One SQLite read snapshot per request. `observations` is one page (`limit` 1-500, default 200, ordered
by `(observed_at, id)`; follow `next_cursor` until it is null), while `totals`, `coverage`, `attempts_total` and
`attempts_with_usage` are computed over the whole run on every page, so they do not change from page to page unless
new usage is recorded in between:

```json
{"run_id": "...", "attempts_total": 3, "attempts_with_usage": 2,
 "observations": [{"id": "...", "attempt_id": "...", "task_id": "...", "profile_id": "...", "source": "claude-cli",
                   "confidence": "reported", "observed_at": "...", "input_tokens": 2, "output_tokens": 4,
                   "cache_read_tokens": 3474, "cache_write_tokens": 3200, "cost_usd": 0.0131914}],
 "totals": {"input_tokens": 2, "output_tokens": 4, "cache_read_tokens": 3474, "cache_write_tokens": 3200,
            "cost_usd": 0.0131914},
 "coverage": {"input_tokens": {"known": 2, "attempts": 3}, "output_tokens": {"known": 2, "attempts": 3},
              "cache_read_tokens": {"known": 1, "attempts": 3}, "cache_write_tokens": {"known": 1, "attempts": 3},
              "cost_usd": {"known": 1, "attempts": 3}},
 "next_cursor": null}
```

- A field an observation did not report is `null` in that observation.
- A total sums only the known values; if no observation knows a field, that total is `null`, not `0`. Token totals are exact integers (no 64-bit overflow, summed in Python rather than SQLite `SUM()`); the cost total is `math.fsum` of the known costs, correctly rounded and independent of row order or SQLite version.
- **Coverage is per field.** `coverage.<field>.known` is the number of attempts that reported that field and
  `coverage.<field>.attempts` the run's attempt count (attempts that crashed, ended `unknown` or carried no usage
  count in `attempts` and contribute nothing). A total is a **lower bound exactly when `known < attempts`** for
  that field; with `known == attempts` it is complete. Example: attempt 1 reports tokens and cost, attempt 2 only
  tokens: `attempts_with_usage` is 2 of 2, yet `cost_usd` coverage is `known: 1, attempts: 2`, so the cost total is
  partial.
- `attempts_with_usage` (attempts with **any** field) is kept for compatibility. It cannot show a gap in one
  field; use `coverage` for that.
- A reported `0` is a known value and counts toward `known`.

## Not done here

Budgets and caps, per-profile aggregates, and analytics views build on this ledger (M5). Cache categories are
reported as the provider gives them and are not merged or converted into a single "total tokens" figure
(`reported_total_tokens` stays unset).
