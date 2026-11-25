"""Schemas for currently implemented operator API responses."""
from copy import deepcopy

from fastapi.openapi.utils import get_openapi

from synapse.api.manifest_contracts import MANIFEST_RESPONSES


def _object(properties, *, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required}


def _nullable(kind="string"):
    return {"anyOf": [{"type": kind}, {"type": "null"}]}


STRING = {"type": "string"}
INTEGER = {"type": "integer"}
JSON_OBJECT = {"type": "object", "additionalProperties": True}
EVENT = _object({"sequence": INTEGER, "id": STRING, "aggregate_type": STRING, "aggregate_id": STRING,
                 "run_id": _nullable(), "type": STRING, "schema_version": INTEGER, "payload": JSON_OBJECT,
                 "occurred_at": STRING, "correlation_id": _nullable(), "causation_id": _nullable()})
RUN = _object({"id": STRING, "workflow_id": STRING, "workflow_version": STRING, "state": STRING,
               "state_version": INTEGER, "created_at": STRING, "updated_at": STRING})
TASK_FIELDS = {"id": STRING, "node_id": STRING, "state": STRING, "state_version": INTEGER,
               "assigned_profile_id": _nullable(), "attempt_count": INTEGER, "created_at": STRING, "updated_at": STRING}
ATTEMPT = _object({"id": STRING, "attempt_no": INTEGER, "profile_id": _nullable(), "state": STRING,
                   "session_id": _nullable(), "worktree_path": _nullable(), "started_at": _nullable(),
                   "ended_at": _nullable(), "exit_code": _nullable("integer")})
TASK = _object(TASK_FIELDS)
TASK_DETAIL = _object({**TASK_FIELDS, "attempts": {"type": "array", "items": ATTEMPT}})
DEPENDENCY = _object({"task_id": STRING, "depends_on_id": STRING})
_SUBJECT_COMMON = {
    "allowed_tools": {"type": "array", "items": STRING}, "permission_mode": STRING,
    "timeout_seconds": {"type": "number"}, "prompt_bytes": INTEGER, "prompt_preview": _nullable(),
    "allowed_tools_truncated": {"type": "boolean"}, "prompt_preview_truncated": {"type": "boolean"},
    "prompt_unavailable": {"type": "boolean"}, "spec_sha256": STRING}
_AGENT_RUN_SUBJECT = _object({
    "kind": {"type": "string", "const": "agent_run"}, "node_id": STRING, "repo": _nullable(), "base_ref": _nullable(),
    "worktree_head": _nullable(), **_SUBJECT_COMMON})
_SUBJECT_REPO = _object({"repo_id": STRING, "access": {"type": "string", "enum": ["write", "read"]},
                         "baseline_ref": STRING, "baseline_sha": STRING})
_AGENT_RUN_MULTI_SUBJECT = _object({
    "kind": {"type": "string", "const": "agent_run_multi"}, "node_id": STRING,
    "repos": {"type": "array", "items": _SUBJECT_REPO}, **_SUBJECT_COMMON})
_MANIFEST_REPO = _object({**_SUBJECT_REPO["properties"], "candidate_sha": _nullable(),
                          "changed_paths": _nullable("integer"), "diff_bytes": _nullable("integer")})
_MANIFEST_SUBJECT = _object({
    "kind": {"type": "string", "const": "candidate_manifest"}, "manifest_sha256": STRING, "seq": INTEGER,
    "bindings_sha256": STRING,
    "invalidated": {"type": "array", "items": {"type": "string",
                                               "enum": ["baseline_moved", "candidate_invalid", "operator"]}},
    "repos": {"anyOf": [{"type": "null"}, {"type": "array", "items": _MANIFEST_REPO}]},
    "manifest_unavailable": {"type": "boolean"}})
APPROVAL_SUBJECT = {"anyOf": [{"type": "null"}, _AGENT_RUN_SUBJECT, _AGENT_RUN_MULTI_SUBJECT, _MANIFEST_SUBJECT],
    "description": "Read-only, bounded description of what an approval authorizes: agent_run (single repository), "
                   "agent_run_multi (multi-repository task: repositories, access and pinned baselines, no paths) or "
                   "candidate_manifest (an approval bound to a v2 candidate manifest: per-repository baseline and "
                   "candidate commits, invalidation reasons); null for other approvals. "
                   "prompt_preview (at most 8 KiB) is only present while the approval is pending."}
