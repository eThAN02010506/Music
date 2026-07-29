from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from music_insight.api.accounts import UserPublic
from music_insight.api.debug import debug_state, diagnostic_report, task_detail
from music_insight.api.dependencies import (
    get_current_user,
    get_history_store,
    get_job_store,
)
from music_insight.api.history import HistoryStore
from music_insight.api.job_access import (
    get_job,
    get_result,
    job_events,
    list_jobs,
)
from music_insight.api.jobs import AnalysisJobStore
from music_insight.config import Settings, get_settings


router = APIRouter(prefix="/debug", tags=["debug"])


def _attach_maintenance_state(
    state: dict[str, object],
    request: Request,
) -> dict[str, object]:
    task = getattr(request.app.state, "asset_gc_task", None)
    report = getattr(request.app.state, "asset_gc_report", None)
    error = getattr(request.app.state, "asset_gc_error", None)
    service = state.get("service")
    if not isinstance(service, dict):
        return state
    service["asset_gc_status"] = (
        "failed"
        if error
        else "completed"
        if report is not None
        else "running"
        if task is not None and not task.done()
        else "pending"
    )
    if isinstance(report, dict):
        service["asset_gc_removed_files"] = report.get("removed_count", 0)
        reclaimed = report.get("reclaimed_bytes", 0)
        service["asset_gc_reclaimed_mb"] = (
            round(float(reclaimed) / 1024 / 1024, 2)
            if isinstance(reclaimed, (int, float))
            else 0
        )
        service["asset_gc_grace_hours"] = report.get("grace_hours")
    if error:
        service["asset_gc_error"] = str(error)
    return state


@router.get("/state")
async def get_debug_state(
    request: Request,
    settings: Settings = Depends(get_settings),
    history: HistoryStore = Depends(get_history_store),
    jobs: AnalysisJobStore = Depends(get_job_store),
    user: UserPublic = Depends(get_current_user),
) -> dict[str, object]:
    recent_jobs = await list_jobs(
        jobs,
        owner_user_id=user.id,
        limit=20,
    )
    state = await run_in_threadpool(
        debug_state,
        history,
        settings,
        recent_jobs,
        user_id=user.id,
    )
    return _attach_maintenance_state(state, request)


@router.get("/report", response_class=PlainTextResponse)
async def get_debug_report(
    request: Request,
    settings: Settings = Depends(get_settings),
    history: HistoryStore = Depends(get_history_store),
    jobs: AnalysisJobStore = Depends(get_job_store),
    user: UserPublic = Depends(get_current_user),
) -> PlainTextResponse:
    recent_jobs = await list_jobs(
        jobs,
        owner_user_id=user.id,
        limit=20,
    )
    state = await run_in_threadpool(
        debug_state,
        history,
        settings,
        recent_jobs,
        user_id=user.id,
    )
    _attach_maintenance_state(state, request)
    report = await run_in_threadpool(diagnostic_report, state)
    return PlainTextResponse(
        report,
        headers={
            "Content-Disposition": "attachment; filename=music-insight-debug.json"
        },
    )


@router.get("/tasks/{task_id}")
async def get_debug_task(
    task_id: str,
    history: HistoryStore = Depends(get_history_store),
    jobs: AnalysisJobStore = Depends(get_job_store),
    user: UserPublic = Depends(get_current_user),
) -> dict[str, object]:
    snapshot = await get_job(
        jobs,
        task_id,
        owner_user_id=user.id,
    )
    events = await job_events(
        jobs,
        task_id,
        owner_user_id=user.id,
    )
    result = await get_result(
        jobs,
        task_id,
        owner_user_id=user.id,
    )
    detail = await run_in_threadpool(
        task_detail,
        task_id,
        history,
        snapshot,
        events,
        result,
        user_id=user.id,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Debug task not found.")
    return detail
