"""Per-run mock scenario maps for published workflows; synthetic outcomes only.

A scenario map is chosen from a closed server-owned catalog (no client text,
commands or bytes reach the provider). It is persisted once, immutably, as a
run event in the same transaction that starts the run. The run's kind is
derived from the persisted mock_run_config profile plus that event, so every
cancel, recovery and resume guard can tell runs it understands from any other.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from synapse.domain.contracts import EventEnvelope, encode_payload, validate_id
from synapse.persistence.mock_resume import MOCK_MAX_ATTEMPTS

FIXED_KIND = "fixed-v1"
SCENARIO_KIND = "scenario-v1"
FIXED_PROFILE = "synapse.builtin.mock.v1"
SCENARIO_PROFILE = "synapse.builtin.mock.scenario.v1"
KIND_PROFILES = {FIXED_KIND: FIXED_PROFILE, SCENARIO_KIND: SCENARIO_PROFILE}
EVENT_TYPE = "mock.scenarios_configured"
STEPS_EVENT_TYPE = "mock.approval_steps_configured"
_TOKEN = re.compile(r"[A-Za-z0-9._:/@+-]{1,128}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STEP_KEYS = {"operation", "target", "candidate_sha256", "policy_version", "ttl_seconds"}
DEFAULT_TTL_SECONDS = 3600
MAX_TTL_SECONDS = 86400

CANDIDATE = b"synapse mock candidate v1\n"
# Closed catalog: name -> (output chunks, outcome name). "slow" streams many
# empty chunks before the exact candidate so cancellation can land mid-run.
CATALOG: dict[str, tuple[tuple[str, ...], str]] = {
    "succeed": ((CANDIDATE.decode("ascii"),), "succeeded"),
    "fail": ((), "failed"),
    "reject": (("synapse mock rejected candidate\n",), "succeeded"),
    "slow": (("",) * 200 + (CANDIDATE.decode("ascii"),), "succeeded"),
}
DEFAULT_SCENARIO = "succeed"
MAX_BODY_BYTES = 16 * 1024
MAX_NODES = 256


class ScenarioError(ValueError):
    """The supplied scenario map is invalid for this run."""


def parse_scenarios(raw: Any) -> dict[str, tuple[str, ...]]:
    """Validate shape only: {node: name | [name, ...]} with catalog names."""
    if not isinstance(raw, dict) or not raw or len(raw) > MAX_NODES:
        raise ScenarioError("scenarios must be a nonempty object with at most 256 nodes")
    result: dict[str, tuple[str, ...]] = {}
    for node, value in raw.items():
        validate_id(node)
        choices = (value,) if isinstance(value, str) else tuple(value) if isinstance(value, list) else None
        if not choices or len(choices) > MOCK_MAX_ATTEMPTS or any(c not in CATALOG for c in choices):
            raise ScenarioError("each scenario must name 1 to %d catalog scenarios" % MOCK_MAX_ATTEMPTS)
        result[node] = choices
    return result


def resolve_for_run(db: sqlite3.Connection, run_id: str, parsed: dict[str, tuple[str, ...]]) -> dict[str, list[str]]:
    """Reject unknown nodes; unlisted nodes default to 'succeed'. Canonical map."""
    nodes = {row[0] for row in db.execute("SELECT node_id FROM tasks WHERE run_id=?", (run_id,))}
    if not nodes:
        raise ScenarioError("run has no tasks")
    unknown = sorted(set(parsed) - nodes)
    if unknown:
        raise ScenarioError("scenarios name nodes that are not in the run")
    return {node: list(parsed.get(node, (DEFAULT_SCENARIO,))) for node in sorted(nodes)}


def scenarios_sha256(canonical: dict[str, list[str]]) -> str:
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record_scenarios(db: sqlite3.Connection, run_id: str, canonical: dict[str, list[str]]) -> None:
    if not db.in_transaction:
        raise RuntimeError("Recording scenarios requires a transaction")
    event = EventEnvelope.create(aggregate_type="run", aggregate_id=run_id, event_type=EVENT_TYPE,
                                 payload={"run_id": run_id, "kind": SCENARIO_KIND, "scenarios": canonical,
                                          "sha256": scenarios_sha256(canonical)})
    db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
               (event.id, event.aggregate_type, run_id, run_id, event.event_type, event.schema_version,
                encode_payload(event.payload), event.occurred_at))
    db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))


def load_scenarios(db: sqlite3.Connection, run_id: str) -> dict[str, list[str]] | None:
    """The run's single recorded map, or None when absent or not trustworthy."""
    rows = db.execute("SELECT payload_json FROM events WHERE run_id=? AND type=?", (run_id, EVENT_TYPE)).fetchall()
    if len(rows) != 1:
        return None
    try:
        payload = json.loads(rows[0][0])
        canonical = payload["scenarios"]
        if (payload["run_id"] != run_id or payload["kind"] != SCENARIO_KIND
                or payload["sha256"] != scenarios_sha256(canonical)):
            return None
        for node, choices in canonical.items():
            validate_id(node)
            if (not isinstance(choices, list) or not 1 <= len(choices) <= MOCK_MAX_ATTEMPTS
                    or any(c not in CATALOG for c in choices)):
                return None
    except (ValueError, KeyError, TypeError):
        return None
    return canonical


