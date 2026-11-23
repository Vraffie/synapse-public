"""Authenticated operator workflow publication and queued run commands."""
from __future__ import annotations

from collections.abc import Callable
import json
import re
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from synapse.persistence.idempotency import CommandResult, IdempotencyConflict
from synapse.persistence.state_store import StateConflict

PublishCommand = Callable[[str, str, list[dict], str], CommandResult]
RunCommand = Callable[[str, str, str], CommandResult]
_MAX_BODY = 1024 * 1024
_HEADER = re.compile(r"[\x21-\x7e]{1,256}\Z")
Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=256)]


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier
    dependencies: list[Identifier] = Field(max_length=1000)

    @field_validator("id", "dependencies")
    @classmethod
    def nonblank(cls, value):
        values = value if isinstance(value, list) else [value]
        if any(not item.strip() for item in values):
            raise ValueError("identifiers must not be blank")
        return value


class WorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: Identifier
    version: Identifier
    nodes: list[Node] = Field(min_length=1, max_length=1000)

    @field_validator("workflow_id", "version")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("identifier must not be blank")
        return value


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: Identifier
    workflow_version: Identifier

    @field_validator("workflow_id", "workflow_version")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("identifier must not be blank")
        return value


def _headers(request: Request) -> tuple[str, str]:
    values = []
    for name in (b"idempotency-key", b"x-correlation-id"):
        found = [v for k, v in request.scope["headers"] if k.lower() == name]
        if len(found) != 1:
            raise HTTPException(400, detail="One Idempotency-Key and X-Correlation-ID are required")
        try:
            text = found[0].decode("ascii")
        except UnicodeDecodeError:
            raise HTTPException(400, detail="Invalid command header") from None
        if _HEADER.fullmatch(text) is None:
            raise HTTPException(400, detail="Invalid command header")
        values.append(text)
    return values[0], values[1]


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


async def _body(request: Request, model: type[BaseModel]) -> BaseModel:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, detail="application/json required")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > _MAX_BODY:
            raise HTTPException(413, detail="Request too large")
    try:
        def invalid_constant(_):
            raise ValueError("non-finite JSON")
        parsed = json.loads(bytes(data), object_pairs_hook=_object, parse_constant=invalid_constant)
        return model.model_validate(parsed, strict=True)
    except (ValueError, UnicodeDecodeError, ValidationError, RecursionError):
        raise HTTPException(422, detail="Invalid request body") from None


def _workflow_schema() -> dict[str, Any]:
    schema = WorkflowRequest.model_json_schema()
    definitions = schema.pop("$defs")
    schema["properties"]["nodes"]["items"] = definitions["Node"]
    return schema


_COMMAND_HEADERS = [
    {"name": name, "in": "header", "required": True,
     "schema": {"type": "string", "minLength": 1, "maxLength": 256, "pattern": r"^[!-~]+$"}}
    for name in ("Idempotency-Key", "X-Correlation-ID")
]


def workflow_command_router(publish: PublishCommand | None, create: RunCommand | None) -> APIRouter:
    router = APIRouter(dependencies=[Security(HTTPBearer())])

    async def command(request: Request, model, callback, status: int):
        key, correlation = _headers(request)
        if callback is None:
            raise HTTPException(503, detail="Command unavailable")
        body = await _body(request, model)
        try:
            if model is WorkflowRequest:
                result = callback(body.workflow_id, body.version, [n.model_dump() for n in body.nodes], key)
            else:
                result = callback(body.workflow_id, body.workflow_version, key)
        except IdempotencyConflict:
            raise HTTPException(409, detail="Idempotency key conflicts with prior request") from None
        except StateConflict:
            raise HTTPException(409, detail="Request conflicts with current state") from None
        except sqlite3.IntegrityError as exc:
            if str(exc) == "workflow versions are immutable":
                raise HTTPException(409, detail="Workflow version already exists") from None
            raise
        except ValueError:
            raise HTTPException(422, detail="Invalid workflow or run request") from None
        return JSONResponse(result.response, status_code=status, headers={"X-Correlation-ID": correlation,
                            "Idempotency-Replayed": str(result.replayed).lower()})

    @router.post("/api/v1/workflows", status_code=201,
                 openapi_extra={"parameters": _COMMAND_HEADERS, "requestBody": {"required": True, "content": {"application/json": {"schema": _workflow_schema()}}}})
    async def publish_workflow(request: Request):
        return await command(request, WorkflowRequest, publish, 201)

    @router.post("/api/v1/runs", status_code=202,
                 openapi_extra={"parameters": _COMMAND_HEADERS, "requestBody": {"required": True, "content": {"application/json": {"schema": RunRequest.model_json_schema()}}}})
    async def create_run(request: Request):
        return await command(request, RunRequest, create, 202)

    return router
