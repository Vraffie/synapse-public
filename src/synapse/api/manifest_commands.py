"""Operator endpoints for candidate manifest v2 of multi-repository runs (ADR 026 D8 to D10).

Thin HTTP layer: strict body parsing, then delegation to ``ManifestService`` (which owns the refusals, audit and
all git and database work). Error bodies carry only a fixed refusal code as ``detail`` (for example
``baseline_moved`` or ``candidate_invalid:diff_mismatch``), never a path, git output or request value.
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from synapse.api.agent_commands import AgentRunError
from synapse.api.workflow_commands import _body, _COMMAND_HEADERS, _headers
from synapse.persistence.approval_environment import ALLOWED_ENVIRONMENTS
from synapse.persistence.idempotency import IdempotencyConflict
from synapse.persistence.run_snapshot import RunNotFound
from synapse.persistence.state_store import StateConflict

# Every callable may be async. Each raises RunNotFound (unknown run) or AgentRunError(status, refusal code).
BuildManifest = Callable[[str, dict | None], dict | Awaitable[dict]]
ListManifests = Callable[[str], dict | Awaitable[dict]]
ShowManifest = Callable[[str, str], dict | Awaitable[dict]]
RecheckManifest = Callable[[str, str], dict | Awaitable[dict]]
# (run_id, manifest_sha256, environment, policy_version, ttl_seconds, idempotency_key, correlation_id) -> CommandResult
RequestApproval = Callable[..., object | Awaitable[object]]

# The ADR 022 allowlist, and nothing else: widening it (for example to DEV) is its own reviewed change there.
ENVIRONMENTS = tuple(sorted(ALLOWED_ENVIRONMENTS))
MAX_ATTEMPT_NO = 2**31 - 1
TaskId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
AttemptNo = Annotated[int, Field(ge=1, le=MAX_ATTEMPT_NO)]
_SHA = Path(pattern=r"^[0-9a-f]{64}$")
_RUN = Path(min_length=1, max_length=256)


class BuildRequest(BaseModel):
    """``sources`` optionally names ``{writer_task_id: attempt_no}``; by default each writer's latest completed attempt."""
    model_config = ConfigDict(extra="forbid")
    sources: dict[TaskId, AttemptNo] | None = Field(default=None, max_length=6)


class ApprovalRequestBody(BaseModel):
    """The operation is always ``deploy`` (ADR 026 D9); the target is the environment, one of ADR 022's
    ``ALLOWED_ENVIRONMENTS``. The ADR 022 environment binding itself is not written here (deploy slice)."""
    model_config = ConfigDict(extra="forbid")
    environment: Literal[ENVIRONMENTS] = Field(json_schema_extra={"enum": list(ENVIRONMENTS)})  # type: ignore[valid-type]
    policy_version: str = Field(default="1", strict=True, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    ttl_seconds: int = Field(default=3600, strict=True, ge=1, le=3600)


def _schema(model: type[BaseModel]) -> dict:
    return model.model_json_schema()


async def _call(fn, *args):
    try:
        result = fn(*args)
        return await result if inspect.isawaitable(result) else result
    except RunNotFound:
        raise HTTPException(404, detail="Run not found") from None
    except AgentRunError as exc:
        raise HTTPException(exc.status, detail=exc.message) from None
    except IdempotencyConflict:
        raise HTTPException(409, detail="Idempotency key conflicts with prior request") from None
    except StateConflict:
        raise HTTPException(409, detail="State changed concurrently; retry") from None


def manifest_router(build: BuildManifest | None, list_: ListManifests | None, show: ShowManifest | None,
                    recheck: RecheckManifest | None, request_approval: RequestApproval | None) -> APIRouter:
    router = APIRouter(dependencies=[Security(HTTPBearer())])
    base = "/api/v1/runs/{run_id}/candidate-manifests"

    def need(fn):
        if fn is None:
            raise HTTPException(503, detail="Candidate manifests unavailable")

    def respond(body: dict, correlation: str, replayed: bool):
        return JSONResponse(body, headers={"X-Correlation-ID": correlation, "Idempotency-Replayed": str(replayed).lower()})

    @router.post(base, openapi_extra={
        "parameters": _COMMAND_HEADERS,
        "requestBody": {"required": True, "content": {"application/json": {"schema": _schema(BuildRequest)}}}})
    async def build_manifest(request: Request, run_id: str = _RUN):
        _, correlation = _headers(request)
        if not run_id.strip():
            raise HTTPException(422, detail="Invalid run ID")
        need(build)
        body = await _body(request, BuildRequest)
        out = await _call(build, run_id, body.sources)
        return respond(out, correlation, bool(out.get("replayed")))

    @router.get(base)
    async def list_manifests(run_id: str = _RUN):
        if not run_id.strip():
            raise HTTPException(422, detail="Invalid run ID")
        need(list_)
        return await _call(list_, run_id)

    @router.get(base + "/{manifest_sha256}")
    async def show_manifest(run_id: str = _RUN, manifest_sha256: str = _SHA):
        if not run_id.strip():
            raise HTTPException(422, detail="Invalid run ID")
        need(show)
        return await _call(show, run_id, manifest_sha256)

    @router.post(base + "/{manifest_sha256}/recheck", openapi_extra={"parameters": _COMMAND_HEADERS})
    async def recheck_manifest(request: Request, run_id: str = _RUN, manifest_sha256: str = _SHA):
        _, correlation = _headers(request)
        if not run_id.strip():
            raise HTTPException(422, detail="Invalid run ID")
        need(recheck)
        async for chunk in request.stream():
            if chunk:
                raise HTTPException(400, detail="Request body is not supported")
        out = await _call(recheck, run_id, manifest_sha256)
        return respond(out, correlation, False)

    @router.post(base + "/{manifest_sha256}/approval", openapi_extra={
        "parameters": _COMMAND_HEADERS,
        "requestBody": {"required": True, "content": {"application/json": {"schema": _schema(ApprovalRequestBody)}}}})
    async def request_manifest_approval(request: Request, run_id: str = _RUN, manifest_sha256: str = _SHA):
        key, correlation = _headers(request)
        if not run_id.strip():
            raise HTTPException(422, detail="Invalid run ID")
        need(request_approval)
        body = await _body(request, ApprovalRequestBody)
        result = await _call(request_approval, run_id, manifest_sha256, body.environment, body.policy_version,
                             body.ttl_seconds, key, correlation)
        return respond(result.response, correlation, result.replayed)

    return router
