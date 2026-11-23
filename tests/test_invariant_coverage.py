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
    6: ("no capability elevation by message or prompt", "partial", [
        "test_message_gateway.py::test_untrusted_fields_size_invalid_json_and_capability_are_rejected",
        "test_session_authority.py::test_credentials_bind_identity_and_exact_capabilities",
        "test_message_service.py::test_forged_and_cross_run_sessions_cannot_read_or_complete",
        "test_capability_elevation.py::test_a_message_that_asks_for_power_is_only_data_and_changes_no_grant",
        "test_capability_elevation.py::test_request_fields_that_try_to_set_identity_or_capability_are_refused",
        "test_capability_elevation.py::test_a_sender_without_the_recipient_or_thread_grant_cannot_reach_other_agents",
        "test_capability_elevation.py::test_hostile_text_in_a_prompt_value_cannot_close_its_fence_or_outrank_the_guard",
        "test_capability_elevation.py::test_a_prompt_value_cannot_inject_new_template_fields_or_extra_values",
    ], "message and prompt seams are tested (grants unchanged after a message that asks for power, forged fields refused, hostile text cannot close its fence); not tested: a real spawned agent, whose only enforcement is the spawn's tool and permission allowlist, and a real model's obedience"),
    7: ("unknown quota displayed as unknown", "covered", [
        "test_usage_ledger.py::test_partial_usage_keeps_unknown_fields_none_not_zero",
        "test_budget_wiring.py::test_missing_usage_is_unknown_never_zero",
        "test_invariants.py::test_detects_usage_row_that_reports_nothing",
        "test_cli_usage_daemon.py::test_usage_reports_known_totals_and_per_field_coverage",
        "test_cli_usage_daemon.py::test_usage_for_run_without_attempts_is_empty_not_zero",
        "test_desktop_agent_run.py::test_usage_table_distinguishes_zero_from_unknown",
        "test_desktop_agent_run.py::test_usage_empty_unavailable_and_invalid_states",
    ], "real provider usage reports (only fakes are used) and the desktop under a real display; Qt tests run offscreen"),
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
        "test_agent_runtime.py::test_raw_output_is_stored_and_summary_carries_reduction_and_raw_hash",
        "test_agent_runtime.py::test_a_large_reduction_is_clipped_so_a_succeeded_attempt_still_gets_a_summary",
        "test_agent_runtime.py::test_a_stored_raw_artifact_with_the_wrong_digest_fails_a_succeeded_attempt_closed",
    ], "reduction is now exercised through the engine (raw output kept, failure lines survive, a summary that would overflow is clipped, a raw artifact that cannot be stored fails a success closed), but the exact cache (M5.4 second half) is not built and routing is a dry-run log only, so neither can bypass a check yet and neither has a fault scenario"),
}
STATUSES = {"covered", "partial", "outside-harness"}


BLOCKING_MARKS = {"skip", "skipif", "xfail"}


