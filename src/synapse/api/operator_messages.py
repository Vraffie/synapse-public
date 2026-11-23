"""Read-only operator messaging routes; app middleware authenticates."""
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query, Security
from fastapi.security import HTTPBearer

ThreadReader = Callable[[str, int, int], list[dict[str, Any]]]
MessageReader = Callable[[str, str, int, int], list[dict[str, Any]]]


def message_query_router(threads: ThreadReader | None, messages: MessageReader | None) -> APIRouter:
    router = APIRouter(dependencies=[Security(HTTPBearer())])

    @router.get("/api/v1/runs/{run_id}/threads")
    async def get_threads(run_id: str = Path(min_length=1, max_length=256),
                          after: int = Query(default=0, ge=0, le=9223372036854775807),
                          limit: int = Query(default=100, ge=1, le=100)):
        if not run_id.strip():
            raise HTTPException(status_code=422, detail="Invalid run ID")
        if threads is None:
            raise HTTPException(status_code=503, detail="Thread query unavailable")
        try:
            return {"threads": threads(run_id, after, limit)}
        except LookupError:
            raise HTTPException(status_code=404, detail="Run not found") from None

    @router.get("/api/v1/runs/{run_id}/threads/{thread_id}/messages")
    async def get_messages(run_id: str = Path(min_length=1, max_length=256),
                           thread_id: str = Path(min_length=1, max_length=256),
                           after: int = Query(default=0, ge=0, le=9223372036854775807),
                           limit: int = Query(default=100, ge=1, le=100)):
        if not run_id.strip() or not thread_id.strip():
            raise HTTPException(status_code=422, detail="Invalid identifier")
        if messages is None:
            raise HTTPException(status_code=503, detail="Message query unavailable")
        try:
            return {"messages": messages(run_id, thread_id, after, limit)}
        except LookupError:
            raise HTTPException(status_code=404, detail="Thread not found") from None

    return router
