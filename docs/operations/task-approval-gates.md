# Task approval gates

> **Security limit (read first).** Against code running as the same OS user as the daemon (including any agent child), approvals are **not** a security boundary: that code can read `approver.token` and `operator.token` and write the SQLite file directly, so it can forge or delete approvals and audit rows. The checks described here catch mistakes and honest components, not a hostile same-user process. See the [security review](../security/review-2026-11-20.md) (F1, F2). The daemon refuses to enable agent runs unless the operator passes `--allow-same-user-agents`.

A workflow step that needs approval parks only its own task in `awaiting_approval`. Sibling tasks keep running and the run stays `running`.

## Schema (migration 0012)

`task_approval_gates(task_id PK, approval_id UNIQUE, run_id, created_at, resolved_at)` links a task to exactly one approval. Triggers make rows immutable against accidental or buggy writes (any same-user process can still `DROP TRIGGER` or edit the file, see the note above) (no delete or replace; the only allowed update is the one-way `resolved_at` NULL -> value that consumes the gate), require the task to be `running` and the approval to be `requested` in the same run at insert, and reject any direct SQL change of `tasks.state` that bypasses the gate: `running -> awaiting_approval` needs a gate on a still-requested approval; `awaiting_approval -> running` needs an unresolved gate on an approved, unexpired approval; `awaiting_approval -> failed` needs an unresolved gate whose approval is rejected/expired/revoked, or approved/requested but past `expires_at`. A consumed gate authorizes nothing, so a task that was gated, resumed, verified and later enters the post-verification `awaiting_approval` is not affected by its old gate. `tasks.state` has no CHECK constraint, so no table rebuild is needed and existing rows are untouched. A task can be gated once in its lifetime; gating a second step of the same task needs a later migration.

## Transitions

`RUNNING -> AWAITING_APPROVAL -> RUNNING | FAILED | CANCELLED` (plus the pre-existing `VERIFYING -> AWAITING_APPROVAL -> COMPLETED | CHANGES_REQUIRED | CANCELLED`). The three gate-only edges are refused by `write_task_transition`; use `synapse.persistence.task_approval_gate`.

## Functions

- `write_task_approval_gate` / `park_task_for_approval`: parks a `running` task (optimistic version check, run `running`, approval `requested`, same run, unexpired, no unresolved attempt; finish the worker's attempt in the same transaction first). Writes the gate, `task.state_changed` and `task.approval_gated` events with outbox rows atomically.
- `write_task_approval_resolution` / `resolve_task_approval_gate`: rejected, expired or revoked approvals fail the task (`FAILED` is terminal, never auto-retried; dependents stay waiting under the normal rules). An approved approval resumes the task to `RUNNING` only if `binding_recheck_passed is True`, otherwise `BindingRecheckRequired` is raised and the task stays parked. An approved approval whose expiry has lapsed fails the task. A still-requested approval past its expiry also fails the task (`requested_lapsed`); an unexpired pending one raises `StateConflict`. Resuming requires the run to be `running`; failing is allowed in any non-terminal run state, and a terminal run refuses resolution. Resolution consumes the gate (`resolved_at`), and a consumed gate cannot be resolved again. A naive or unparsable stored `expires_at` raises `StateConflict`. Emits `task.approval_resolved`.

Both are compare-and-swap: a stale or concurrent call raises `StateConflict` without extra events. The wrappers own a `BEGIN IMMEDIATE` transaction; the `write_` forms join the caller's and work with `execute_idempotent`.

## Seam: execution-time binding recheck

This module does not verify candidate hash, operation, target, policy, expiry or approver at use time. The execution service must do that and pass `binding_recheck_passed=True` only after it succeeds. A resumed task has no unresolved attempt: the execution service must reserve a new attempt itself.

## Restart behaviour

Claiming and attempt reservation only accept `ready`/`assigned` tasks, so a parked task is never claimed or relaunched, and holds no worker slot. The mock engine ignores it (the run reports `blocked` while only parked tasks remain). `recover_mock_run` leaves a parked task parked and does not resolve decided gates; a parked task that still has an unresolved attempt aborts recovery as inconsistent. Cancellation may cancel a parked task; its approval stays `requested`. Expiry is driven by the daemon's expiry sweep, which fails the parked task in the same transaction (`approval-expiry.md`).
