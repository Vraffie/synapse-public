"""M3 contract coverage audit for the operator HTTP API.

Uses stubbed readers/commands (no daemon) so every documented status code can be provoked. Checks:
route table <-> OpenAPI parity, every documented status produced and schema-valid, 401 without a
bearer token, pagination bounds, and that error bodies never leak traces or file paths.
"""
import json
import re

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from synapse.api.agent_commands import AgentRunError
from synapse.api.app import create_app
from synapse.api.openapi_contracts import RESPONSES
from synapse.application.analytics_views import (USAGE_FIELDS, Kind, MessageAnalytics, ProfileAnalytics, Quantity,
                                                 RunAnalytics, TaskAnalytics)
from synapse.application.approval_service import ApprovalAccessDenied
from synapse.persistence.idempotency import CommandResult, IdempotencyConflict
from synapse.persistence.paging import InvalidPageCursor
from synapse.persistence.run_list import InvalidCursor
from synapse.persistence.run_snapshot import RunNotFound
from synapse.persistence.state_store import StateConflict

TOKEN = "c" * 43
AUTH = {"Authorization": "Bearer " + TOKEN}
APPROVER = "A" * 43
SECRET = "boom /home/operator/secret_module.py Traceback (most recent call last)"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

TS = "2026-01-01T00:00:00Z"
RUN = {"id": "r1", "workflow_id": "wf", "workflow_version": "1", "state": "queued", "state_version": 1,
       "created_at": TS, "updated_at": TS}
TASK = {"id": "t1", "node_id": "n", "state": "queued", "state_version": 1, "assigned_profile_id": None,
        "attempt_count": 0, "created_at": TS, "updated_at": TS}
EVENT = {"sequence": 1, "id": "e", "aggregate_type": "run", "aggregate_id": "r1", "run_id": "r1", "type": "x",
         "schema_version": 1, "payload": {}, "occurred_at": TS, "correlation_id": None, "causation_id": None}
APPROVAL = {"id": "a1", "run_id": "r1", "operation": "op", "target": "t", "candidate_manifest_sha256": "0" * 64,
            "policy_version": "1", "state": "pending", "state_version": 0, "expires_at": TS, "requested_at": TS,
            "decided_at": None, "decision_principal": None, "decision_reason": None, "subject": None}
THREAD = {"cursor": 1, "id": "th", "subject": "s", "status": "open", "created_at": TS}
MESSAGE = {"cursor": 1, "id": "m", "sender_instance_id": "i", "type": "t", "schema_version": 1,
           "correlation_id": None, "causation_id": None, "payload": {}, "created_at": TS, "artifact_refs": []}


def _page(rows, after, limit, key="cursor"):
    return [r for r in rows if r[key] > after][:limit]


def _keyset_page(rows, limit, cursor):
    """Stub of the opaque string-cursor readers: the cursor is the last id; "bad" is a tampered cursor."""
    if cursor == "bad":
        raise InvalidPageCursor("Invalid cursor")
    rest = [r for r in rows if cursor is None or r["id"] > cursor]
    return rest[:limit], (rest[limit - 1]["id"] if len(rest) > limit else None)


def _result(body, replayed=False):
    return CommandResult(body, replayed)


