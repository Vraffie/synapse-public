# Invariant coverage (ARCHITECTURE.md section 13)

Which tests assert each of the ten critical invariants, and what is not covered. The map lives in
`tests/test_invariant_coverage.py` as data, and that file fails if a named test is renamed or removed
or if this page stops listing an invariant. It proves the named tests *exist*; whether they pass is the
job of the suite. Companion to [fault-injection.md](fault-injection.md) (the harness) and
[fault-injection-tests.md](fault-injection-tests.md) (the SIGKILL suite).

Statuses: `covered` (asserted by tests in or beside the harness), `partial` (some aspect untested, stated
below), `outside-harness` (covered only by unit or security tests, not by a fault scenario).
Every scenario also runs the database checker `tests/invariants.py` afterwards.

## 1. single authoritative daemon writer

Status: `covered`.

- `test_daemon_lock.py::test_lock_rejects_concurrent_owner_and_releases_on_close`
- `test_daemon_lock.py::test_lock_releases_when_owner_process_exits`
- `test_fault_injection_processes.py::test_a_killed_lock_owner_releases_ownership_and_loses_only_its_uncommitted_write`
- `test_fault_injection_workers.py::test_a_second_writer_holding_the_lock_fails_a_start_cleanly`

## 2. state change and event/outbox commit atomically

Status: `covered`.

- `test_invariants.py::test_detects_event_without_outbox_row`
- `test_invariants.py::test_detects_outbox_row_without_event`
- `test_fault_injection_workers.py::test_disk_full_on_artifact_write_leaves_no_partial_state`
- `test_fault_injection_messaging.py::test_duplicate_event_rows_are_rejected_and_replay_is_stable`

## 3. messages immutable, delivery at least once

Status: `covered`.

- `test_invariants.py::test_every_required_trigger_exists_on_a_healthy_database`
- `test_fault_injection_messaging.py::test_lease_expiry_redelivers_and_clock_jumps_never_steal_a_live_lease`
- `test_fault_injection_messaging.py::test_a_publisher_that_crashes_before_the_ack_redelivers_the_same_event_ids`
- `test_fault_injection_messaging.py::test_exhausted_deliveries_dead_letter_instead_of_looping`

## 4. side effects idempotent or reconciled

Status: `covered`.

- `test_side_effect_ledger.py::test_crash_between_intent_and_completion_becomes_unknown_never_replayed`
- `test_fault_injection_workers.py::test_worker_killed_mid_attempt_becomes_unknown_and_is_never_respawned`
- `test_fault_injection_workers.py::test_resume_after_a_crash_launches_exactly_one_new_attempt_and_keeps_every_invariant`

## 5. approval invalid after a candidate or policy change

Status: `covered`.

- `test_fault_injection_approvals.py::test_a_decision_bound_to_a_changed_candidate_or_policy_is_refused_and_launches_nothing`
- `test_fault_injection_approvals.py::test_parked_approval_survives_a_crash_and_expires_only_forward_in_time`

## 6. no capability elevation by message or prompt

Status: `outside-harness`.

- `test_message_gateway.py::test_untrusted_fields_size_invalid_json_and_capability_are_rejected`
- `test_session_authority.py::test_credentials_bind_identity_and_exact_capabilities`
- `test_message_service.py::test_forged_and_cross_run_sessions_cannot_read_or_complete`

Not covered: security-style unit tests, not a fault scenario; no end-to-end test that a real agent message cannot widen its tools.

## 7. unknown quota displayed as unknown

Status: `partial`.

- `test_usage_ledger.py::test_partial_usage_keeps_unknown_fields_none_not_zero`
- `test_budget_wiring.py::test_missing_usage_is_unknown_never_zero`
- `test_invariants.py::test_detects_usage_row_that_reports_nothing`

Not covered: storage and budget side only; what the desktop and CLI show for unknown is not asserted here.

## 8. desktop disconnection cannot cancel work

Status: `covered`.

- `test_fault_injection_clients.py::test_dropped_clients_never_cancel_a_parked_run_and_approval_still_finishes_it`

Not covered: real daemon, dropped HTTP and WebSocket clients; a killed Qt process is not simulated, only its socket behaviour.

## 9. verification identifies its exact candidate artifact

Status: `covered`.

- `test_invariants.py::test_detects_verification_without_candidate_artifact`
- `test_invariants.py::test_detects_verification_with_different_candidate_hash`
- `test_invariants.py::test_detects_verification_not_bound_to_an_attempt_of_its_task`

## 10. token optimization cannot bypass mandatory checks

Status: `partial`.

- `test_output_reducer.py::test_pytest_failures_survive_a_tight_budget`
- `test_output_reducer.py::test_failures_beyond_the_budget_are_kept_and_reported_over_budget`
- `test_budget_wiring.py::test_approved_gated_step_is_blocked_at_resume_and_never_runs`

Not covered: the reducer and budget are unit-tested; no fault scenario drives reduction or caching through the engine until the M5.3 wiring merges.

## Invariant 8 scenario

`tests/test_fault_injection_clients.py` starts a real daemon, parks a gated run on its approval, then
makes five rounds of a half-sent authenticated HTTP request and a WebSocket event-stream client that is
dropped without a close frame. It asserts the daemon still answers, the run stays `running` with its task
parked, no `*cancel*` event or audit row exists, and a human approval afterwards still finishes the run, then
runs the database invariant checker on the quiescent result. A Qt process killed outright is not simulated;
its observable effect on the daemon is exactly these dropped sockets.

## Remaining gaps

- Invariant 6: no end-to-end test that an agent message cannot widen the agent's own tools.
- Invariant 7: the display of unknown quota in the desktop and CLI is not asserted by the harness.
- Invariant 10: engine-level reduction and caching scenarios wait for the M5.3 wiring and the M5.4 cache.
- Stability: the harness (`tests/test_invariants.py`, `tests/test_fault_injection*.py`, this map) passed 5 consecutive runs of 98 tests each (2026-11-23, local, about 15 s per run). That is evidence against obvious flakiness, not a proof, and CI was not used.