def _mark_names(node: ast.AST) -> set[str]:
    """Names of pytest marks found in a decorator or a pytestmark value (call, attribute, or a list of them)."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return set().union(*(_mark_names(e) for e in node.elts)) if node.elts else set()
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and node.value.attr == "mark":
        return {node.attr} & BLOCKING_MARKS
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "mark":
        return {node.attr} & BLOCKING_MARKS
    return set()


def _pytestmark(body: list[ast.stmt]) -> set[str]:
    found: set[str] = set()
    for stmt in body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets):
            found |= _mark_names(stmt.value)
    return found


def _decorator_marks(fn: ast.AST) -> set[str]:
    return set().union(*(_mark_names(d) for d in fn.decorator_list)) if fn.decorator_list else set()


def _collected(path: Path) -> dict[str, set[str]]:
    """Tests pytest would collect from the file -> blocking marks on them.

    Only module-level test_* functions and test_* methods of module-level Test* classes count; a nested
    helper or a function inside another function with the same name does not. Marks come from the
    function's decorators, its class's decorators and class-level pytestmark, and module-level pytestmark.
    Every skip, skipif (conditional or not: a condition can silently be true in CI) and xfail counts.
    """
    tree = ast.parse(path.read_text())
    module_marks = _pytestmark(tree.body)
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    found: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, funcs) and node.name.startswith("test"):
            found[node.name] = module_marks | _decorator_marks(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            class_marks = module_marks | _decorator_marks(node) | _pytestmark(node.body)
            for item in node.body:
                if isinstance(item, funcs) and item.name.startswith("test"):
                    found[f"{node.name}::{item.name}"] = class_marks | _decorator_marks(item)
                    found.setdefault(item.name, found[f"{node.name}::{item.name}"])
    return found


def _problem(tests_dir: Path, ref: str) -> str | None:
    """Why a named test does not count as coverage, or None if it does."""
    file, _, name = ref.partition("::")
    path = tests_dir / file
    if not path.is_file():
        return f"no file {file}"
    collected = _collected(path)
    if name not in collected:
        return f"no collected test {ref}"
    if collected[name]:
        return f"{ref} is marked {'/'.join(sorted(collected[name]))} and would not assert anything"
    return None


def test_every_invariant_is_listed_with_a_status_and_tests():
    assert sorted(COVERAGE) == list(range(1, 11))
    for number, (name, status, tests, missing) in COVERAGE.items():
        assert name and status in STATUSES and tests, number
        assert (status == "covered") or missing, f"invariant {number} is {status} but says nothing is missing"


def test_every_named_test_exists():
    problems = []
    for number, (_, _, tests, _) in COVERAGE.items():
        for ref in tests:
            if (why := _problem(TESTS, ref)):
                problems.append(f"{number}: {why}")
    assert not problems, "\n".join(problems)


def test_the_checker_refuses_skipped_xfailed_nested_and_missing_tests(tmp_path):
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n"
        "from pytest import mark\n"
        "def test_ok(): pass\n"
        "@pytest.mark.skip(reason='x')\ndef test_skipped(): pass\n"
        "@pytest.mark.skipif(False, reason='x')\ndef test_conditionally_skipped(): pass\n"
        "@pytest.mark.xfail\ndef test_xfailed(): pass\n"
        "@mark.skip\ndef test_bare_mark_skipped(): pass\n"
        "@pytest.mark.parametrize('a', [1])\ndef test_parametrized(a): pass\n"
        "def helper_owner():\n    def test_nested(): pass\n"
        "class TestGroup:\n    def test_method(self): pass\n"
        "    @pytest.mark.skip\n    def test_skipped_method(self): pass\n"
        "class Helper:\n    def test_not_collected(self): pass\n"
        "@pytest.mark.skip\nclass TestSkippedClass:\n    def test_in_skipped_class(self): pass\n"
    )
    (tmp_path / "test_modmark.py").write_text("import pytest\npytestmark = [pytest.mark.xfail]\ndef test_a(): pass\n")
    (tmp_path / "test_classmark.py").write_text("import pytest\nclass TestC:\n    pytestmark = pytest.mark.skipif(True, reason='x')\n    def test_a(self): pass\n")
    ok = ["test_sample.py::test_ok", "test_sample.py::test_parametrized", "test_sample.py::test_method"]
    assert [_problem(tmp_path, r) for r in ok] == [None, None, None]
    for ref, text in [
        ("test_sample.py::test_skipped", "skip"),
        ("test_sample.py::test_conditionally_skipped", "skipif"),
        ("test_sample.py::test_xfailed", "xfail"),
        ("test_sample.py::test_bare_mark_skipped", "skip"),
        ("test_sample.py::test_skipped_method", "skip"),
        ("test_sample.py::test_in_skipped_class", "skip"),
        ("test_modmark.py::test_a", "xfail"),
        ("test_classmark.py::test_a", "skipif"),
    ]:
        assert text in (_problem(tmp_path, ref) or ""), ref
    for ref in ["test_sample.py::test_nested", "test_sample.py::test_not_collected", "test_sample.py::test_absent"]:
        assert "no collected test" in _problem(tmp_path, ref), ref
    assert _problem(tmp_path, "test_missing.py::test_ok") == "no file test_missing.py"


def test_the_documentation_lists_every_invariant_and_status():
    text = DOC.read_text()
    for number, (name, status, tests, _) in COVERAGE.items():
        assert name in text and f"`{status}`" in text, number
        for ref in tests:
            assert ref.split("::")[1] in text, f"doc does not mention {ref}"
