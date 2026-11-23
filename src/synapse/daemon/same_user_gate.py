"""Fail-closed gate: agent runs share the uid that can read the approver token.

Security review F1/F2 (docs/security/review-2026-11-20.md): agent children run as the daemon's
own user, so they can read ``approver.token``/``operator.token`` and write the SQLite file.
Approvals are therefore NOT a security boundary against agent code. The daemon refuses to enable
the agent runtime unless the operator explicitly acknowledges that.
"""
from __future__ import annotations

from dataclasses import dataclass
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


SAME_UID_NO_SANDBOX = "same_uid_no_sandbox"
SAME_UID_SANDBOXED = "same_uid_sandboxed"
DIFFERENT_UID = "different_uid"
UNKNOWN_INPUT = "unknown_input"


@dataclass(frozen=True)
class ExposureVerdict:
    """Pure description of same-uid exposure. ``exposed`` is the only field the gate may act on."""
    exposed: bool
    reason: str
    sandbox_configured: bool


def assess_exposure(owner_uid: object, agent_uid: object, sandbox_configured: object) -> ExposureVerdict:
    """Classify exposure of the state dir to the agent uid. No I/O, no logging; fails closed.

    A sandbox reduces but does not remove same-uid exposure, so ``same_uid_sandboxed`` is still exposed.
    Anything that is not a plain int uid (None, bool, str, negative) is treated as exposed.
    """
    sandbox = sandbox_configured is True
    for uid in (owner_uid, agent_uid):
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            return ExposureVerdict(True, UNKNOWN_INPUT, sandbox)
    if owner_uid != agent_uid:
        return ExposureVerdict(False, DIFFERENT_UID, sandbox)
    return ExposureVerdict(True, SAME_UID_SANDBOXED if sandbox else SAME_UID_NO_SANDBOX, sandbox)


def sandbox_note(verdict: ExposureVerdict) -> str:
    if verdict.sandbox_configured:
        return ("a sandbox is configured for the agent profile, which reduces but does not remove this exposure; "
                "it does not replace the acknowledgement")
    return "no sandbox is configured for the agent profile"


class SameUserAgentsRefused(RuntimeError):
    """Agent runs would share a uid with the approver token and no acknowledgement was given."""


def agents_share_approver_uid(state_dir: Path) -> bool:
    """True when the agent uid (always the daemon's effective uid today) owns the approver secret."""
    try:
        info = (state_dir / "approver.token").lstat()
    except FileNotFoundError:
        info = state_dir.lstat()  # the token will be created here, owned by the daemon uid
    return info.st_uid == os.geteuid()


CODEX_NOTE = ("a codex profile is configured; Synapse runs the Codex child WITHOUT any Synapse sandbox (only "
              "Codex's own -s read-only/workspace-write, which does not confine reads), so it can read the approver "
              "and operator tokens and reach agentd.sock")


def check_same_user_agents(state_dir: Path, *, agents_enabled: bool, allow: bool,
                           sandbox_configured: bool | None = None, codex_unsandboxed: bool = False) -> bool:
    """Return True when an acknowledged same-user start must be recorded; raise when refused."""
    if not agents_enabled or not agents_share_approver_uid(state_dir):
        return False
    # The verdict only words the message; the refusal below depends solely on `allow`.
    note = sandbox_note(assess_exposure(os.geteuid(), os.geteuid(), sandbox_configured))
    if codex_unsandboxed:
        note += "; " + CODEX_NOTE
    if not allow:
        raise SameUserAgentsRefused(
            "refusing to enable agent runs: agents would run as the same user that can read the approver "
            "and operator tokens and write the database, so an agent can approve its own requests "
            "(see %s, F1/F2). Approvals are not a security boundary here. Run agents under a separate uid, "
            "or pass %s (or set %s=1) to acknowledge the risk (%s)." % (REVIEW_DOC, FLAG, ENV, note))
    LOG.warning("SECURITY: agent runs enabled with %s; agents run as the same user that can read the approver "
                "token and write the database, so approvals are NOT a security boundary (%s); %s", FLAG, REVIEW_DOC, note)
    return True


def record_acknowledgement(db: sqlite3.Connection, *, codex_unsandboxed: bool = False) -> None:
    detail = {"flag": FLAG, "review": REVIEW_DOC}
    if codex_unsandboxed:  # only then, so earlier audit rows keep their exact shape
        detail["codex_unsandboxed"] = True
    append_audit(db, "daemon:startup", ACK_ACTION, "agent-runtime", "allowed", None,
                 json.dumps(detail, sort_keys=True, separators=(",", ":")),
                 datetime.now(timezone.utc).isoformat())