def mock_kind(db: sqlite3.Connection, run_id: str) -> str | None:
    """'fixed-v1' or 'scenario-v1' only for a consistent mock-provider run.

    The fixture column is a legacy CHECK-constrained constant, so the kind is
    decided by the run's profile and scenario event. Anything else (no config,
    non-mock provider, missing/duplicated/tampered scenarios) returns None and
    every guard treats the run as not understood.
    """
    row = db.execute("SELECT c.profile_id,c.fixture,p.provider FROM mock_run_config c "
                     "LEFT JOIN profiles p ON p.id=c.profile_id WHERE c.run_id=?", (run_id,)).fetchone()
    if row is None or row[1] != FIXED_KIND or row[2] != "mock":
        return None
    has_event = db.execute("SELECT 1 FROM events WHERE run_id=? AND type=? LIMIT 1", (run_id, EVENT_TYPE)).fetchone()
    steps = load_approval_steps(db, run_id)
    if row[0] == FIXED_PROFILE and not has_event and not steps and steps is not None:
        return FIXED_KIND
    if row[0] == SCENARIO_PROFILE and load_scenarios(db, run_id) is not None and steps is not None:
        return SCENARIO_KIND
    return None


def parse_approval_steps(raw: Any) -> dict[str, dict[str, Any]]:
    """Validate the closed approval-step spec: {node: {operation, target, candidate_sha256,
    policy_version[, ttl_seconds]}}. Returns the canonical form (ttl always present)."""
    if not isinstance(raw, dict) or not raw or len(raw) > MAX_NODES:
        raise ScenarioError("approval_steps must be a nonempty object with at most 256 nodes")
    result: dict[str, dict[str, Any]] = {}
    for node, spec in raw.items():
        validate_id(node)
        if not isinstance(spec, dict) or not {"operation", "target", "candidate_sha256", "policy_version"} <= set(spec) <= _STEP_KEYS:
            raise ScenarioError("approval step has missing or unknown fields")
        for key in ("operation", "target", "policy_version"):
            if not isinstance(spec[key], str) or not _TOKEN.fullmatch(spec[key]):
                raise ScenarioError("operation, target and policy_version must be 1-128 characters of [A-Za-z0-9._:/@+-]")
        if not isinstance(spec["candidate_sha256"], str) or not _SHA256.fullmatch(spec["candidate_sha256"]):
            raise ScenarioError("candidate_sha256 must be 64 lowercase hex characters")
        ttl = spec.get("ttl_seconds", DEFAULT_TTL_SECONDS)
        if type(ttl) is not int or not 1 <= ttl <= MAX_TTL_SECONDS:
            raise ScenarioError("ttl_seconds must be an integer from 1 to 86400")
        result[node] = {"operation": spec["operation"], "target": spec["target"],
                        "candidate_sha256": spec["candidate_sha256"], "policy_version": spec["policy_version"],
                        "ttl_seconds": ttl}
    return result


def check_steps_for_run(db: sqlite3.Connection, run_id: str, steps: dict[str, dict[str, Any]]) -> None:
    nodes = {row[0] for row in db.execute("SELECT node_id FROM tasks WHERE run_id=?", (run_id,))}
    if set(steps) - nodes:
        raise ScenarioError("approval_steps name nodes that are not in the run")


def _steps_sha256(steps: dict[str, dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(steps, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record_approval_steps(db: sqlite3.Connection, run_id: str, steps: dict[str, dict[str, Any]]) -> None:
    if not db.in_transaction:
        raise RuntimeError("Recording approval steps requires a transaction")
    event = EventEnvelope.create(aggregate_type="run", aggregate_id=run_id, event_type=STEPS_EVENT_TYPE,
                                 payload={"run_id": run_id, "steps": steps, "sha256": _steps_sha256(steps)})
    db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
               (event.id, event.aggregate_type, run_id, run_id, event.event_type, event.schema_version,
                encode_payload(event.payload), event.occurred_at))
    db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))


def load_approval_steps(db: sqlite3.Connection, run_id: str) -> dict[str, dict[str, Any]] | None:
    """The run's recorded steps ({} when none were configured); None when not trustworthy."""
    rows = db.execute("SELECT payload_json FROM events WHERE run_id=? AND type=?", (run_id, STEPS_EVENT_TYPE)).fetchall()
    if not rows:
        return {}
    if len(rows) != 1:
        return None
    try:
        payload = json.loads(rows[0][0])
        steps = payload["steps"]
        if payload["run_id"] != run_id or payload["sha256"] != _steps_sha256(steps):
            return None
        if parse_approval_steps(steps) != steps:
            return None
    except (ValueError, KeyError, TypeError):
        return None
    return steps
