"""Plain-language narration for the Control Center: pure functions, no Qt, no I/O.

Everything here turns daemon vocabulary (event types, message kinds, state names, node ids) into short sentences an
operator can read without knowing the system. Daemon text is untrusted: every value interpolated into a sentence is
cleaned and bounded, and unknown shapes fall back to a safe generic sentence rather than raising.
"""
from __future__ import annotations

import html
import re

MAX_NAME = 48
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# (badge label, tone) per task state; tones are idle | running | done | failed | waiting
TASK_BADGES = {
    "created": ("IDLE", "idle"), "waiting_dependencies": ("IDLE", "idle"), "ready": ("READY", "idle"),
    "assigned": ("STARTING", "running"), "running": ("RUNNING", "running"), "verifying": ("CHECKING", "running"),
    "completed": ("DONE", "done"), "failed": ("FAILED", "failed"), "blocked": ("BLOCKED", "failed"),
    "cancelled": ("STOPPED", "idle"), "retry_wait": ("RETRYING", "waiting"),
    "changes_required": ("CHANGES NEEDED", "waiting"), "awaiting_approval": ("WAITING", "waiting"),
}
TASK_SENTENCES = {
    "created": "has not started yet", "waiting_dependencies": "is waiting for earlier steps to finish",
    "ready": "is ready to start", "assigned": "is being handed to an agent", "running": "is working now",
    "verifying": "is being checked", "completed": "finished successfully", "failed": "failed",
    "blocked": "is blocked and cannot continue", "cancelled": "was stopped", "retry_wait": "is waiting to try again",
    "changes_required": "needs changes before it can continue", "awaiting_approval": "is waiting for your approval",
}
RUN_LABELS = {
    "created": "Preparing", "queued": "Ready to start", "running": "Running", "awaiting_approval": "Needs your approval",
    "paused": "Paused", "recovering": "Recovering", "succeeded": "Complete", "failed": "Failed", "cancelled": "Stopped",
}
APPROVAL_LABELS = {
    "requested": "Pending", "approved": "Approved", "rejected": "Rejected", "expired": "Expired", "revoked": "Revoked",
}
MESSAGE_KINDS = {
    "task.request": "asked for a piece of work", "question": "asked a question", "answer": "answered a question",
    "review.request": "asked for a review", "review.finding": "shared review findings",
    "change.request": "asked for a change", "change.completed": "reported a change as done",
    "verification.result": "reported a check result", "escalation": "asked for a person to step in",
    "ack": "acknowledged a message",
}


def clean(value, limit: int = 120) -> str:
    """Bounded single-line text from an untrusted value."""
    return _CONTROL.sub(" ", value if isinstance(value, str) else "").strip()[:limit]


def safe_tip(text) -> str:
    """Tooltip text that Qt can never interpret as markup: Qt renders any tooltip that looks like HTML as rich text, so
    escape it and wrap it in an explicit rich block that preserves whitespace."""
    return "<p style='white-space:pre'>%s</p>" % html.escape(text if isinstance(text, str) else "", quote=True)


def humanize_node(node_id) -> str:
    """Readable name from a node id: split on - and _, title-case. Falls back to 'Step' for empty/odd ids."""
    words = [w for w in re.split(r"[-_\s.]+", clean(node_id, 200)) if w]
    return " ".join(w[:1].upper() + w[1:] if not w.isupper() else w for w in words)[:MAX_NAME] or "Step"


def task_badge(state) -> tuple[str, str]:
    return TASK_BADGES.get(state, ("UNKNOWN", "idle")) if isinstance(state, str) else ("UNKNOWN", "idle")


def task_sentence(state) -> str:
    return TASK_SENTENCES.get(state, "is in an unrecognised state") if isinstance(state, str) else "is in an unrecognised state"


def run_label(state) -> str:
    return RUN_LABELS.get(state, "Unknown") if isinstance(state, str) else "Unknown"


def approval_label(state) -> str:
    return APPROVAL_LABELS.get(state, "Unknown") if isinstance(state, str) else "Unknown"


def phase_label(index: int) -> str:
    return f"Step {index + 1}"


def _name(names, key) -> str | None:
    return names.get(key) if isinstance(names, dict) and isinstance(key, str) else None