def stubs():
    """Well-behaved stubs: every route succeeds."""
    events = [{**EVENT, "sequence": n} for n in range(1, 8)]
    threads = [{**THREAD, "cursor": n} for n in range(1, 8)]
    messages = [{**MESSAGE, "cursor": n} for n in range(1, 8)]
    approvals = [{**APPROVAL, "id": "a%d" % n} for n in range(1, 8)]
    agent = {"run_id": "r1", "run_state": "running", "cancellation_requested": False, "attempts_resumed": 0, "profile": "default", "outcome": None,
             "summary_text": None, "tool_names": [], "worktree": None, "tasks": []}

    def read_run(run_id):
        if run_id == "missing":
            raise RunNotFound()
        return {"run": RUN, "mock_config": None, "tasks": [TASK], "dependencies": [], "last_sequence": 1}

    tasks = [{**TASK, "id": "t%d" % n, "attempts": []} for n in range(1, 8)]
    fields = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd")
    observations = [{"id": "o%d" % n, "attempt_id": "a", "task_id": "t1", "profile_id": "p", "source": "s",
                     "confidence": "reported", "observed_at": TS, **dict.fromkeys(fields)} for n in range(1, 8)]

    def read_tasks(run_id, limit, cursor):
        if run_id == "missing":
            raise RunNotFound()
        rows, next_cursor = _keyset_page(tasks, limit, cursor)
        return {"tasks": rows, "next_cursor": next_cursor}

    def read_runs(state, limit, cursor):
        if cursor == "bad":
            raise InvalidCursor()
        if cursor == "end":
            return {"runs": [], "next_cursor": None}
        return {"runs": [{**RUN, "task_counts": {"queued": 1}, "pending_approvals": 0}][:limit], "next_cursor": None}

    def read_threads(run_id, after, limit):
        if run_id == "missing":
            raise LookupError()
        return _page(threads, after, limit)

    def read_messages(run_id, thread_id, after, limit):
        if thread_id == "missing":
            raise LookupError()
        return _page(messages, after, limit)

    def read_agent(run_id):
        if run_id == "missing":
            raise RunNotFound()
        return agent

    def read_usage(run_id, limit, cursor):
        if run_id == "missing":
            raise RunNotFound()
        rows, next_cursor = _keyset_page(observations, limit, cursor)
        return {"run_id": run_id, "attempts_total": 1, "attempts_with_usage": 1, "observations": rows,
                "totals": {f: None for f in fields},
                "coverage": {f: {"known": 0, "attempts": 1} for f in fields}, "next_cursor": next_cursor}

    def read_analytics(run_id, limit, cursor):
        if run_id == "missing":
            raise RunNotFound()
        if cursor == "bad":
            raise InvalidCursor()
        one = Quantity(Kind.MEASURED, 1, "count", "daemon_record", 1, 1, False)
        none = Quantity(Kind.UNKNOWN, None, "tokens", "provider_reported", 0, 1, False)
        usage = dict.fromkeys(USAGE_FIELDS, none)
        msgs = MessageAnalytics(*(one,) * 6)
        return RunAnalytics(
            run_id=run_id, source="tables", attempts=one, retries=Quantity(Kind.MEASURED, 0, "count", "daemon_record", 0, 0, False),
            outcome_mix={"succeeded": one}, usage=usage, attempts_with_any_usage=0,
            estimated={"m": {"est_tokens": Quantity(Kind.ESTIMATED, 5, "est_tokens", "m", 1, 1, False)}},
            messages=msgs, tasks=(TaskAnalytics("t1", one, one, {"succeeded": one}, usage),), next_cursor=None,
            per_profile={"p": ProfileAnalytics(one, usage)}, per_profile_truncated=False, not_reproducible=(),
            replay_ignored_events=None, ignored_estimate_events=0).to_dict()

    return {
        "read_analytics": read_analytics,
        "read_events": lambda after, limit: _page(events, after, limit, "sequence"),
        "read_run": read_run, "read_tasks": read_tasks, "read_usage": read_usage, "read_runs": read_runs,
        "read_approvals": lambda after, limit: [a for a in approvals if a["id"] > after][:limit],
        "read_threads": read_threads, "read_messages": read_messages,
        "publish_workflow": lambda *a, **k: _result({"workflow_id": "wf", "version": "1", "definition_sha256": "0" * 64}),
        "create_run": lambda *a, **k: _result({"run_id": "r1", "state": "queued", "tasks": {"n": "t1"}}),
        "start_mock": lambda *a, **k: _result({"run_id": "r1", "state": "running", "fixture": "fixed-v1"}),
        "cancel_mock": lambda *a, **k: _result({"run_id": "r1", "state": "cancellation_requested"}),
        "resume_mock": lambda *a, **k: _result({"run_id": "r1", "state": "running",
                                                "assertion": "fixed-v1-no-external-effects"}),
        "decide_approval": lambda *a, **k: _result({"approval_id": "a1", "state": "approved", "state_version": 1}),
        "start_agent_run": lambda *a, **k: _result({"run_id": "r1", "task_id": "t", "state": "running",
                                                    "worktree_path": "w", "repo": "r", "base_ref": "main"}),
        "list_profiles": lambda: {"profiles": [{"id": "default", "provider": "claude", "enabled": True, "max_concurrency": 2,
                                                "load": None, "cooldown_remaining_seconds": 0.0, "position": "primary"}]},
        "set_profile_cooldown": lambda profile_id, seconds: {"id": profile_id, "cooldown_remaining_seconds": float(seconds)},
        "clear_profile_cooldown": lambda profile_id: {"id": profile_id, "cooldown_remaining_seconds": 0.0},
        "read_agent_run": read_agent, "read_usage": read_usage,
        "cancel_agent_run": lambda *a, **k: _result({"run_id": "r1", "state": "cancellation_requested"}),
        "abandon_agent_run": lambda *a, **k: _result({"run_id": "r1", "state": "failed", "cancelled_tasks": 1}),
        "commit_agent_worktree": lambda run_id, message: {"run_id": run_id, "tasks": [
            {"task_id": "t1", "branch": "synapse/r/t1", "head": "a" * 40, "paths": 1}]},
        "build_manifest": _manifest_stub(MANIFEST),
        "list_manifests": _manifest_stub({"run_id": "r1", "manifests": [{
            "manifest_sha256": SHA, "version": 2, "seq": 1, "artifact_id": "art", "bindings_sha256": SHA, "created_at": TS,
            "valid": False, "invalidations": [INVALIDATION]}]}),
        "show_manifest": _manifest_stub({
            "run_id": "r1", "manifest_sha256": SHA, "version": 2, "seq": 1, "created_at": TS, "valid": True,
            "manifest": {"v": 2, "run_id": "r1", "repos": []}, "invalidations": [],
            "diffs": [{"repo_id": "portfolio", "diff": "d", "truncated": False}]}),
        "recheck_manifest": _manifest_stub({"run_id": "r1", "manifest_sha256": SHA, "valid": True, "invalidations": []}),
        "request_manifest_approval": lambda run_id, *a, **k: (_manifest_stub(None)(run_id), _result({
            "approval_id": "a1", "run_id": "r1", "manifest_sha256": SHA, "operation": "deploy", "target": "STAGING",
            "state": "requested", "state_version": 0, "expires_at": TS}))[1],
        "remove_agent_worktree": lambda run_id, force: {"run_id": run_id, "removed": True,
                                                        "tasks": [{"task_id": "t", "removed": True}]},
    }


