# Approval expiry sweep

`synapse.persistence.approval_expiry.expire_due_approvals(connection, *, now=None, limit=100, principal="daemon:expiry")` moves overdue `requested` approvals to `expired` and returns the ids it expired.

- Selection: `state='requested'` and `expires_at <= now` (UTC, boundary inclusive). Rows whose `expires_at` is naive or unparseable are skipped and logged at WARNING, never fatal. Approved, rejected, expired and revoked rows are never touched.
- Each approval is expired in its own short `BEGIN IMMEDIATE` transaction using `write_approval_transition(REQUESTED -> EXPIRED)` with compare-and-swap on `state_version`. The state change, the `approval.state_changed` event, its outbox row and an `audit_entries` row (`action='approval.expire'`, `decision='allowed'`, the given principal, no secrets) commit or roll back together.
- A `StateConflict` (decided concurrently, or not expired by the store's own clock check) is not an error: that approval is skipped. Other errors roll back and propagate. Reruns and concurrent sweepers are safe; a row can be expired once.
- `limit` bounds how many approvals one call expires, so each sweep holds the writer briefly. The caller must not already be in a transaction. The store's expiry check uses the real clock, so `now` only affects selection.

## Daemon hook

`run_expiry_sweeps(connection, interval, limit=100)` is an asyncio coroutine that sweeps every `interval` seconds until cancelled, logging and retrying after failures. The daemon runs it (every 30 seconds) alongside the server on the daemon's single connection and owning thread, via `_with_expiry_sweeps` in `synapse.daemon.__main__`; it is cancelled and awaited when the server stops. Each sweep is synchronous and bounded.

## Gated tasks

If a task is parked (`awaiting_approval`) on the approval being expired, the sweep fails it in the same `BEGIN IMMEDIATE` transaction as the expiry, by calling `write_task_approval_resolution` (the only writer allowed through the migration 0012/0013 triggers; no trigger is changed). The approval transition, the task failure, `task.approval_resolved`, the audit entry and the outbox rows commit or roll back together, so a failure to fail the task leaves the approval `requested` for the next sweep. Reruns, replays and restarts find nothing to do: the approval is no longer `requested` and the gate is consumed.

- Only parked tasks of non-terminal runs are touched. A terminal run still has its approval expired; its task is left alone because gate resolution refuses terminal runs.
- Each sweep also heals tasks still parked on an approval that is already `expired` (for example expired by the earlier sweep that did not fail tasks), one transaction per task.
- The sweep does not finish the run. The engine's next pass (or the daemon's park re-drive, see `workflow-approval-gates.md`) sees the failed task and completes the run.
- Approved approvals are not swept. An approved approval that lapses before the task resumes is failed by the engine's resume pass (`approved_lapsed`).