APPROVAL = _object({"id": STRING, "run_id": STRING, "operation": STRING, "target": STRING,
                    "candidate_manifest_sha256": STRING, "policy_version": STRING, "state": STRING,
                    "state_version": INTEGER, "expires_at": STRING, "requested_at": STRING,
                    "decided_at": _nullable(), "decision_principal": _nullable(), "decision_reason": _nullable(),
                    "subject": APPROVAL_SUBJECT})
RUN_SUMMARY = _object({**RUN["properties"], "task_counts": {"type": "object", "additionalProperties": INTEGER},
                       "pending_approvals": INTEGER})
THREAD = _object({"cursor": INTEGER, "id": STRING, "subject": STRING, "status": STRING, "created_at": STRING})
MESSAGE = _object({"cursor": INTEGER, "id": STRING, "sender_instance_id": STRING, "type": STRING,
                   "schema_version": INTEGER, "correlation_id": _nullable(), "causation_id": _nullable(),
                   "payload": JSON_OBJECT, "created_at": STRING, "artifact_refs": {"type": "array", "items": STRING}})
AGENT_WORKTREE = {"anyOf": [{"type": "null"}, _object({"exists": {"type": ["boolean", "null"]}}, required=["exists"])],
                  "description": "exists=true adds clean, head, branch, changed_paths, diff_stat, diff, diff_truncated."}
AGENT_TASK = _object({"task_id": STRING, "node_id": STRING, "state": STRING,
                      "profile_id": {"type": ["string", "null"], "description": "Profile the task was claimed under; null before the claim."},
                      "spec": JSON_OBJECT,
                      "attempt": {"anyOf": [{"type": "null"}, JSON_OBJECT]},
                      "result": {"anyOf": [{"type": "null"}, JSON_OBJECT]}, "worktree": AGENT_WORKTREE})
ERROR = _object({"code": STRING, "message": STRING, "details": JSON_OBJECT, "correlation_id": STRING,
                 "detail": {"description": "Legacy error detail retained for compatibility."}})


def _page(name, item):
    return _object({name: {"type": "array", "items": item}})


QUANTITY = _object({"kind": {"type": "string", "enum": ["measured", "estimated", "unknown"]},
                    "value": {"anyOf": [{"type": "null"}, {"type": "integer"}, {"type": "number"}]},
                    "unit": STRING, "basis": STRING, "known": INTEGER, "of": INTEGER, "lower_bound": {"type": "boolean"}})
_QUANTITIES = {"type": "object", "additionalProperties": QUANTITY}
_USAGE_QUANTITIES = _object({f: QUANTITY for f in (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd")})
ANALYTICS = _object({
    "run_id": STRING, "source": STRING, "attempts": QUANTITY, "retries": QUANTITY, "outcome_mix": _QUANTITIES,
    "usage": _USAGE_QUANTITIES, "attempts_with_any_usage": INTEGER,
    "estimated": {"type": "object", "additionalProperties": _QUANTITIES},
    "messages": _object({f: QUANTITY for f in (
        "sent", "deliveries_created", "claimed", "redeliveries", "acknowledged", "dead_lettered")}),
    "tasks": {"type": "array", "items": _object({
        "task_id": STRING, "attempts": QUANTITY, "retries": QUANTITY, "outcome_mix": _QUANTITIES,
        "usage": _USAGE_QUANTITIES})},
    "next_cursor": _nullable(),
    "per_profile": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": _object({
        "attempts": QUANTITY, "usage": _USAGE_QUANTITIES})}]},
    "per_profile_truncated": {"type": "boolean"},
    "not_reproducible": {"type": "array", "items": STRING},
    "replay_ignored_events": _nullable("integer"), "ignored_estimate_events": INTEGER})