SHA = "0" * 64
MANIFEST = {"run_id": "r1", "manifest_sha256": SHA, "artifact_id": "art", "seq": 1, "invalidated": [], "replayed": False}
INVALIDATION = {"reason": "operator", "actor": "owner", "at": TS}


def _manifest_stub(body):
    def fn(run_id, *a, **k):
        if run_id == "missing":
            raise RunNotFound()
        return body
    return fn


def raising(exc):
    def fn(*a, **k):
        raise exc
    return fn


def make_client(**overrides):
    kwargs = {**stubs(), **overrides}
    return TestClient(create_app(token=TOKEN, **kwargs), client=None, raise_server_exceptions=False)


CMD = {**AUTH, "Idempotency-Key": "key-1", "X-Correlation-ID": "corr-1", "Content-Type": "application/json"}
DECISION_HEADERS = {**CMD, "X-Approver-Token": APPROVER}
WORKFLOW_BODY = {"workflow_id": "wf", "version": "1", "nodes": [{"id": "n", "dependencies": []}]}
RUN_BODY = {"workflow_id": "wf", "workflow_version": "1"}
RESUME_BODY = {"expected_run_version": 1, "tasks": [], "assertion": "fixed-v1-no-external-effects"}
ABANDON_BODY = {"expected_run_version": 1, "assertion": "no-agent-process-remains"}
AGENT_BODY = {"repo": "/r", "base_ref": "main", "prompt": "p", "allowed_tools": ["Read"]}
DECISION_BODY = {"decision": "approved", "expected_version": 0, "operation": "op", "target": "t",
                 "candidate_sha256": "0" * 64, "policy_version": "1"}
BIG = b'{"x":"' + b"a" * (1024 * 1024 + 10) + b'"}'

# ---------------------------------------------------------------------------------------------
# Case table: route -> {documented status: (stub overrides, request kwargs)}. The meta test below
# insists the table produces every status the OpenAPI document declares for the route.
# ---------------------------------------------------------------------------------------------
BLANK = "%20"


def _cmd(path, body=None, headers=CMD):
    req = {"headers": headers}
    if body is not None:
        req["json"] = body
    return req


def _post_cases(path, body, callback, success, *, conflict=StateConflict(SECRET), blank=False, headers=CMD):
    sub = path.replace("{run_id}", "r1").replace("{approval_id}", "a1").replace("{manifest_sha256}", SHA)
    cases = {
        success: ({}, _cmd(sub, body, headers)),
        400: ({}, _cmd(sub, body, {k: v for k, v in headers.items() if k != "Idempotency-Key"})),
        500: ({callback: raising(RuntimeError(SECRET))}, _cmd(sub, body, headers)),
        503: ({callback: None}, _cmd(sub, body, headers)),
    }
    if conflict:
        cases[409] = ({callback: raising(conflict)}, _cmd(sub, body, headers))
    if blank:
        cases[422] = ({}, {**_cmd(sub, body, headers), "alt_path": sub.replace("r1", BLANK)})
    elif body is not None:
        cases[422] = ({}, _cmd(sub, {"bogus": 1}, headers))
    if body is not None:
        cases[413] = ({}, {"headers": headers, "content": BIG})
        cases[415] = ({}, {"headers": {**headers, "Content-Type": "text/plain"}, "content": b"x"})
    return cases


