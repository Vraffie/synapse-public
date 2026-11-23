# Side-effect ledger (M7 deployment reconciliation)

Library only (`synapse.persistence.side_effect_ledger`, ADR-021). No deployment code, no network, not wired into the daemon.

## Usage contract

```
entry = record_intent(conn, StepKey(task, attempt_no, candidate_sha256, "deploy"))
if entry is Entry.PROCEED:
    run_step()                      # the only path that executes the effect
    record_completion(conn, key)    # idempotent
elif entry is Entry.ALREADY_DONE:   # treat as success, do not run
else:                               # IN_FLIGHT / UNKNOWN / NOT_DONE: do not run
```

Call `ensure_schema` once at startup (idempotent). All functions need an autocommit connection (`open_database`) and manage their own `BEGIN IMMEDIATE`.

## Recovery and reconciliation

1. After acquiring daemon ownership and establishing that no worker survives, call `recover_side_effects(conn, no_workers_survive=True)`. Every intent without completion becomes `unknown`; nothing is replayed.
2. An operator inspects the target environment and calls `resolve_unknown(conn, key, outcome="done"|"not_done", actor=..., evidence=...)`, or a read-only probe is run via `reconcile_with_probe`. Inconclusive/failing probes keep the step `unknown`.
3. `done`: downstream treats the step as complete. `not_done`: the same key stays closed; a retry uses `attempt_no + 1`.

Every transition is recorded in `side_effect_ledger_transitions` (append-only, source worker/recovery/operator/probe, actor, evidence).

## Wiring plan (not done)

- Promote `SCHEMA` to migration 0017 (after 0015/0016 merge) and drop runtime `ensure_schema`.
- Daemon startup: call `recover_side_effects` next to `recover_mock_run`, under the ownership lock.
- Deployment executor (future): wrap each irreversible step as above, with the candidate hash taken from the approved manifest and rechecked by the approval-binding execution-time check (`approval-binding.md`).
- Local API/CLI: list `unknown` steps, an authenticated operator resolve command (reuse approver identity), event emission for each transition.

## Limits

The ledger cannot make a crash between the external effect and `record_completion` safe; it makes that window visible as `unknown`. Operator identity is not authenticated here. Tests: `tests/test_side_effect_ledger.py`.
