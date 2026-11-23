"""Human approval decisions; the approver token proves a person, not the operator bearer."""
from collections.abc import Callable
import asyncio
import base64
import re

from fastapi import APIRouter, HTTPException, Path, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from synapse.api.workflow_commands import _body, _COMMAND_HEADERS, _headers
from synapse.application.approval_service import ApprovalAccessDenied
from synapse.persistence.idempotency import IdempotencyConflict
from synapse.persistence.state_store import StateConflict

_APPROVER_HEADER = {"name": "X-Approver-Token", "in": "header", "required": False,
                    "schema": {"type": "string", "pattern": r"^[A-Za-z0-9_-]{43}$"}}
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_PASSPHRASE_HEADER = {"name": "X-Approver-Passphrase", "in": "header", "required": False,
                      "schema": {"type": "string", "description": "base64url UTF-8 passphrase (passphrase mode); alternative to X-Approver-Token"}}
_CLOCK = re.compile(r"[0-2][0-9]:[0-5][0-9]")
_PASSPHRASE = re.compile(r"[A-Za-z0-9_-]{1,512}")
_HUMAN = "human:local-approver"  # conflicts are only reachable after the approver token authenticated

# decide(approver_token, approval_id, decision=..., expected_version=..., operation=..., target=...,
#        candidate_sha256=..., policy_version=..., reason=..., key=..., correlation=...) -> CommandResult
DecideCommand = Callable[..., object]


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approved", "rejected"]
    expected_version: int = Field(strict=True, ge=0, le=9223372036854775807)
    operation: str = Field(strict=True, min_length=1, max_length=256)
    target: str = Field(strict=True, min_length=1, max_length=256)
    candidate_sha256: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(strict=True, min_length=1, max_length=256)
    reason: str | None = Field(default=None, strict=True, max_length=4096)


# audit_denied(reason, correlation_id=..., http_status=..., approval_id=..., principal=...) -> bool; best effort.
AuditDenied = Callable[..., object]


def approval_decision_router(decide: DecideCommand | None, audit_denied: AuditDenied | None = None,
                             preauthenticate: Callable[[str], object] | None = None) -> APIRouter:
    router = APIRouter(dependencies=[Security(HTTPBearer())])

    def deny(status: int, detail: str, reason: str, correlation: str, approval_id: str | None = None,
             principal: str | None = None, headers: dict | None = None) -> HTTPException:
        # Only fixed codes and validated ids are passed; never the token or request text.
        if audit_denied is not None:
            try:
                audit_denied(reason, correlation_id=correlation, http_status=status,
                             approval_id=approval_id, principal=principal)
            except Exception:
                pass
        return HTTPException(status, detail=detail, headers=headers)

    @router.post("/api/v1/approvals/{approval_id}/decision",
                 openapi_extra={"parameters": [*_COMMAND_HEADERS, _APPROVER_HEADER, _PASSPHRASE_HEADER],
                                "requestBody": {"required": True, "content": {"application/json": {"schema": DecisionRequest.model_json_schema()}}}})
    async def decision(request: Request, approval_id: str = Path(min_length=1, max_length=256)):
        key, correlation = _headers(request)
        tokens = [v for k, v in request.scope["headers"] if k.lower() == b"x-approver-token"]
        phrases = [v for k, v in request.scope["headers"] if k.lower() == b"x-approver-passphrase"]
        token = ""
        try:
            if len(tokens) == 1 and not phrases:
                token = tokens[0].decode("ascii")
                if _TOKEN.fullmatch(token) is None:
                    token = ""
            elif len(phrases) == 1 and not tokens:
                encoded = phrases[0].decode("ascii")
                if _PASSPHRASE.fullmatch(encoded) is not None:
                    token = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            token = ""
        if not token:
            raise deny(401, "Approver token required", "unauthenticated", correlation)
        if not approval_id.strip():
            raise HTTPException(422, detail="Invalid approval ID")
        if decide is None:
            raise HTTPException(503, detail="Approval decisions unavailable")
        body = await _body(request, DecisionRequest)
        try:
            if preauthenticate is not None:  # slow KDF on a worker thread, never on the event loop
                await asyncio.to_thread(preauthenticate, token)
            result = decide(token, approval_id, decision=body.decision, expected_version=body.expected_version,
                            operation=body.operation, target=body.target, candidate_sha256=body.candidate_sha256,
                            policy_version=body.policy_version, reason=body.reason, key=key, correlation=correlation)
        except ApprovalAccessDenied as denied:
            code = denied.code if denied.code in ("unauthenticated", "not_permitted", "self_approval", "rate_limited") else "unauthenticated"
            principal = denied.principal if isinstance(denied.principal, str) else None
            until = getattr(denied, "locked_until", None)
            if isinstance(until, str) and _CLOCK.fullmatch(until):
                raise deny(403, f"Approval access denied: approver locked until {until} UTC", code, correlation,
                           approval_id if principal else None, principal,
                           headers={"X-Approver-Locked-Until": until}) from None
            raise deny(403, "Approval access denied", code, correlation,
                       approval_id if principal else None, principal) from None
        except (IdempotencyConflict, StateConflict):
            raise deny(409, "Approval cannot be decided", "conflict", correlation, approval_id, _HUMAN) from None
        except ValueError:
            raise HTTPException(422, detail="Invalid approval decision") from None
        return JSONResponse(result.response, status_code=200,
                            headers={"X-Correlation-ID": correlation, "Idempotency-Replayed": str(result.replayed).lower()})

    return router