CASES = {
    ("/api/v1/system/health", "get"): {200: ({}, {})},
    ("/api/v1/events", "get"): {
        200: ({}, {}),
        422: ({}, {"params": {"after": "-1"}}),
        500: ({"read_events": raising(RuntimeError(SECRET))}, {}),
    },
    ("/api/v1/runs", "get"): {
        200: ({}, {}),
        422: ({}, {"params": {"limit": "0"}}),
        500: ({"read_runs": raising(RuntimeError(SECRET))}, {}),
        503: ({"read_runs": None}, {}),
    },
    ("/api/v1/approvals", "get"): {
        200: ({}, {}),
        422: ({}, {"params": {"limit": "0"}}),
        500: ({"read_approvals": raising(RuntimeError(SECRET))}, {}),
        503: ({"read_approvals": None}, {}),
    },
    ("/api/v1/workflows", "post"): _post_cases("/api/v1/workflows", WORKFLOW_BODY, "publish_workflow", 201, conflict=IdempotencyConflict(SECRET)),
    ("/api/v1/runs", "post"): _post_cases("/api/v1/runs", RUN_BODY, "create_run", 202, conflict=IdempotencyConflict(SECRET)),
    ("/api/v1/agent-runs", "post"): {
        **_post_cases("/api/v1/agent-runs", AGENT_BODY, "start_agent_run", 201),
        200: ({"start_agent_run": lambda *a, **k: _result(stubs()["start_agent_run"]().response, True)}, _cmd("", AGENT_BODY)),
    },
}


def _get_cases(path, reader, missing_arg):
    return {
        200: ({}, {}),
        404: ({}, {"alt_path": path.replace("r1", "missing") if missing_arg == "run" else path.replace("th1", "missing")}),
        422: ({}, {"alt_path": path.replace("r1", BLANK)}),
        500: ({reader: raising(RuntimeError(SECRET))}, {}),
        503: ({reader: None}, {}),
    }


CASES[("/api/v1/runs/{run_id}", "get")] = _get_cases("/api/v1/runs/r1", "read_run", "run")
CASES[("/api/v1/runs/{run_id}/tasks", "get")] = _get_cases("/api/v1/runs/r1/tasks", "read_tasks", "run")
CASES[("/api/v1/runs/{run_id}/usage", "get")] = _get_cases("/api/v1/runs/r1/usage", "read_usage", "run")
CASES[("/api/v1/runs/{run_id}/analytics", "get")] = _get_cases("/api/v1/runs/r1/analytics", "read_analytics", "run")
CASES[("/api/v1/runs/{run_id}/threads", "get")] = _get_cases("/api/v1/runs/r1/threads", "read_threads", "run")
CASES[("/api/v1/runs/{run_id}/threads/{thread_id}/messages", "get")] = _get_cases(
    "/api/v1/runs/r1/threads/th1/messages", "read_messages", "thread")
CASES[("/api/v1/runs/{run_id}/agent", "get")] = _get_cases("/api/v1/runs/r1/agent", "read_agent_run", "run")
CASES[("/api/v1/runs/{run_id}/mock-start", "post")] = _post_cases(
    "/api/v1/runs/{run_id}/mock-start", None, "start_mock", 202, blank=True)
CASES[("/api/v1/runs/{run_id}/mock-cancel", "post")] = _post_cases(
    "/api/v1/runs/{run_id}/mock-cancel", None, "cancel_mock", 202, blank=True)
CASES[("/api/v1/runs/{run_id}/agent-cancel", "post")] = _post_cases(
    "/api/v1/runs/{run_id}/agent-cancel", None, "cancel_agent_run", 202, blank=True)
CASES[("/api/v1/runs/{run_id}/agent-abandon", "post")] = {
    status: case for status, case in _post_cases(
        "/api/v1/runs/{run_id}/agent-abandon", ABANDON_BODY, "abandon_agent_run", 200, blank=True).items()
    if status not in (413, 415)}  # the contract table does not document these for this route
CASES[("/api/v1/runs/{run_id}/agent-worktree-remove", "post")] = {
    **_post_cases("/api/v1/runs/{run_id}/agent-worktree-remove", None, "remove_agent_worktree", 200,
                  conflict=AgentRunError(409, "Run is still active"), blank=True),
}
CASES[("/api/v1/runs/{run_id}/agent-worktree-commit", "post")] = {
    status: case for status, case in _post_cases(
        "/api/v1/runs/{run_id}/agent-worktree-commit", {"message": "m"}, "commit_agent_worktree", 200,
        conflict=AgentRunError(409, "Nothing to commit")).items()
    if status not in (413, 415)}  # like agent-abandon, the contract table does not document these
