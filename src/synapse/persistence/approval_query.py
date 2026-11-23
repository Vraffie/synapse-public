"""Read-only approval queries for the trusted daemon operator."""
from collections.abc import Callable
import sqlite3
from typing import Any

from synapse.persistence.approval_subject import agent_subject


def read_approvals(connection: sqlite3.Connection, *, after: str = "", limit: int = 100,
                   read_blob: Callable[[str], bytes] | None = None) -> list[dict[str, Any]]:
    """Return an ID-ordered page; caller must authorize access to every row.

    Every row carries ``subject``: for an agent run's approval, a bounded read-only description of what
    is being approved (see ``approval_subject``); otherwise null. The prompt preview is only read for
    still-pending approvals (``read_blob`` is the private artifact reader); decided rows get the
    structured fields without it.
    """
    if not isinstance(after, str) or len(after) > 256 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Invalid cursor or page size")
    rows = connection.execute(
        "SELECT id,run_id,operation,target,candidate_manifest_sha256,policy_version,state,state_version,"
        "expires_at,requested_at,decided_at,decision_principal,decision_reason "
        "FROM approval_requests WHERE id > ? ORDER BY id LIMIT ?", (after, limit)
    )
    keys = ("id", "run_id", "operation", "target", "candidate_manifest_sha256", "policy_version",
            "state", "state_version", "expires_at", "requested_at", "decided_at", "decision_principal", "decision_reason")
    out = [dict(zip(keys, row, strict=True)) for row in rows.fetchall()]
    for item in out:
        item["subject"] = agent_subject(connection, read_blob, item["run_id"], item["candidate_manifest_sha256"],
                                        with_prompt=item["state"] == "requested")
    return out
