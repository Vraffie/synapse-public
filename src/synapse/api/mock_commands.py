"""Start the daemon's synthetic mock fixture, never a live provider."""
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Path, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from synapse.api.workflow_commands import _body, _COMMAND_HEADERS, _headers
from synapse.persistence.idempotency import IdempotencyConflict
from synapse.persistence.state_store import StateConflict


class TaskEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(strict=True, min_length=1, max_length=256)
    expected_version: int = Field(strict=True, ge=0, le=9223372036854775807)
    unknown_attempt_id: str | None = Field(min_length=1, max_length=256)


class ResumeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_run_version: int = Field(strict=True, ge=0, le=9223372036854775807)
    tasks: list[TaskEvidence] = Field(max_length=1000)
    assertion: Literal["fixed-v1-no-external-effects"]


def _resume_schema():
    schema = ResumeEvidence.model_json_schema()
    definitions = schema.pop("$defs")
    schema["properties"]["tasks"]["items"] = definitions["TaskEvidence"]
    return schema


def mock_command_router(start: Callable | None, cancel: Callable | None = None, resume: Callable | None = None):
    router = APIRouter(dependencies=[Security(HTTPBearer())])

    @router.post("/api/v1/runs/{run_id}/mock-start", status_code=202, openapi_extra={"parameters": _COMMAND_HEADERS})
    async def mock_start(request: Request, run_id: str = Path(min_length=1, max_length=256)):
        key, correlation = _headers(request)
        if not run_id.strip():
            raise HTTPException(422, detail="Invalid run ID")
        if start is None:
            raise HTTPException(503, detail="Mock execution unavailable")
        # This command has no body or configurable verifier/scenario.
        async for chunk in request.stream():
            if chunk:
                raise HTTPException(400, detail="Request body is not supported")
        try:
            result = start(run_id, key)
        except (IdempotencyConflict, StateConflict):
            raise HTTPException(409, detail="Mock run cannot be started") from None
        return JSONResponse(result.response, status_code=202,
                            headers={"X-Correlation-ID": correlation, "Idempotency-Replayed": str(result.replayed).lower()})
    @router.post("/api/v1/runs/{run_id}/mock-cancel", status_code=202, openapi_extra={"parameters": _COMMAND_HEADERS})
    async def mock_cancel(request: Request, run_id: str = Path(min_length=1, max_length=256)):
        key, correlation = _headers(request)
        if not run_id.strip():
            raise HTTPException(422, detail="Invalid run ID")
        if cancel is None:
            raise HTTPException(503, detail="Mock cancellation unavailable")
        async for chunk in request.stream():
            if chunk:
                raise HTTPException(400, detail="Request body is not supported")
        try:
            result = cancel(run_id, key)
        except (IdempotencyConflict, StateConflict):
            raise HTTPException(409, detail="Mock run cannot be cancelled") from None
        return JSONResponse(result.response, status_code=202,
                            headers={"X-Correlation-ID": correlation, "Idempotency-Replayed": str(result.replayed).lower()})
    @router.post("/api/v1/runs/{run_id}/mock-resume", status_code=202,
                 openapi_extra={"parameters": _COMMAND_HEADERS, "requestBody": {"required": True, "content": {"application/json": {"schema": _resume_schema()}}}})
    async def mock_resume(request: Request, run_id: str = Path(min_length=1, max_length=256)):
        key, correlation = _headers(request)
        if not run_id.strip():
            raise HTTPException(422, detail="Invalid run ID")
        if resume is None:
            raise HTTPException(503, detail="Mock resume unavailable")
        evidence = await _body(request, ResumeEvidence)
        try:
            result = resume(run_id, key, correlation, evidence.expected_run_version,
                            [item.model_dump() for item in evidence.tasks], evidence.assertion)
        except (IdempotencyConflict, StateConflict):
            raise HTTPException(409, detail="Mock run cannot be resumed") from None
        except ValueError:
            raise HTTPException(422, detail="Invalid mock evidence") from None
        return JSONResponse(result.response, status_code=202,
                            headers={"X-Correlation-ID": correlation, "Idempotency-Replayed": str(result.replayed).lower()})
    return router