RESPONSES = {
    ("/api/v1/system/health", "get"): (200, _object({"status": {"type": "string", "const": "ok"}})),
    ("/api/v1/events", "get"): (200, _page("events", EVENT)),
    ("/api/v1/runs", "get"): (200, _object({"runs": {"type": "array", "items": RUN_SUMMARY}, "next_cursor": _nullable()})),
    ("/api/v1/runs/{run_id}", "get"): (200, _object({"run": RUN, "mock_config": {"anyOf": [{"type": "null"}, _object({"fixture": STRING, "profile_id": STRING, "provider": _nullable()})]}, "tasks": {"type": "array", "items": TASK}, "dependencies": {"type": "array", "items": DEPENDENCY}, "last_sequence": INTEGER})),
    ("/api/v1/runs/{run_id}/tasks", "get"): (200, _object({"tasks": {"type": "array", "items": TASK_DETAIL},
                                                           "next_cursor": _nullable()})),
    ("/api/v1/runs/{run_id}/usage", "get"): (200, _object({
        "run_id": STRING, "attempts_total": INTEGER, "attempts_with_usage": INTEGER,
        "observations": {"type": "array", "items": _object({
            "id": STRING, "attempt_id": STRING, "task_id": STRING, "profile_id": STRING, "source": STRING,
            "confidence": STRING, "observed_at": STRING, "input_tokens": _nullable("integer"),
            "output_tokens": _nullable("integer"), "cache_read_tokens": _nullable("integer"),
            "cache_write_tokens": _nullable("integer"), "cost_usd": _nullable("number")})},
        "totals": _object({"input_tokens": _nullable("integer"), "output_tokens": _nullable("integer"),
                           "cache_read_tokens": _nullable("integer"), "cache_write_tokens": _nullable("integer"),
                           "cost_usd": _nullable("number")}),
        "coverage": _object({f: _object({"known": INTEGER, "attempts": INTEGER}) for f in (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd")}),
        "next_cursor": _nullable()})),
    ("/api/v1/runs/{run_id}/analytics", "get"): (200, ANALYTICS),
    ("/api/v1/runs/{run_id}/threads", "get"): (200, _page("threads", THREAD)),
    ("/api/v1/runs/{run_id}/threads/{thread_id}/messages", "get"): (200, _page("messages", MESSAGE)),
    ("/api/v1/approvals", "get"): (200, _page("approvals", APPROVAL)),
    ("/api/v1/workflows", "post"): (201, _object({"workflow_id": STRING, "version": STRING, "definition_sha256": STRING})),
    ("/api/v1/runs", "post"): (202, _object({"run_id": STRING, "state": {"type": "string", "const": "queued"}, "tasks": {"type": "object", "additionalProperties": STRING}})),
    ("/api/v1/runs/{run_id}/mock-start", "post"): (202, _object({"run_id": STRING, "state": {"type": "string", "const": "running"}, "fixture": {"type": "string", "const": "fixed-v1"}})),
    ("/api/v1/runs/{run_id}/mock-resume", "post"): (202, _object({"run_id": STRING, "state": {"type": "string", "const": "running"}, "assertion": {"type": "string", "const": "fixed-v1-no-external-effects"}})),
    ("/api/v1/approvals/{approval_id}/decision", "post"): (200, _object({"approval_id": STRING, "state": {"type": "string", "enum": ["approved", "rejected"]}, "state_version": INTEGER})),
    ("/api/v1/agent-runs", "post"): (201, _object({"run_id": STRING, "task_id": STRING, "state": {"type": "string", "const": "running"},
                                                   "worktree_path": STRING, "repo": STRING, "base_ref": STRING})),
    ("/api/v1/runs/{run_id}/agent", "get"): (200, _object({"run_id": STRING, "run_state": STRING, "cancellation_requested": {"type": "boolean"},
                                                           "attempts_resumed": INTEGER,
                                                           "profile": {"type": ["string", "null"], "description": "Run-level profile id selected at creation ('default' when none was named); null for runs started before it was recorded."},
                                                           "outcome": _nullable(), "summary_text": _nullable(),
                                                           "tool_names": {"type": "array", "items": STRING},
                                                           "worktree": {"anyOf": [{"type": "null"}, _object({"status": STRING, "head": STRING, "diff_stat": STRING, "diff": STRING, "truncated": {"type": "boolean"}})]},
                                                           "tasks": {"type": "array", "items": AGENT_TASK}})),
    ("/api/v1/runs/{run_id}/agent-cancel", "post"): (202, _object({"run_id": STRING, "state": {"type": "string", "const": "cancellation_requested"}})),
    ("/api/v1/runs/{run_id}/agent-abandon", "post"): (200, _object({"run_id": STRING, "state": {"type": "string", "const": "failed"}, "cancelled_tasks": INTEGER})),
    ("/api/v1/runs/{run_id}/agent-worktree-remove", "post"): (200, _object({"run_id": STRING, "removed": {"type": "boolean"}, "tasks": {"type": "array", "items": _object({"task_id": STRING, "removed": {"type": "boolean"}})}})),
    ("/api/v1/runs/{run_id}/agent-worktree-commit", "post"): (200, _object({"run_id": STRING, "tasks": {"type": "array", "items": _object({"task_id": STRING, "branch": STRING, "head": STRING, "paths": INTEGER})}})),
    ("/api/v1/profiles", "get"): (200, _object({"profiles": {"type": "array", "items": _object({
        "id": STRING, "provider": STRING, "enabled": {"type": "boolean"}, "max_concurrency": INTEGER,
        "load": _nullable("integer"), "cooldown_remaining_seconds": {"type": "number"},
        "position": _nullable()})}})),
    ("/api/v1/profiles/{profile_id}/cooldown", "post"): (200, _object({"id": STRING, "cooldown_remaining_seconds": {"type": "number"}})),
    ("/api/v1/profiles/{profile_id}/cooldown", "delete"): (200, _object({"id": STRING, "cooldown_remaining_seconds": {"type": "number"}})),
    ("/api/v1/runs/{run_id}/mock-cancel", "post"): (202, _object({"run_id": STRING, "state": {"type": "string", "const": "cancellation_requested"}})),
}
RESPONSES.update(MANIFEST_RESPONSES)


def install_openapi_contracts(app):
    """Describe existing wire responses without filtering or rewriting payloads."""
    def openapi():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            for (path, method), (status, response_schema) in RESPONSES.items():
                operation = schema["paths"][path][method]
                headers = {"X-Correlation-ID": {"schema": STRING, "description": "Validated request correlation ID, or a generated ID."},
                           "Cache-Control": {"schema": STRING, "description": "Always no-store."}}
                if method == "post" and not path.startswith("/api/v1/profiles"):
                    headers["Idempotency-Replayed"] = {"schema": {"type": "string", "enum": ["true", "false"]}}
                operation["responses"][str(status)] = {"description": "Successful response", "headers": headers,
                                                        "content": {"application/json": {"schema": deepcopy(response_schema)}}}
                if path == "/api/v1/agent-runs":  # an idempotent replay answers 200 with the same body
                    operation["responses"]["200"] = deepcopy(operation["responses"][str(status)])
                    operation["responses"]["200"]["description"] = "Idempotent replay of an earlier start"
                errors = {401, 422, 500}
                if path == "/api/v1/system/health":  # no input to validate and no failing dependency
                    errors = {401}
                if path.startswith("/api/v1/runs/{run_id}") and (method == "get" or "/candidate-manifests" in path):
                    errors.update({404, 503})
                if path == "/api/v1/approvals" or (path == "/api/v1/runs" and method == "get"):
                    errors.add(503)
                if path == "/api/v1/profiles":
                    errors = {401, 500, 503}  # no input to validate
                elif path.startswith("/api/v1/profiles/"):  # not idempotency-keyed, no state conflicts (see profile_commands)
                    errors.add(503)
                    if method == "post":
                        errors.update({413, 415})
                elif method == "post":
                    errors.update({400, 409, 503})
                    if path == "/api/v1/approvals/{approval_id}/decision":
                        errors.update({403, 413, 415})
                    if path in ("/api/v1/workflows", "/api/v1/runs", "/api/v1/runs/{run_id}/mock-resume", "/api/v1/agent-runs"):
                        errors.update({413, 415})
                if "/candidate-manifests" in path and method == "post" and not path.endswith("/recheck"):
                    errors.update({413, 415})
                for error_status in sorted(errors):
                    operation["responses"][str(error_status)] = {"description": "Error response", "content": {"application/json": {"schema": deepcopy(ERROR)}}}
            app.openapi_schema = schema
        return app.openapi_schema
    app.openapi = openapi
