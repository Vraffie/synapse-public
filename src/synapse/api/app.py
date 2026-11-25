"""Authenticated read-only local API; the caller owns daemon and transport."""
from __future__ import annotations

import asyncio
import hmac
import logging
import re
from uuid import uuid4
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Query, Request, Security, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.security import HTTPBearer
from fastapi.responses import JSONResponse

from synapse.api.operator_messages import MessageReader, ThreadReader, message_query_router
from synapse.api.run_queries import AnalyticsReader, RunListReader, RunPageReader, RunReader, run_query_router
from synapse.api.approvals import ApprovalReader, approval_router
from synapse.api.profile_commands import ClearCooldown, ListProfiles, SetCooldown, profile_router
from synapse.api.approval_decisions import DecideCommand, approval_decision_router
from synapse.api.workflow_commands import PublishCommand, RunCommand, workflow_command_router
from synapse.api.mock_commands import mock_command_router
from synapse.api.agent_commands import (AbandonAgent, CancelAgent, CommitWorktree, ReadAgent, RemoveWorktree, StartAgent,
                                      agent_command_router)
from synapse.api.manifest_commands import manifest_router
from synapse.api.openapi_contracts import install_openapi_contracts

EventReader = Callable[[int, int], list[dict[str, Any]]]


def create_app(*, token: str, read_events: EventReader, poll_seconds: float = 0.25,
               read_run: RunReader | None = None, read_tasks: RunPageReader | None = None, read_runs: RunListReader | None = None,
               read_usage: RunPageReader | None = None, read_analytics: AnalyticsReader | None = None,
               read_approvals: ApprovalReader | None = None,
               read_threads: ThreadReader | None = None, read_messages: MessageReader | None = None,
               publish_workflow: PublishCommand | None = None, create_run: RunCommand | None = None,
               start_mock: Callable | None = None, cancel_mock: Callable | None = None, resume_mock: Callable | None = None,
               decide_approval: DecideCommand | None = None, audit_denied_decision: Callable | None = None, preauthenticate_decision: Callable | None = None,
               start_agent_run: StartAgent | None = None, read_agent_run: ReadAgent | None = None,
               cancel_agent_run: CancelAgent | None = None, abandon_agent_run: AbandonAgent | None = None,
               remove_agent_worktree: RemoveWorktree | None = None,
               commit_agent_worktree: CommitWorktree | None = None,
               list_repos: Callable | None = None,
               list_profiles: ListProfiles | None = None, set_profile_cooldown: SetCooldown | None = None,
               clear_profile_cooldown: ClearCooldown | None = None,
               build_manifest: Callable | None = None, list_manifests: Callable | None = None,
               show_manifest: Callable | None = None, recheck_manifest: Callable | None = None,
               request_manifest_approval: Callable | None = None) -> FastAPI:
    """Create an operator-only app for serving on a protected Unix socket.

    The daemon must provide a reader that is safe to call on the ASGI event
    loop and keeps the daemon database ownership lock for its full lifetime.
    """
    if not isinstance(token, str) or len(token) < 32 or not token.isascii() or not token.isprintable() or " " in token:
        raise ValueError("token must be at least 32 printable non-space ASCII characters")
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    expected = ("Bearer " + token).encode("ascii")
    app = FastAPI(title="Synapse local API", version="0.9.0", docs_url=None, redoc_url=None)

    codes = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
             405: "method_not_allowed", 409: "conflict", 413: "request_too_large",
             415: "unsupported_media_type", 422: "validation_error", 500: "internal_error",
             503: "unavailable"}

    def error(request: Request, status: int, message: str, *, detail=None, details=None, headers=None):
        return JSONResponse({"code": codes.get(status, "http_error"), "message": message,
                             "details": details or {}, "correlation_id": request.state.correlation_id,
                             "detail": message if detail is None else detail}, status_code=status, headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException):
        # Public routes use fixed string details; never echo arbitrary exception objects.
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return error(request, exc.status_code, message, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        # Omit input values and context, which can contain credentials or payloads.
        items = [{"loc": list(item.get("loc", ())), "type": item.get("type", "invalid"), "msg": "Invalid value"}
                 for item in exc.errors()]
        return error(request, 422, "Request validation failed", detail=items, details={"errors": items})

    @app.middleware("http")
    async def authenticate(request, call_next):
        # This slice grants its one operator identity access to all events.
        # Never trust forwarded headers or browser origins on a local socket.
        headers = request.scope["headers"]
        correlations = [v for k, v in headers if k.lower() == b"x-correlation-id"]
        value = correlations[0] if len(correlations) == 1 else b""
        request.state.correlation_id = value.decode("ascii") if re.fullmatch(rb"[\x21-\x7e]{1,256}", value) else str(uuid4())
        credentials = [v for k, v in headers if k.lower() == b"authorization"]
        origins = [v for k, v in headers if k.lower() == b"origin"]
        if request.scope.get("client") is not None or origins or len(credentials) != 1 or not hmac.compare_digest(credentials[0], expected):
            # Never log the header value or any token material.
            logging.getLogger(__name__).warning("Operator authentication failed for %s %s", request.method, request.url.path)
            response = error(request, 401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
        else:
            try:
                response = await call_next(request)
            except Exception as exc:
                # Exception messages may contain sensitive data; log only the type.
                logging.getLogger(__name__).error("Local API request failed (%s)", type(exc).__name__)
                response = error(request, 500, "Internal server error")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        return response

    app.include_router(workflow_command_router(publish_workflow, create_run))
    app.include_router(mock_command_router(start_mock, cancel_mock, resume_mock))
    app.include_router(agent_command_router(start_agent_run, read_agent_run, cancel_agent_run, abandon_agent_run,
                                            remove_agent_worktree, commit_agent_worktree, list_repos))
    app.include_router(profile_router(list_profiles, set_profile_cooldown, clear_profile_cooldown))
    app.include_router(manifest_router(build_manifest, list_manifests, show_manifest, recheck_manifest,
                                       request_manifest_approval))
    app.include_router(approval_router(read_approvals))
    app.include_router(approval_decision_router(decide_approval, audit_denied_decision, preauthenticate_decision))
    app.include_router(message_query_router(read_threads, read_messages))

    bearer_schema = HTTPBearer()

    @app.get("/api/v1/system/health", dependencies=[Security(bearer_schema)])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/events", dependencies=[Security(bearer_schema)])
    async def events(after: int = Query(default=0, ge=0, le=9223372036854775807), limit: int = Query(default=100, ge=1, le=1000)):
        return {"events": read_events(after, limit)}

    @app.websocket("/api/v1/events/stream")
    async def stream(websocket: WebSocket):
        headers = websocket.scope["headers"]
        credentials = [v for k, v in headers if k.lower() == b"authorization"]
        origins = [v for k, v in headers if k.lower() == b"origin"]
        if websocket.scope.get("client") is not None or origins or len(credentials) != 1 or not hmac.compare_digest(credentials[0], expected):
            await websocket.close(code=1008)
            return
        cursor = websocket.query_params.get("after", "0")
        if not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 19 or int(cursor) > 9223372036854775807:
            await websocket.close(code=1008)
            return
        after = int(cursor)
        await websocket.accept()
        try:
            while True:
                batch = read_events(after, 100)
                for event in batch:
                    await websocket.send_json(event)
                    after = event["sequence"]
                # Read disconnects as well as polling for events; no client
                # messages are accepted as commands on this read-only stream.
                if len(batch) == 100:
                    continue  # Still catching up: read the next page without idling.
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=poll_seconds)
                    if message["type"] == "websocket.disconnect":
                        return
                    await websocket.close(code=1008)
                    return
                except asyncio.TimeoutError:
                    continue
        except WebSocketDisconnect:
            return

    app.include_router(run_query_router(read_run, read_tasks, read_runs, read_usage, read_analytics))
    install_openapi_contracts(app)
    return app