CASES[("/api/v1/runs/{run_id}/mock-resume", "post")] = _post_cases(
    "/api/v1/runs/{run_id}/mock-resume", RESUME_BODY, "resume_mock", 202)
CASES[("/api/v1/approvals/{approval_id}/decision", "post")] = {
    **_post_cases("/api/v1/approvals/{approval_id}/decision", DECISION_BODY, "decide_approval", 200,
                  headers=DECISION_HEADERS),
    403: ({"decide_approval": raising(ApprovalAccessDenied(SECRET, code="not_permitted", principal="p"))},
          _cmd("", DECISION_BODY, DECISION_HEADERS)),
}
MANIFESTS = "/api/v1/runs/{run_id}/candidate-manifests"
for _route, _body_, _cb in (((MANIFESTS, "post"), {}, "build_manifest"),
                            ((MANIFESTS + "/{manifest_sha256}/recheck", "post"), None, "recheck_manifest"),
                            ((MANIFESTS + "/{manifest_sha256}/approval", "post"), {"environment": "STAGING"},
                             "request_manifest_approval")):
    _cases = _post_cases(_route[0], _body_, _cb, 200, conflict=AgentRunError(409, "baseline_moved"), blank=True)
    _cases[404] = ({_cb: raising(RunNotFound())}, _cmd(_route[0].replace("{run_id}", "r1").replace("{manifest_sha256}", SHA), _body_))
    CASES[_route] = _cases
CASES[(MANIFESTS, "get")] = _get_cases(MANIFESTS.replace("{run_id}", "r1"), "list_manifests", "run")
CASES[(MANIFESTS + "/{manifest_sha256}", "get")] = _get_cases(MANIFESTS.replace("{run_id}", "r1") + "/" + SHA, "show_manifest", "run")
COOLDOWN = "/api/v1/profiles/{profile_id}/cooldown"
COOLDOWN_HEADERS = {**AUTH, "X-Correlation-ID": "corr-1", "Content-Type": "application/json"}
CASES[("/api/v1/repos", "get")] = {
    200: ({"list_repos": lambda: {"repos": [{"id": "a", "baseline_refs": ["refs/heads/main"],
                                              "default_baseline_ref": "refs/heads/main", "protected_paths": 0}]}}, {}),
    500: ({"list_repos": raising(RuntimeError(SECRET))}, {}),
    503: ({"list_repos": None}, {}),
}
CASES[("/api/v1/profiles", "get")] = {
    200: ({}, {}),
    500: ({"list_profiles": raising(RuntimeError(SECRET))}, {}),
    503: ({"list_profiles": None}, {}),
}
CASES[(COOLDOWN, "post")] = {
    200: ({}, {"headers": COOLDOWN_HEADERS, "json": {"seconds": 60}}),
    422: ({"set_profile_cooldown": raising(AgentRunError(422, "unknown profile"))},
          {"headers": COOLDOWN_HEADERS, "json": {"seconds": 60}}),
    500: ({"set_profile_cooldown": raising(RuntimeError(SECRET))}, {"headers": COOLDOWN_HEADERS, "json": {"seconds": 60}}),
    503: ({"set_profile_cooldown": None}, {"headers": COOLDOWN_HEADERS, "json": {"seconds": 60}}),
    413: ({}, {"headers": COOLDOWN_HEADERS, "content": BIG}),
    415: ({}, {"headers": {**COOLDOWN_HEADERS, "Content-Type": "text/plain"}, "content": b"x"}),
}
CASES[(COOLDOWN, "delete")] = {
    200: ({}, {"headers": COOLDOWN_HEADERS}),
    422: ({"clear_profile_cooldown": raising(AgentRunError(422, "unknown profile"))}, {"headers": COOLDOWN_HEADERS}),
    500: ({"clear_profile_cooldown": raising(RuntimeError(SECRET))}, {"headers": COOLDOWN_HEADERS}),
    503: ({"clear_profile_cooldown": None}, {"headers": COOLDOWN_HEADERS}),
}
# the mock/agent cancel routes also need a non-empty body case for 400 (headers case is covered above)


def _url(route, request):
    path, _ = route
    if "alt_path" in request:
        return request["alt_path"]
    return (path.replace("{run_id}", "r1").replace("{approval_id}", "a1").replace("{manifest_sha256}", SHA).replace("{thread_id}", "th1").replace("{profile_id}", "reader"))


def fire(route, status):
    overrides, request = CASES[route][status]
    request = dict(request)
    url = _url(route, request)
    request.pop("alt_path", None)
    method = route[1]
    if method == "get":
        request.setdefault("headers", AUTH)
    with make_client(**overrides) as client:
        return client.request(method.upper(), url, **request)


