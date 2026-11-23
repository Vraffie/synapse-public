"""Fail-closed gate: agent runs share the uid that can read the approver token.

Security review F1/F2 (docs/security/review-2026-11-20.md): agent children run as the daemon's
own user, so they can read ``approver.token``/``operator.token`` and write the SQLite file.
Approvals are therefore NOT a security boundary against agent code. The daemon refuses to enable
the agent runtime unless the operator explicitly acknowledges that.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3

from synapse.persistence.audit_chain import append_audit

LOG = logging.getLogger("synapse.daemon")
FLAG = "--allow-same-user-agents"
ENV = "SYNAPSE_ALLOW_SAME_USER_AGENTS"
ACK_ACTION = "agent_runtime.same_user_acknowledged"
REVIEW_DOC = "docs/security/review-2026-11-20.md"


class SameUserAgentsRefused(RuntimeError):
    """Agent runs would share a uid with the approver token and no acknowledgement was given."""


def agents_share_approver_uid(state_dir: Path) -> bool:
    """True when the agent uid (always the daemon's effective uid today) owns the approver secret."""
    try:
        info = (state_dir / "approver.token").lstat()
    except FileNotFoundError:
        info = state_dir.lstat()  # the token will be created here, owned by the daemon uid
    return info.st_uid == os.geteuid()


def check_same_user_agents(state_dir: Path, *, agents_enabled: bool, allow: bool) -> bool:
    """Return True when an acknowledged same-user start must be recorded; raise when refused."""
    if not agents_enabled or not agents_share_approver_uid(state_dir):
        return False
    if not allow:
        raise SameUserAgentsRefused(
            "refusing to enable agent runs: agents would run as the same user that can read the approver "
            "and operator tokens and write the database, so an agent can approve its own requests "
            "(see %s, F1/F2). Approvals are not a security boundary here. Run agents under a separate uid, "
            "or pass %s (or set %s=1) to acknowledge the risk." % (REVIEW_DOC, FLAG, ENV))
    LOG.warning("SECURITY: agent runs enabled with %s; agents run as the same user that can read the approver "
                "token and write the database, so approvals are NOT a security boundary (%s)", FLAG, REVIEW_DOC)
    return True


def record_acknowledgement(db: sqlite3.Connection) -> None:
    append_audit(db, "daemon:startup", ACK_ACTION, "agent-runtime", "allowed", None,
                 json.dumps({"flag": FLAG, "review": REVIEW_DOC}, sort_keys=True, separators=(",", ":")),
                 datetime.now(timezone.utc).isoformat())
