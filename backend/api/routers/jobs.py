"""Background jobs: start long work, follow its progress, cancel it.

Progress is available three ways: poll GET /api/jobs/{id}, or follow
GET /api/jobs/{id}/events, a server-sent event stream that emits the job each
time it changes and ends when the job finishes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from backend.api.deps import current_context
from backend.api.routers.backtest import BacktestRequest, execute_backtest
from backend.api.routers.data import FetchRequest, execute_fetch
from backend.app.context import AppContext
from backend.app.jobs import JobHandle, JobRecord, get_runner

router = APIRouter()

EVENT_POLL_S = 0.25


def _get(job_id: str, ctx: AppContext) -> JobRecord:
    try:
        return get_runner().get(job_id, ctx)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/api/jobs")
def list_jobs(
    active_only: bool = False,
    limit: int = Query(default=50, ge=1, le=500),
    ctx: AppContext = Depends(current_context),
) -> dict:
    jobs = get_runner().list_jobs(ctx, active_only=active_only, limit=limit)
    # Results can be large (a whole backtest); the list carries status only.
    return {"jobs": [j.to_dict() | {"result": None} for j in jobs]}


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str, ctx: AppContext = Depends(current_context)) -> dict:
    return {"job": _get(job_id, ctx).to_dict()}


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, ctx: AppContext = Depends(current_context)) -> dict:
    _get(job_id, ctx)
    return {"job": get_runner().cancel(job_id, ctx).to_dict()}


@router.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, ctx: AppContext = Depends(current_context)) -> StreamingResponse:
    _get(job_id, ctx)

    async def stream() -> AsyncIterator[str]:
        last = None
        while True:
            record = await asyncio.to_thread(get_runner().get, job_id, ctx)
            snapshot = record.to_dict()
            if record.finished:
                yield f"event: done\ndata: {json.dumps(snapshot, default=str)}\n\n"
                return
            view = {k: snapshot[k] for k in ("status", "progress", "message", "eta_seconds",
                                             "cancel_requested")}
            if view != last:
                last = view
                yield f"event: progress\ndata: {json.dumps(snapshot | {'result': None}, default=str)}\n\n"
            await asyncio.sleep(EVENT_POLL_S)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@router.post("/api/jobs/backtest", status_code=202)
def start_backtest_job(req: BacktestRequest, ctx: AppContext = Depends(current_context)) -> dict:
    def work(job: JobHandle) -> dict:
        job.progress(0.0, "loading data", force=True)
        return execute_backtest(
            req, ctx,
            on_progress=lambda done, total: job.progress(done / max(total, 1),
                                                         f"bar {done:,} of {total:,}"),
        )

    params = req.model_dump(mode="json", exclude={"strategy"}) | {
        "strategy_name": req.strategy.get("name")}
    job_id = get_runner().submit("backtest", work, params=params, ctx=ctx)
    return {"job": get_runner().get(job_id, ctx).to_dict()}


@router.post("/api/jobs/fetch", status_code=202)
def start_fetch_job(req: FetchRequest, ctx: AppContext = Depends(current_context)) -> dict:
    def work(job: JobHandle) -> dict:
        return execute_fetch(
            req,
            on_progress=lambda done, total: job.progress(done / max(total, 1),
                                                         f"request {done + 1} of {total}"),
        )

    job_id = get_runner().submit("fetch", work, params=req.model_dump(mode="json"), ctx=ctx)
    return {"job": get_runner().get(job_id, ctx).to_dict()}
