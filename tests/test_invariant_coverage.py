"""Which test asserts each of the ten ARCHITECTURE.md section 13 invariants.

The map is data, checked here: every named test must exist, every invariant must name at least one,
and any invariant not fully covered must say what is missing. A rename or deletion of a test that
the map relies on fails this file, so the coverage claim in docs/operations/invariant-coverage.md
cannot silently rot. It proves the tests exist, not that they pass (the suite does that).
"""
import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
DOC = TESTS.parent / "docs" / "operations" / "invariant-coverage.md"

# number -> (short name, status, tests, what is missing)
COVERAGE = {
    1: ("single authoritative daemon writer", "covered", [
        "test_daemon_lock.py::test_lock_rejects_concurrent_owner_and_releases_on_close",
        "test_daemon_lock.py::test_lock_releases_when_owner_process_exits",
        "test_fault_injection_processes.py::test_a_killed_lock_owner_releases_ownership_and_loses_only_its_uncommitted_write",
        "test_fault_injection_workers.py::test_a_second_writer_holding_the_lock_fails_a_start_cleanly",
    ], ""),
    2: ("state change and event/outbox commit atomically", "covered", [
        "test_invariants.py::test_detects_event_without_outbox_row",
        "test_invariants.py::test_detects_outbox_row_without_event",
        "test_fault_injection_workers.py::test_disk_full_on_artifact_write_leaves_no_partial_state",
        "test_fault_injection_messaging.py::test_duplicate_event_rows_are_rejected_and_replay_is_stable",
    ], ""),
    3: ("messages immutable, delivery at least once", "covered", [
        "test_invariants.py::test_every_required_trigger_exists_on_a_healthy_database",
        "test_fault_injection_messaging.py::test_lease_expiry_redelivers_and_clock_jumps_never_steal_a_live_lease",
        "test_fault_injection_messaging.py::test_a_publisher_that_crashes_before_the_ack_redelivers_the_same_event_ids",
        "test_fault_injection_messaging.py::test_exhausted_deliveries_dead_letter_instead_of_looping",
    ], ""),
    4: ("side effects idempotent or reconciled", "covered", [
        "test_side_effect_ledger.py::test_crash_between_intent_and_completion_becomes_unknown_never_replayed",
        "test_fault_injection_workers.py::test_worker_killed_mid_attempt_becomes_unknown_and_is_never_respawned",
        "test_fault_injection_workers.py::test_resume_after_a_crash_launches_exactly_one_new_attempt_and_keeps_every_invariant",
    ], ""),
    5: ("approval invalid after a candidate or policy change", "covered", [
        "test_fault_injection_approvals.py::test_a_decision_bound_to_a_changed_candidate_or_policy_is_refused_and_launches_nothing",
        "test_fault_injection_approvals.py::test_parked_approval_survives_a_crash_and_expires_only_forward_in_time",
    ], ""),
    6: ("no capability elevation by message or prompt", "outside-harness", [
        "test_message_gateway.py::test_untrusted_fields_size_invalid_json_and_capability_are_rejected",
        "test_session_authority.py::test_credentials_bind_identity_and_exact_capabilities",
        "test_message_service.py::test_forged_and_cross_run_sessions_cannot_read_or_complete",
    ], "security-style unit tests, not a fault scenario; no end-to-end test that a real agent message cannot widen its tools"),
    7: ("unknown quota displayed as unknown", "partial", [
        "test_usage_ledger.py::test_partial_usage_keeps_unknown_fields_none_not_zero",
        "test_budget_wiring.py::test_missing_usage_is_unknown_never_zero",
        "test_invariants.py::test_detects_usage_row_that_reports_nothing",
    ], "storage and budget side only; what the desktop and CLI show for unknown is not asserted here"),
    8: ("desktop disconnection cannot cancel work", "covered", [
        "test_fault_injection_clients.py::test_dropped_clients_never_cancel_a_parked_run_and_approval_still_finishes_it",
    ], "real daemon, dropped HTTP and WebSocket clients; a killed Qt process is not simulated, only its socket behaviour"),
    9: ("verification identifies its exact candidate artifact", "covered", [
        "test_invariants.py::test_detects_verification_without_candidate_artifact",
        "test_invariants.py::test_detects_verification_with_different_candidate_hash",
        "test_invariants.py::test_detects_verification_not_bound_to_an_attempt_of_its_task",
    ], ""),
    10: ("token optimization cannot bypass mandatory checks", "partial", [
        "test_output_reducer.py::test_pytest_failures_survive_a_tight_budget",
        "test_output_reducer.py::test_failures_beyond_the_budget_are_kept_and_reported_over_budget",
        "test_budget_wiring.py::test_approved_gated_step_is_blocked_at_resume_and_never_runs",
    ], "the reducer and budget are unit-tested; no fault scenario drives reduction or caching through the engine until the M5.3 wiring merges"),
}
STATUSES = {"covered", "partial", "outside-harness"}


def _functions(path: Path) -> set[str]:
    return {n.name for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_invariant_is_listed_with_a_status_and_tests():
    assert sorted(COVERAGE) == list(range(1, 11))
    for number, (name, status, tests, missing) in COVERAGE.items():
        assert name and status in STATUSES and tests, number
        assert (status == "covered") or missing, f"invariant {number} is {status} but says nothing is missing"


def test_every_named_test_exists():
    problems = []
    for number, (_, _, tests, _) in COVERAGE.items():
        for ref in tests:
            file, _, name = ref.partition("::")
            path = TESTS / file
            if not path.is_file():
                problems.append(f"{number}: no file {file}")
            elif name not in _functions(path):
                problems.append(f"{number}: no test {ref}")
    assert not problems, "\n".join(problems)


def test_the_documentation_lists_every_invariant_and_status():
    text = DOC.read_text()
    for number, (name, status, tests, _) in COVERAGE.items():
        assert name in text and f"`{status}`" in text, number
        assert tests[0].split("::")[1] in text, f"doc does not mention {tests[0]}"
