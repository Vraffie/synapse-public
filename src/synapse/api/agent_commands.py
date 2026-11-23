"""Operator endpoints to start, inspect and cancel REAL agent runs (see docs/operations/agent-runs.md).

Thin HTTP layer: strict body parsing, then delegation to ``AgentRunService`` (which owns all
validation, side effects and cleanup). Error bodies never echo request values; the prompt is
never logged by this layer.
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Path, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from synapse.api.workflow_commands import _body, _COMMAND_HEADERS, _headers
from synapse.persistence.idempotency import IdempotencyConflict
from synapse.persistence.run_snapshot import RunNotFound
from synapse.persistence.state_store import StateConflict

# (key, request-dict) -> CommandResult; may raise AgentRunError / IdempotencyConflict
StartAgent = Callable[[str, dict], object | Awaitable[object]]
ReadAgent = Callable[[str], dict | Awaitable[dict]]
CancelAgent = Callable[[str, str], object]
# (run_id, key, expected_run_version, assertion) -> CommandResult
AbandonAgent = Callable[[str, str, int, str], object]
RemoveWorktree = Callable[[str, bool], dict | Awaitable[dict]]


class AgentRunError(Exception):
    """A refusal with an HTTP-mappable status and a fixed, input-free message."""
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class ApprovalStepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str = Field(strict=True, min_length=1, max_length=128)
    target: str = Field(strict=True, min_length=1, max_length=128)
    ttl_seconds: int | None = Field(default=None, strict=True, ge=1, le=86400)


class AbandonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_run_version: int = Field(strict=True, ge=0)
    assertion: str = Field(strict=True, min_length=1, max_length=128)


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(strict=True, min_length=1, max_length=64)
    prompt: str = Field(strict=True, min_length=1, max_length=32768)
    allowed_tools: list[str] = Field(min_length=1, max_length=50)
    permission_mode: str | None = Field(default=None, strict=True, max_length=32)
    timeout_seconds: float | None = Field(default=None, gt=0, le=3600, allow_inf_nan=False)
    depends_on: list[str] = Field(default_factory=list, max_length=6)
    approval_step: ApprovalStepRequest | None = None
    profile: str | None = Field(default=None, strict=True, pattern=r"^[A-Za-z0-9._-]{1,64}$")


class AgentRunRequest(BaseModel):
    """Either the single-task body (prompt + allowed_tools) or ``tasks`` (1 to 6, with depends_on)."""
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(strict=True, min_length=1, max_length=4096)
    base_ref: str = Field(strict=True, min_length=1, max_length=256)
    prompt: str | None = Field(default=None, strict=True, min_length=1, max_length=32768)
    allowed_tools: list[str] | None = Field(default=None, min_length=1, max_length=50)
    permission_mode: str = Field(default="acceptEdits", strict=True, max_length=32)
    timeout_seconds: float = Field(default=600, gt=0, le=3600, allow_inf_nan=False)
    approval_step: ApprovalStepRequest | None = None
    tasks: list[TaskRequest] | None = Field(default=None, min_length=1, max_length=6)
    profile: str | None = Field(default=None, strict=True, pattern=r"^[A-Za-z0-9._-]{1,64}$")

    @model_validator(mode="after")
    def _one_shape(self):
        if self.tasks is not None:
            if self.prompt is not None or self.allowed_tools is not None or self.approval_step is not None:
                raise ValueError("tasks excludes prompt, allowed_tools and approval_step")
        elif self.prompt is None or self.allowed_tools is None:
            raise ValueError("prompt and allowed_tools are required without tasks")
        return self


def _schema():
    schema = AgentRunRequest.model_json_schema()
    definitions = schema.pop("$defs")
    schema["properties"]["approval_step"] = {"anyOf": [definitions["ApprovalStepRequest"], {"type": "null"}]}
    task = {k: v for k, v in definitions["TaskRequest"].items()}
    task["properties"] = {**task["properties"], "approval_step": {"anyOf": [definitions["ApprovalStepRequest"], {"type": "null"}]}}
    schema["properties"]["tasks"] = {"anyOf": [{"type": "array", "minItems": 1, "maxItems": 6, "items": task}, {"type": "null"}]}
    return schema


def agent_command_router(start: StartAgent | None, read: ReadAgent | None, cancel: CancelAgent | None,
                         abandon: AbandonAgent | None = None,
                         remove_worktree: RemoveWorktree | None = None) -> APIRouter:
    router = APIRouter(dependencies=[Security(HTTPBearer())])

    def respond(result, status, correlation):
        return JSONResponse(result.response, status_code=status,
                            headers={"X-Correlation-ID": correlation, "Idempotency-Replayed": str(result.replayed).lower()})

    @router.post("/api/v1/agent-runs", status_code=201,
                 openapi_extra={"parameters": _COMMAND_HEADERS,
                                "requestBody": {"required": True, "content": {"application/json": {"schema": _schema()}}}})
    async def start_agent_run(request: Request):
        key, correlation = _headers(request)
        if start is None:
            raise HTTPException(503, detail="Agent runs unavailable")
        body = await _body(request, AgentRunRequest)
        data = body.model_dump()
        if data["approval_step"] is not None and data["approval_step"]["ttl_seconds"] is None:
            del data["approval_step"]["ttl_seconds"]
        if data["profile"] is None:
            del data["profile"]  # absent and null both mean the default profile
        if data["tasks"] is None:
            del data["tasks"]
        else:
            for k in ("prompt", "allowed_tools", "approval_step"):
                data.pop(k, None)
            for task in data["tasks"]:
                for k in ("permission_mode", "timeout_seconds", "profile"):
                    if task[k] is None:
                        del task[k]  # the service applies the request-level defaults
                step = task["approval_step"]
                if step is not None and step["ttl_seconds"] is None:
                    del step["ttl_seconds"]
        try:
            result = start(key, data)
            if inspect.isawaitable(result):
                result = await result
        except AgentRunError as exc:
            raise HTTPException(exc.status, detail=exc.message) from None
        except (IdempotencyConflict, StateConflict):
            raise HTTPException(409, detail="Idempotency key conflicts with prior request") from None
        return respond(result, 200 if result.replayed else 201, correlation)

    @router.get("/api/v1/runs/{run_id}/agent")
    async def get_agent(run_id: str = Path(min_length=1, max_length=256)):
        if not run_id.strip():
            raise HTTPException(422, detail="Invalid run ID")
        if read is None:
            raise HTTPException(503, detail="Agent runs unavailable")
        try:
            result = read(run_id)
            return await result if inspect.isawaitable(result) else result
        except RunNotFound:
            raise HTTPException(404, detail="Agent run not found") from None

    @router.post("/api/v1/runs/{run_id}/agent-cancel", status_code=202, openapi_extra={"parameters": _COMMAND_HEADERS})
    async def agent_cancel(request: Request, run_id: str = Path(min_length=1, max_length=256)):
        key, correlation = _headers(request)
        if not run_id.strip():
            raise HTTPException(422, detail="Invalid run ID")
        if cancel is None:
            raise HTTPException(503, detail="Agent cancellation unavailable")
        async for chunk in request.stream():
            if chunk:
                raise HTTPException(400, detail="Request body is not supported")
        try:
            result = cancel(run_id, key)
        except (IdempotencyConflict, StateConflict):
            raise HTTPException(409, detail="Agent run cannot be cancelled") from None
        except ValueError:
            raise HTTPException(422, detail="Invalid run ID") from None
        return respond(result, 202, correlation)

    @router.post("/api/v1/runs/{run_id}/agent-abandon", openapi_extra={
        "parameters": _COMMAND_HEADERS,
        "requestBody": {"required": True, "content": {"application/json": {"schema": AbandonRequest.model_json_schema()}}}})
    async def agent_abandon(request: Request, run_id: str = Path(min_length=1, max_length=256)):
        key, correlation = _headers(request)
        if not run_id.strip():
            raise HTTPException(422, detail="Invalid run ID")
        if abandon is None:
            raise HTTPException(503, detail="Agent runs unavailable")
        body = await _body(request, AbandonRequest)
        try:
            result = abandon(run_id, key, body.expected_run_version, body.assertion)
        except (IdempotencyConflict, StateConflict):
            raise HTTPException(409, detail="Agent run cannot be abandoned") from None
        except ValueError:
            raise HTTPException(422, detail="Invalid request") from None
        return respond(result, 200, correlation)

    @router.post("/api/v1/runs/{run_id}/agent-worktree-remove", openapi_extra={"parameters": _COMMAND_HEADERS})
    async def agent_worktree_remove(request: Request, run_id: str = Path(min_length=1, max_length=256),
                                    force: bool = Query(default=False)):
        _headers(request)
        if not run_id.strip():
            raise HTTPException(422, detail="Invalid run ID")
        if remove_worktree is None:
            raise HTTPException(503, detail="Agent runs unavailable")
        async for chunk in request.stream():
            if chunk:
                raise HTTPException(400, detail="Request body is not supported")
        try:
            result = remove_worktree(run_id, force)
            if inspect.isawaitable(result):
                result = await result
        except RunNotFound:
            raise HTTPException(404, detail="Agent run not found") from None
        except AgentRunError as exc:
            raise HTTPException(exc.status, detail=exc.message) from None
        return JSONResponse(result)

    return router