def narrate_event(event, names: dict | None = None) -> str:
    """One short sentence for a daemon event. `names` maps task ids to readable step names (optional)."""
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        return "Something happened: unknown event"
    kind = clean(event["type"], 64)
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    step = _name(names, event.get("aggregate_id")) or (humanize_node(payload["node_id"]) if isinstance(payload.get("node_id"), str) else "A step")
    after = payload.get("after") if isinstance(payload.get("after"), str) else None
    if kind == "run.created":
        return "A new run was set up."
    if kind == "run.state_changed" and after:
        return f"The run is now: {run_label(after).lower()}."
    if kind == "task.created":
        return f"{step} was added to the plan."
    if kind == "task.state_changed" and after:
        return f"{step} {task_sentence(after)}."
    if kind == "task.attempt_reserved":
        return f"{step} is getting an agent."
    if kind == "task_attempt.state_changed" and after:
        return f"An agent working on {_name(names, payload.get('task_id')) or 'a step'} is now {after.replace('_', ' ')[:30]}."
    if kind == "task_attempt.mock_reconciled":
        return "A leftover practice attempt was cleaned up."
    if kind == "task.approval_gated":
        return f"{step} paused and is waiting for your approval."
    if kind == "task.approval_resolved":
        return f"Your decision was applied to {step}."
    if kind == "approval.requested":
        what = clean(payload.get("operation"), 40)
        return f"Approval requested{' to ' + what if what else ''}. Nothing happens until you decide."
    if kind == "approval.state_changed" and after:
        return {"approved": "The request was approved.", "rejected": "The request was rejected.",
                "expired": "The request expired without a decision, so it was not approved.",
                "revoked": "The approval was withdrawn."}.get(after, f"An approval changed to {after.replace('_', ' ')[:30]}.")
    if kind == "message.created":
        return "An agent sent a message."
    if kind == "agent.tasks_configured":
        return "The agent's work was planned."
    if kind == "agent.cancellation_requested":
        return "A stop was requested for the agent."
    if kind == "agent.usage_observed":
        return "The agent used some of its budget."
    if kind == "workflow.published":
        return "A workflow was published."
    quiet = {"mock.scenarios_configured": "The practice run was set up.", "mock.approval_steps_configured": "An approval step was added to the plan.",
             "mock.cancellation_requested": "A stop was requested for the run.", "verification.recorded": f"The result of checking {step} was saved.",
             "task_attempt.mock_cancelled_after_stop": f"{step} was stopped."}
    if kind in quiet:
        return quiet[kind]
    return f"Something happened: {kind or 'unknown event'}"


def narrate_message(message, names: dict | None = None) -> str:
    """Human sentence for a message. The API names only the sending agent instance, never the recipient or a
    readable agent name, so the sentence stays role-neutral and refers to the step when the payload carries one."""
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        return "An agent sent a message."
    kind = clean(message["type"], 64)
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    step = _name(names, payload.get("task_id"))
    what = MESSAGE_KINDS.get(kind)
    if what is None:
        return f"Something happened: {kind or 'unknown message'}"
    text = f"An agent {what}"
    if kind == "review.finding" and isinstance(payload.get("approved"), bool):
        text = "A reviewer approved the work" if payload["approved"] else "A reviewer asked for changes"
    return text + (f" on {step}." if step else ".")


DECISION_OUTCOMES = {
    "accepted": "Your decision was recorded.",
    "unauthorized": "The daemon did not accept your credentials, so nothing was changed.",
    "forbidden": "You are not allowed to decide this request, so nothing was changed.",
    "conflict": "This request changed or was already decided. Look again before deciding.",
    "failed": "We could not confirm your decision. You can try again safely.",
    "invalid": "The answer from the daemon was not understood. Check the current state.",
}


def decision_outcome_text(outcome) -> str:
    if isinstance(outcome, str) and outcome.startswith("locked@"):
        return f"The approver is locked until {outcome[7:]} UTC after failed attempts, so nothing was changed."
    return DECISION_OUTCOMES.get(outcome, DECISION_OUTCOMES["failed"]) if isinstance(outcome, str) else DECISION_OUTCOMES["failed"]
