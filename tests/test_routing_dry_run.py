"""M5.5 dry-run routing log: never applied, never downgrades protected work, estimates stay estimates."""
from contextlib import closing
import json
import sqlite3
from pathlib import Path

import pytest

from synapse.application.routing_dry_run import RoutingDryRunConfig, record_routing, routing_record
from synapse.application.routing_policy import RoutingInput
from synapse.persistence.migrations import apply_migrations, open_database

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
PRICING = {"fast": {"input_per_mtok": 1, "output_per_mtok": 5}, "balanced": {"input_per_mtok": 3, "output_per_mtok": 15},
           "deep": {"input_per_mtok": 15, "output_per_mtok": 75}}
SMALL = dict(task_kind="format", prompt_bytes=100, file_count=1, has_approval_gate=False, is_review=False, retry_no=0)


def test_record_is_dry_run_and_never_applied():
    r = routing_record(RoutingDryRunConfig(), RoutingInput(**SMALL))
    assert r["dry_run"] is True and r["applied"] is False and r["kind"] == "routing"
    assert r["decision"]["tier"] == "fast" and r["decision"]["downgraded_from"] == "balanced"
    assert "small_mechanical" in r["decision"]["rules_fired"]


@pytest.mark.parametrize("override", [{"has_approval_gate": True}, {"is_review": True}, {"is_review": None},
                                      {"has_approval_gate": "yes"}, {"retry_no": 1}, {"retry_no": None}])
def test_protected_work_is_never_downgraded(override):
    r = routing_record(RoutingDryRunConfig(), RoutingInput(**{**SMALL, **override}))
    assert r["decision"]["downgraded_from"] is None and r["decision"]["tier"] != "fast"
    assert r["decision"]["protected"] is True and r["applied"] is False


def test_record_is_deterministic_and_keeps_no_free_text():
    a = routing_record(RoutingDryRunConfig(), RoutingInput(**SMALL, role="x" * 500))
    b = routing_record(RoutingDryRunConfig(), RoutingInput(**SMALL, role="x" * 500))
    assert a == b and a["inputs"]["role"] is None  # over-long text is dropped, not stored
    assert a["inputs_sha256"] != routing_record(RoutingDryRunConfig(), RoutingInput(**{**SMALL, "prompt_bytes": 101}))["inputs_sha256"]
    junk = routing_record(RoutingDryRunConfig(), RoutingInput(prompt_bytes=True, file_count=-1, task_kind=["x"]))
    assert junk["inputs"]["prompt_bytes"] is None and junk["inputs"]["file_count"] is None and junk["inputs"]["task_kind"] is None


def test_estimate_is_unknown_without_configuration_and_never_zero():
    r = routing_record(RoutingDryRunConfig(), RoutingInput(**SMALL))
    assert r["estimate"]["status"] == "unknown" and r["estimate"]["estimated_usd"] is None
    partial = routing_record(RoutingDryRunConfig(expected_input_tokens=1000, expected_output_tokens=1000), RoutingInput(**SMALL))
    assert partial["estimate"]["status"] == "unknown" and partial["estimate"]["estimated_usd"] is None  # no prices


def test_estimate_uses_operator_prices_and_is_labelled():
    cfg = RoutingDryRunConfig(pricing=PRICING, expected_input_tokens=1_000_000, expected_output_tokens=100_000)
    r = routing_record(cfg, RoutingInput(**SMALL))  # balanced -> fast
    assert r["estimate"]["status"] == "estimated" and "ESTIMATE" in r["estimate"]["label"]
    assert r["estimate"]["estimated_usd"] == pytest.approx((3 + 1.5) - (1 + 0.5))
    kept = routing_record(cfg, RoutingInput(**{**SMALL, "is_review": True}))
    assert kept["decision"]["tier"] == kept["decision"]["baseline_tier"] and kept["estimate"]["estimated_usd"] == 0.0


@pytest.mark.parametrize("kwargs", [{"policy_version": "bad version"}, {"policy_version": ""}, {"allowed_tiers": ()},
                                    {"allowed_tiers": ("huge",)}, {"min_downgrade_confidence": 2}, {"expected_input_tokens": -1},
                                    {"expected_output_tokens": True}, {"pricing": [1]}])
def test_config_is_validated(kwargs):
    with pytest.raises(ValueError):
        RoutingDryRunConfig(**kwargs)


@pytest.fixture
def db(tmp_path):
    from synapse.persistence.workflow_store import create_run, publish_workflow
    with closing(open_database(tmp_path / "db")) as db:
        apply_migrations(db, MIGRATIONS)
        publish_workflow(db, workflow_id="wf", version="1", nodes=[{"id": "n", "dependencies": []}], principal="o", idempotency_key="p")
        run = create_run(db, workflow_id="wf", version="1", principal="o", idempotency_key="k").response["run_id"]
        task = db.execute("SELECT id FROM tasks WHERE run_id=?", (run,)).fetchone()[0]
        db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,state) VALUES('att1',?,1,'running')", (task,))
        yield db, run


def _record(db, run, record, attempt="att1"):
    db.execute("BEGIN IMMEDIATE")
    try:
        return record_routing(db, run_id=run, attempt_id=attempt, record=record)
    finally:
        db.execute("COMMIT")


def test_row_event_and_outbox_commit_together_once(db):
    db, run = db
    record = routing_record(RoutingDryRunConfig(), RoutingInput(**SMALL))
    assert _record(db, run, record) is True and _record(db, run, record) is False
    ((policy, doc),) = db.execute("SELECT policy_version, decision_json FROM optimization_decisions").fetchall()
    assert policy == "routing-dry-run-1" and json.loads(doc) == record
    (payload,) = db.execute("SELECT payload_json FROM events WHERE type='optimization.decision_recorded'").fetchone()
    p = json.loads(payload)
    assert p["applied"] is False and p["tier"] == "fast" and p["estimate_status"] == "unknown" and "estimated_usd" not in p
    assert db.execute("SELECT count(*) FROM outbox o JOIN events e ON e.id=o.event_id WHERE e.type='optimization.decision_recorded'").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM usage_observations").fetchone()[0] == 0  # an estimate never enters the usage ledger


def test_record_refuses_applied_or_non_routing_records_and_outside_a_transaction(db):
    db, run = db
    record = routing_record(RoutingDryRunConfig(), RoutingInput(**SMALL))
    with pytest.raises(RuntimeError, match="transaction"):
        record_routing(db, run_id=run, attempt_id="att1", record=record)
    db.execute("BEGIN")
    for bad in ({**record, "applied": True}, {**record, "dry_run": False}, {**record, "kind": "cache"}):
        with pytest.raises(ValueError):
            record_routing(db, run_id=run, attempt_id="att1", record=bad)
    with pytest.raises(sqlite3.IntegrityError):
        record_routing(db, run_id=run, attempt_id="nope", record=record)
    db.execute("ROLLBACK")
    assert db.execute("SELECT count(*) FROM optimization_decisions").fetchone()[0] == 0