def _openapi():
    with make_client() as client:
        return client.get("/openapi.json", headers=AUTH).json()


DOC = _openapi()
DOC_ROUTES = {(p, m) for p, item in DOC["paths"].items() for m in item if m in HTTP_METHODS}
ALL_CASES = [(route, status) for route in sorted(CASES) for status in sorted(CASES[route])]
DOCUMENTED = [(route, int(s)) for route in sorted(DOC_ROUTES) for s in DOC["paths"][route[0]][route[1]]["responses"]
              if s != "422_" and s.isdigit()]
LEAK = re.compile(r"Traceback|File \"|site-packages|/home/|\.py\b|secret_module|RuntimeError|StateConflict|boom|sqlite",
                  re.I)


def schema_for(route, status):
    return DOC["paths"][route[0]][route[1]]["responses"][str(status)]["content"]["application/json"]["schema"]


# (1) route table parity ----------------------------------------------------------------------
def test_every_openapi_route_is_declared_and_no_entry_is_stale():
    assert DOC_ROUTES - set(RESPONSES) == set(), "routes missing from the contract table"
    assert set(RESPONSES) - DOC_ROUTES == set(), "stale contract entries"


def test_case_table_covers_exactly_the_documented_routes():
    assert set(CASES) == DOC_ROUTES, "test case table out of sync with the API"


def test_every_contract_documents_its_success_status_with_a_schema():
    for (path, method), (status, schema) in RESPONSES.items():
        declared = DOC["paths"][path][method]["responses"][str(status)]
        assert declared["content"]["application/json"]["schema"] == schema
        Draft202012Validator.check_schema(schema)


# (2) documented status codes are produced and bodies validate --------------------------------
HEALTH = ("/api/v1/system/health", "get")


def test_health_documents_exactly_200_and_401():
    assert set(DOC["paths"][HEALTH[0]][HEALTH[1]]["responses"]) == {"200", "401"}


def test_every_documented_health_status_is_produced_by_a_case():
    documented = {int(s) for s in DOC["paths"][HEALTH[0]][HEALTH[1]]["responses"]}
    assert 200 in CASES[HEALTH]
    produced = {fire(HEALTH, 200).status_code}
    with make_client() as client:  # 401: the same route without credentials
        unauth = client.get(HEALTH[0])
    produced.add(unauth.status_code)
    Draft202012Validator(schema_for(HEALTH, 401)).validate(unauth.json())
    assert produced == documented


@pytest.mark.parametrize("route,status", ALL_CASES, ids=lambda v: str(v))
def test_case_returns_documented_status_and_schema_valid_body(route, status):
    response = fire(route, status)
    assert response.status_code == status
    assert str(status) in DOC["paths"][route[0]][route[1]]["responses"], "status produced but undocumented"
    validator = Draft202012Validator(schema_for(route, status))
    validator.validate(response.json())
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-correlation-id"]
    if status >= 400:
        assert not LEAK.search(response.text), response.text
        body = response.json()
        assert set(body) == {"code", "message", "details", "correlation_id", "detail"}


def test_every_documented_status_has_a_case():
    for route, status in DOCUMENTED:
        if status == 401:  # 401 is exercised for every route below
            continue
        assert status in CASES[route], (route, status)


# (3) authentication ---------------------------------------------------------------------------
ROUTES = sorted(DOC_ROUTES)


def _bare(route):
    path = route[0].replace("{run_id}", "r1").replace("{approval_id}", "a1").replace("{manifest_sha256}", SHA).replace("{thread_id}", "th1").replace("{profile_id}", "reader")
    return route[1].upper(), path


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: "%s %s" % (r[1], r[0]))
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong" + "x" * 40}, {"Authorization": "Basic abc"},
                                      {"Authorization": TOKEN}], ids=["none", "wrong", "basic", "no-scheme"])
def test_every_route_rejects_missing_or_bad_bearer_without_leaking(route, headers):
    method, path = _bare(route)
    # Valid command headers and body so only authentication can explain the refusal.
    extra = {k: v for k, v in DECISION_HEADERS.items() if k != "Authorization"}
    with make_client() as client:
        response = client.request(method, path, headers={**extra, **headers}, json={} if method == "POST" else None)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    body = response.json()
    assert body["code"] == "unauthorized" and body["message"] == "Authentication required"
    assert body["details"] == {} and not LEAK.search(response.text)
    for secret in (TOKEN, APPROVER):
        assert secret not in response.text


def test_unauthenticated_requests_never_reach_handlers():
    calls = []
    spy = lambda *a, **k: calls.append(a)  # noqa: E731
    with make_client(read_events=spy, read_run=spy, read_runs=spy, publish_workflow=spy, decide_approval=spy) as client:
        for route in ROUTES:
            method, path = _bare(route)
            client.request(method, path, json={} if method == "POST" else None)
    assert calls == []


