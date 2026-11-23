# ADR-021: Side-effect ledger and no blind replay

Status: Proposed (ADR-022 adds environment to the key; library implemented in `src/synapse/persistence/side_effect_ledger.py`; not wired into the daemon). Numbered 021 (020 is the XML dependency index ADR).

## Context

ADR-012 makes interrupted work observable (`BLOCKED`, attempts `unknown`) but explicitly defers reconciliation of external effects. Retrying a deployment step whose outcome is uncertain can duplicate an irreversible effect. Task-level state cannot say whether a step inside an attempt had already acted.

## Decision

Before an irreversible step runs, the daemon records an intent row keyed by `(task_id, attempt_no, candidate_sha256, step)` in a `side_effect_ledger` table (plus an append-only transition log). States: `intent`, `done`, `unknown`, `not_done`.

- Only the caller whose insert created the row (`Entry.PROCEED`) may run the step. Every other outcome (`IN_FLIGHT`, `UNKNOWN`, `NOT_DONE`, `ALREADY_DONE`) means do not run. Concurrent claims are serialized by `BEGIN IMMEDIATE` and the primary key.
- Crash recovery turns every `intent` without completion into `unknown`. Recovery never replays and never guesses.
- `unknown` leaves only through an explicit operator resolution or an injected read-only probe returning done/not_done. An inconclusive or failing probe leaves it `unknown`. A worker cannot complete an `unknown` step.
- `not_done` does not reopen the same key. A retry is a new attempt number (a new key) created by an explicit operator/application decision. The candidate hash is part of the key, so a different candidate never inherits another candidate's record.

## Alternatives and consequences

Storing the marker on `task_attempts` was rejected: a task can have several irreversible steps. Auto-retry with idempotency keys was rejected because the external system's idempotency cannot be assumed. Consequence: a crash mid-step needs a human (or a trusted probe) before progress, trading availability for safety. The resolver identity is recorded but not authenticated by this library; the caller must authorize operators (see approver identity docs). The schema is created by `ensure_schema` for now and must become migration 0017 once 0015/0016 land (the migrator requires a contiguous sequence).
