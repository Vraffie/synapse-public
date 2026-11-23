"""M5.5 wiring: log each routing pick and its estimated cost difference. Dry-run only.

Runs the deterministic rules of ``routing_policy.decide`` for an attempt and records the result in
``optimization_decisions`` plus an ``optimization.decision_recorded`` event, in one transaction. It
**never applies** anything: ``dry_run`` is fixed to True here, the record says ``applied: false``,
and no caller in this module can change what model or profile an attempt uses. Reviews and
approval-gated steps are never downgraded (guaranteed by ``decide``; an unknown review flag counts
as protected).

The cost difference is an ESTIMATE from operator-supplied prices and expected token counts
(``routing_policy.estimate_savings``). Without them it is recorded as unknown, never as zero, and
it is never written to the usage ledger or mixed with measured usage (ADR 017). Prompt text,
tool inputs and file contents are never recorded, only ids, hashes, counts and rule names.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
import json
import re
import sqlite3
from typing import Any

from synapse.application.routing_policy import (
    ESTIMATE_LABEL, TIERS, RoutingDecision, RoutingInput, decide, estimate_savings,
)
from synapse.domain.contracts import EventEnvelope, encode_payload, new_id
from synapse.persistence.idempotency import canonical_request_hash

RECORD_VERSION = 1
_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True, slots=True)
class RoutingDryRunConfig:
    """Operator configuration. Validated strictly; ``ValueError`` on anything malformed."""

    policy_version: str = "routing-dry-run-1"
    allowed_tiers: Collection[str] = TIERS
    min_downgrade_confidence: float = 0.6
    min_upgrade_confidence: float = 0.3
    pricing: Mapping[str, Any] | None = None  # {tier: {"input_per_mtok": usd, "output_per_mtok": usd}}
    expected_input_tokens: int | None = None
    expected_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not _VERSION.fullmatch(self.policy_version):
            raise ValueError("policy_version must match [A-Za-z0-9._-] and be at most 64 characters")
        for what in ("expected_input_tokens", "expected_output_tokens"):
            v = getattr(self, what)
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
                raise ValueError(f"{what} must be a non-negative integer or None")
        if self.pricing is not None and not isinstance(self.pricing, Mapping):
            raise ValueError("pricing must be a mapping or None")
        # Validates allowed_tiers and thresholds with the same rules the policy uses.
        decide(RoutingInput(), allowed_tiers=self.allowed_tiers, min_downgrade_confidence=self.min_downgrade_confidence,
               min_upgrade_confidence=self.min_upgrade_confidence)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 64 else None


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _flag(value: Any) -> bool | None:
    return value if type(value) is bool else None


def routing_record(config: RoutingDryRunConfig, inp: RoutingInput) -> dict[str, Any]:
    """Decide (dry run) and return the JSON-able record. Deterministic for equal inputs and config."""
    decision: RoutingDecision = decide(
        inp, allowed_tiers=config.allowed_tiers, min_downgrade_confidence=config.min_downgrade_confidence,
        min_upgrade_confidence=config.min_upgrade_confidence, dry_run=True)
    assert decision.applied is False
    inputs = {"role": _text(inp.role), "task_kind": _text(inp.task_kind), "prompt_bytes": _count(inp.prompt_bytes),
              "file_count": _count(inp.file_count), "has_approval_gate": _flag(inp.has_approval_gate),
              "is_review": _flag(inp.is_review), "retry_no": _count(inp.retry_no),
              "requested_tier": _text(inp.requested_tier)}
    if config.expected_input_tokens is None or config.expected_output_tokens is None:
        estimate: dict[str, Any] = {"label": ESTIMATE_LABEL, "status": "unknown", "estimated_usd": None,
                                    "reason": "no expected token counts configured"}
    else:
        saving = estimate_savings(decision, config.pricing or {}, expected_input_tokens=config.expected_input_tokens,
                                  expected_output_tokens=config.expected_output_tokens)
        estimate = {"label": saving.label, "status": "estimated" if saving.estimated_usd is not None else "unknown",
                    "estimated_usd": saving.estimated_usd, "basis": dict(saving.basis),
                    "reason": None if saving.estimated_usd is not None else "no valid price for the baseline or chosen tier"}
    return {"v": RECORD_VERSION, "kind": "routing", "dry_run": True, "applied": False,
            "policy_version": config.policy_version, "inputs": inputs, "inputs_sha256": canonical_request_hash(inputs),
            "decision": {"tier": decision.tier, "baseline_tier": decision.baseline_tier, "confidence": decision.confidence,
                         "rules_fired": list(decision.rules_fired), "downgraded_from": decision.downgraded_from,
                         "reason": decision.reason, "protected": decision.protected},
            "estimate": estimate}


def record_routing(db: sqlite3.Connection, *, run_id: str, attempt_id: str, record: dict[str, Any]) -> bool:
    """Append the decision row, event and outbox atomically; False if this attempt already has one.

    Must run inside the caller's transaction.
    """
    if not db.in_transaction:
        raise RuntimeError("record_routing must run inside a transaction")
    if record.get("kind") != "routing" or record.get("applied") is not False or record.get("dry_run") is not True:
        raise ValueError("only dry-run routing records can be stored")
    if db.execute("SELECT 1 FROM optimization_decisions WHERE task_attempt_id=? AND json_extract(decision_json,'$.kind')='routing'",
                  (attempt_id,)).fetchone():
        return False
    decision_id = new_id()
    d = record["decision"]
    event = EventEnvelope.create(aggregate_type="task_attempt", aggregate_id=attempt_id,
        event_type="optimization.decision_recorded",
        payload={"decision_id": decision_id, "kind": "routing", "dry_run": True, "applied": False,
                 "policy_version": record["policy_version"], "baseline_tier": d["baseline_tier"], "tier": d["tier"],
                 "rules_fired": d["rules_fired"], "inputs_sha256": record["inputs_sha256"],
                 "estimate_status": record["estimate"]["status"]})
    db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
               (event.id, event.aggregate_type, attempt_id, run_id, event.event_type, event.schema_version,
                encode_payload(event.payload), event.occurred_at))
    db.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))
    db.execute("INSERT INTO optimization_decisions(id,task_attempt_id,policy_version,decision_json,created_at) VALUES(?,?,?,?,?)",
               (decision_id, attempt_id, record["policy_version"],
                json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False), event.occurred_at))
    return True