def test_unknown_route_and_openapi_document_also_require_auth():
    with make_client() as client:
        assert client.get("/openapi.json").status_code == 401
        assert client.get("/nope").status_code == 401
        assert client.get("/nope", headers=AUTH).status_code == 404


def test_decision_approver_token_failures_are_401_without_leak():
    for bad in ("", "short", "!" * 43):
        with make_client() as client:
            r = client.post("/api/v1/approvals/a1/decision", json=DECISION_BODY,
                            headers={**CMD, **({"X-Approver-Token": bad} if bad else {})})
        assert r.status_code == 401 and r.json()["message"] == "Approver token required"
        assert bad not in r.text or bad == ""


# (4) pagination -------------------------------------------------------------------------------
INT_MAX = 9223372036854775807
PAGED = [  # path, list key, max limit, cursor param is integer
    ("/api/v1/events", "events", 1000),
    ("/api/v1/approvals", "approvals", 1000),
    ("/api/v1/runs/r1/threads", "threads", 100),
    ("/api/v1/runs/r1/threads/th1/messages", "messages", 100),
    ("/api/v1/runs/r1/tasks", "tasks", 500),
    ("/api/v1/runs/r1/usage", "observations", 500),
]
INTEGER_CURSOR_PAGED = [p for p in PAGED if "approvals" not in p[0] and p[1] not in ("tasks", "observations")]
STRING_CURSOR_PAGED = [p for p in PAGED if p[1] in ("tasks", "observations")]


def get(path, **params):
    with make_client() as client:
        return client.get(path, params=params, headers=AUTH)


@pytest.mark.parametrize("path,key,maximum", PAGED)
@pytest.mark.parametrize("limit", ["0", "-1", "abc", "1.5", "", "99999999999999999999"])
def test_list_limit_below_min_or_malformed_is_422(path, key, maximum, limit):
    r = get(path, limit=limit)
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    # Check the validation detail only: the body also holds a random correlation id that can contain "abc" by chance.
    assert len(limit) < 3 or limit not in json.dumps(r.json()["detail"])


@pytest.mark.parametrize("path,key,maximum", PAGED)
def test_list_limit_over_max_is_422_and_max_is_accepted(path, key, maximum):
    assert get(path, limit=maximum + 1).status_code == 422
    r = get(path, limit=maximum)
    assert r.status_code == 200 and len(r.json()[key]) <= maximum


@pytest.mark.parametrize("path,key,maximum", PAGED)
def test_list_limit_one_returns_exactly_one_item(path, key, maximum):
    r = get(path, limit=1)
    assert r.status_code == 200 and len(r.json()[key]) == 1


@pytest.mark.parametrize("path,key,maximum", INTEGER_CURSOR_PAGED)
@pytest.mark.parametrize("after", ["-1", "abc", "1.5", str(INT_MAX + 1)])
def test_integer_cursor_after_invalid_is_422(path, key, maximum, after):
    r = get(path, after=after)
    assert r.status_code == 422 and (len(after) < 4 or after not in r.text)


@pytest.mark.parametrize("path,key,maximum", INTEGER_CURSOR_PAGED)
def test_integer_cursor_past_the_end_is_an_empty_page(path, key, maximum):
    for after in (7, 8, 10 ** 6, INT_MAX):
        r = get(path, after=after)
        assert r.status_code == 200 and r.json() == {key: []}


@pytest.mark.parametrize("path,key,maximum", INTEGER_CURSOR_PAGED)
def test_integer_cursor_pages_without_overlap(path, key, maximum):
    first = get(path, limit=3).json()[key]
    second = get(path, after=first[-1].get("sequence", first[-1].get("cursor")), limit=3).json()[key]
    cursors = [x.get("sequence", x.get("cursor")) for x in first + second]
    assert cursors == [1, 2, 3, 4, 5, 6]


def test_approvals_string_cursor_past_end_is_empty_and_overlong_is_422():
    assert get("/api/v1/approvals", after="zzzz").json() == {"approvals": []}
    assert get("/api/v1/approvals", after="a" * 256).status_code == 200
    assert get("/api/v1/approvals", after="a" * 257).status_code == 422


def test_runs_list_limit_bounds_and_cursor():
    assert get("/api/v1/runs", limit=200).status_code == 200
    for limit in ("0", "-5", "201", "x", "1.5"):
        assert get("/api/v1/runs", limit=limit).status_code == 422
    assert get("/api/v1/runs", cursor="end").json() == {"runs": [], "next_cursor": None}
    assert get("/api/v1/runs", cursor="bad").status_code == 422
    assert get("/api/v1/runs", cursor="").status_code == 422
    assert get("/api/v1/runs", cursor="c" * 1025).status_code == 422
    assert get("/api/v1/runs", state="not-a-state").status_code == 422


