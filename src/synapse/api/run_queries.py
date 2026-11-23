"""Operator-only run read endpoints; transport authentication is app-owned."""
from collections.abc import Callable
from typing import Any

from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query, Security
from fastapi.security import HTTPBearer

from synapse.application.analytics_views import DEFAULT_PAGE, MAX_PAGE, AnalyticsTooLarge
from synapse.domain.runs import RunState
from synapse.persistence.paging import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, InvalidPageCursor
from synapse.persistence.run_list import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor
from synapse.persistence.run_snapshot import RunNotFound

RunReader = Callable[[str], dict[str, Any]]
# (run_id, limit, cursor) -> one page plus "next_cursor" (None on the last page)
RunPageReader = Callable[[str, int, str | None], dict[str, Any]]
# (state, limit, cursor) -> {"runs": [...], "next_cursor": str | None}
RunListReader = Callable[[str | None, int, str | None], dict[str, Any]]
# (run_id, limit, cursor) -> RunAnalytics.to_dict(): per-task page plus "next_cursor" (None on the last page)
AnalyticsReader = Callable[[str, int, str | None], dict[str, Any]]
RunStateFilter = Literal[tuple(state.value for state in RunState)]


def run_query_router(snapshot: RunReader | None, tasks: RunPageReader | None,
                     runs: RunListReader | None = None, usage: RunPageReader | None = None,
                     analytics: AnalyticsReader | None = None) -> APIRouter:
    router = APIRouter(dependencies=[Security(HTTPBearer())])

    @router.get('/api/v1/runs')
    async def list_runs(state: RunStateFilter | None = None,
                        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
                        cursor: str | None = Query(default=None, min_length=1, max_length=1024)):
        if runs is None:
            raise HTTPException(status_code=503, detail="Run reader unavailable")
        try:
            return runs(state, limit, cursor)
        except InvalidCursor:
            raise HTTPException(status_code=422, detail="Invalid cursor") from None

    def read(reader: Callable[..., dict[str, Any]] | None, run_id: str, *page: Any) -> dict[str, Any]:
        if not run_id.strip():
            raise HTTPException(status_code=422, detail="Invalid run ID")
        if reader is None:
            raise HTTPException(status_code=503, detail="Run reader unavailable")
        try:
            return reader(run_id, *page)
        except RunNotFound:
            raise HTTPException(status_code=404, detail="Run not found") from None
        except InvalidPageCursor:
            raise HTTPException(status_code=422, detail="Invalid cursor") from None
        except InvalidCursor:
            raise HTTPException(status_code=422, detail="Invalid cursor") from None
        except AnalyticsTooLarge:
            raise HTTPException(status_code=422, detail="Run too large for analytics") from None

    @router.get('/api/v1/runs/{run_id}')
    async def get_run(run_id: str = Path(min_length=1, max_length=256)):
        return read(snapshot, run_id)

    @router.get('/api/v1/runs/{run_id}/tasks')
    async def get_tasks(run_id: str = Path(min_length=1, max_length=256),
                        limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
                        cursor: str | None = Query(default=None, min_length=1, max_length=1024)):
        return read(tasks, run_id, limit, cursor)

    @router.get('/api/v1/runs/{run_id}/usage')
    async def get_usage(run_id: str = Path(min_length=1, max_length=256),
                        limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
                        cursor: str | None = Query(default=None, min_length=1, max_length=1024)):
        return read(usage, run_id, limit, cursor)

    @router.get('/api/v1/runs/{run_id}/analytics')
    async def get_analytics(run_id: str = Path(min_length=1, max_length=256),
                            limit: int = Query(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE),
                            cursor: str | None = Query(default=None, min_length=1, max_length=1024)):
        return read(analytics, run_id, limit, cursor)

    return router
