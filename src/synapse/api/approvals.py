"""Operator-only, read-only approval query routes."""
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Security
from fastapi.security import HTTPBearer

ApprovalReader = Callable[[str, int], list[dict[str, Any]]]


def approval_router(read_approvals: ApprovalReader | None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/approvals", dependencies=[Security(HTTPBearer())])
    async def approvals(after: str = Query(default="", max_length=256),
                        limit: int = Query(default=100, ge=1, le=1000)):
        if read_approvals is None:
            raise HTTPException(status_code=503, detail="Approval query unavailable")
        return {"approvals": read_approvals(after, limit)}

    return router