def test_pagination_defaults_apply_without_params():
    seen = []
    with make_client(read_events=lambda a, l: seen.append((a, l)) or [],
                     read_threads=lambda r, a, l: seen.append((a, l)) or [],
                     read_tasks=lambda r, l, c: seen.append((r, l, c)) or {"tasks": [], "next_cursor": None},
                     read_usage=lambda r, l, c: seen.append((r, l, c)) or {}) as client:
        client.get("/api/v1/events", headers=AUTH)
        client.get("/api/v1/runs/r1/threads", headers=AUTH)
        client.get("/api/v1/runs/r1/tasks", headers=AUTH)
        client.get("/api/v1/runs/r1/usage", headers=AUTH)
    assert seen == [(0, 100), (0, 100), ("r1", 200, None), ("r1", 200, None)]


@pytest.mark.parametrize("path,key,maximum", STRING_CURSOR_PAGED)
def test_string_cursor_pages_follow_next_cursor_without_overlap(path, key, maximum):
    first = get(path, limit=3).json()
    second = get(path, limit=3, cursor=first["next_cursor"]).json()
    last = get(path, limit=3, cursor=second["next_cursor"]).json()
    ids = [x["id"] for x in first[key] + second[key] + last[key]]
    assert len(ids) == 7 == len(set(ids)) and last["next_cursor"] is None and first["next_cursor"] is not None


@pytest.mark.parametrize("path,key,maximum", STRING_CURSOR_PAGED)
@pytest.mark.parametrize("cursor", ["bad", "", "c" * 1025])
def test_string_cursor_invalid_is_422_without_echo(path, key, maximum, cursor):
    r = get(path, cursor=cursor)
    assert r.status_code == 422 and not LEAK.search(r.text)
    assert len(cursor) < 4 or cursor not in r.text


# (5) error bodies never leak ------------------------------------------------------------------
def test_unhandled_exception_body_is_generic_and_correlated():
    exc = RuntimeError(SECRET)
    with make_client(read_events=raising(exc), read_run=raising(exc)) as client:
        for path in ("/api/v1/events", "/api/v1/runs/r1"):
            r = client.get(path, headers={**AUTH, "X-Correlation-ID": "trace-9"})
            assert r.status_code == 500
            assert r.json()["message"] == "Internal server error" and r.json()["correlation_id"] == "trace-9"
            assert not LEAK.search(r.text)


def test_validation_and_http_errors_echo_no_input_or_paths():
    marker = "zz-marker-/etc/passwd"
    with make_client() as client:
        for r in (client.get("/api/v1/events", params={"after": marker}, headers=AUTH),
                  client.get("/api/v1/runs", params={"state": marker}, headers=AUTH),
                  client.post("/api/v1/workflows", json={"workflow_id": marker, "bogus": marker}, headers=CMD),
                  client.post("/api/v1/agent-runs", json={**AGENT_BODY, "repo": 5, "prompt": marker, "x": marker}, headers=CMD),
                  client.get("/nope/" + marker, headers=AUTH)):
            assert r.status_code in (404, 422)
            assert marker not in r.text and "/etc/passwd" not in r.text and not LEAK.search(r.text)


def test_conflict_and_access_denied_bodies_are_fixed_strings():
    with make_client(decide_approval=raising(ApprovalAccessDenied(SECRET, code="self_approval", principal="p"))) as client:
        r = client.post("/api/v1/approvals/a1/decision", json=DECISION_BODY, headers=DECISION_HEADERS)
        assert r.status_code == 403 and r.json()["message"] == "Approval access denied"
        assert not LEAK.search(r.text)
    with make_client(start_agent_run=raising(AgentRunError(503, "Agent runs unavailable"))) as client:
        r = client.post("/api/v1/agent-runs", json=AGENT_BODY, headers=CMD)
        assert r.status_code == 503 and not LEAK.search(r.text)
    with make_client(publish_workflow=raising(IdempotencyConflict(SECRET))) as client:
        r = client.post("/api/v1/workflows", json=WORKFLOW_BODY, headers=CMD)
        assert r.status_code == 409 and not LEAK.search(r.text)


def test_mock_and_cancel_reject_non_empty_bodies_with_400():
    for path in ("/api/v1/runs/r1/mock-start", "/api/v1/runs/r1/mock-cancel", "/api/v1/runs/r1/agent-cancel"):
        with make_client() as client:
            r = client.post(path, content=b"{}", headers=CMD)
        assert r.status_code == 400 and not LEAK.search(r.text)
