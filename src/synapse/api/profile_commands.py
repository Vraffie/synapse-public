"""Operator endpoints for agent profiles (ADR 016 decisions 6 and 8): list, set and clear a cooldown.

Thin HTTP layer over ``AgentRunService``. A cooldown only makes tasks that name the profile fail closed before
any spawn; it never retries or reroutes work. Both changes write an audit event first (see the service).
Error bodies never echo request values. These commands carry no Idempotency-Key: repeating one is harmless
(a cooldown only ever extends, clearing is idempotent) and each call is audited.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Request, Security
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from synapse.api.agent_commands import AgentRunError
from synapse.api.workflow_commands import _body

ListProfiles = Callable[[], dict[str, Any]]
SetCooldown = Callable[[str, float], dict[str, Any]]
ClearCooldown = Callable[[str], dict[str, Any]]
_ID = Path(pattern=r"^[A-Za-z0-9._-]{1,64}$")


class CooldownRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seconds: float = Field(gt=0, le=86400, allow_inf_nan=False)


def profile_router(list_profiles: ListProfiles | None, set_cooldown: SetCooldown | None,
                   clear_cooldown: ClearCooldown | None) -> APIRouter:
    router = APIRouter(dependencies=[Security(HTTPBearer())])

    def refuse(exc: AgentRunError):
        raise HTTPException(exc.status, detail=exc.message) from None

    @router.get("/api/v1/profiles")
    async def get_profiles():
        if list_profiles is None:
            raise HTTPException(503, detail="Profiles unavailable")
        return list_profiles()

    @router.post("/api/v1/profiles/{profile_id}/cooldown")
    async def post_cooldown(request: Request, profile_id: str = _ID):
        if set_cooldown is None:
            raise HTTPException(503, detail="Profiles unavailable")
        body = await _body(request, CooldownRequest)
        try:
            return set_cooldown(profile_id, body.seconds)
        except AgentRunError as exc:
            refuse(exc)

    @router.delete("/api/v1/profiles/{profile_id}/cooldown")
    async def delete_cooldown(profile_id: str = _ID):
        if clear_cooldown is None:
            raise HTTPException(503, detail="Profiles unavailable")
        try:
            return clear_cooldown(profile_id)
        except AgentRunError as exc:
            refuse(exc)

    return router
