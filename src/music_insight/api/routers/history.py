from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from music_insight.api.accounts import UserPublic
from music_insight.api.contracts.history import (
    HistoryDetail,
    HistoryLyricsRetryRequest,
    HistoryLyricsRetryResult,
    HistoryLyricsUpdate,
    HistoryRename,
    HistoryRevision,
    HistorySummary,
    HistoryWaveform,
)
from music_insight.api.dependencies import (
    get_current_user,
    get_history_store,
    get_job_store,
)
from music_insight.api.history import HistoryStore
from music_insight.api.job_access import get_job, remove_job
from music_insight.api.jobs import AnalysisJobStore, JobState
from music_insight.api.services.history import (
    require_history,
    retry_lyrics,
    revise_lyrics,
)
from music_insight.api.services.waveform import load_waveform
from music_insight.config import Settings, get_settings


router = APIRouter(prefix="/history", tags=["history"])


@router.get("", response_model=list[HistorySummary])
async def list_history(
    limit: int = 100,
    history: HistoryStore = Depends(get_history_store),
    user: UserPublic = Depends(get_current_user),
) -> list[HistorySummary]:
    return await run_in_threadpool(
        history.list,
        limit=limit,
        user_id=user.id,
    )


@router.get("/{history_id}", response_model=HistoryDetail)
async def get_history(
    history_id: str,
    history: HistoryStore = Depends(get_history_store),
    user: UserPublic = Depends(get_current_user),
) -> HistoryDetail:
    return await run_in_threadpool(require_history, history, history_id, user.id)


@router.patch("/{history_id}", response_model=HistoryDetail)
async def rename_history(
    history_id: str,
    payload: HistoryRename,
    history: HistoryStore = Depends(get_history_store),
    user: UserPublic = Depends(get_current_user),
) -> HistoryDetail:
    entry = await run_in_threadpool(
        history.rename,
        history_id,
        payload.title,
        user_id=user.id,
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Analysis history not found.")
    return entry


@router.patch("/{history_id}/lyrics", response_model=HistoryDetail)
async def update_history_lyrics(
    history_id: str,
    payload: HistoryLyricsUpdate,
    history: HistoryStore = Depends(get_history_store),
    user: UserPublic = Depends(get_current_user),
) -> HistoryDetail:
    return await run_in_threadpool(
        revise_lyrics,
        history=history,
        history_id=history_id,
        payload=payload,
        user_id=user.id,
    )


@router.get(
    "/{history_id}/revisions",
    response_model=list[HistoryRevision],
)
async def list_history_revisions(
    history_id: str,
    history: HistoryStore = Depends(get_history_store),
    user: UserPublic = Depends(get_current_user),
) -> list[HistoryRevision]:
    await run_in_threadpool(require_history, history, history_id, user.id)
    return await run_in_threadpool(
        history.revisions,
        history_id,
        user_id=user.id,
    )


@router.post(
    "/{history_id}/lyrics/retry",
    response_model=HistoryLyricsRetryResult,
)
async def retry_history_lyrics(
    request: Request,
    history_id: str,
    payload: HistoryLyricsRetryRequest,
    settings: Settings = Depends(get_settings),
    history: HistoryStore = Depends(get_history_store),
    user: UserPublic = Depends(get_current_user),
) -> HistoryLyricsRetryResult:
    async with request.app.state.direct_work_limiter.lease(user.id):
        return await retry_lyrics(
            history=history,
            history_id=history_id,
            payload=payload,
            settings=settings,
            user_id=user.id,
            local_server=request.app.state.local_server,
            compute_gate=request.app.state.local_compute_gate,
        )


@router.delete("/{history_id}", status_code=204)
async def delete_history(
    history_id: str,
    history: HistoryStore = Depends(get_history_store),
    jobs: AnalysisJobStore = Depends(get_job_store),
    user: UserPublic = Depends(get_current_user),
) -> Response:
    snapshot = await get_job(
        jobs,
        history_id,
        owner_user_id=user.id,
    )
    if snapshot and snapshot.state in {JobState.QUEUED, JobState.RUNNING}:
        raise HTTPException(status_code=409, detail="Cancel the running job first.")
    deleted = await run_in_threadpool(
        history.delete,
        history_id,
        user_id=user.id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Analysis history not found.")
    await remove_job(
        jobs,
        history_id,
        owner_user_id=user.id,
    )
    return Response(status_code=204)


@router.get("/{history_id}/audio")
async def get_history_audio(
    history_id: str,
    history: HistoryStore = Depends(get_history_store),
    user: UserPublic = Depends(get_current_user),
) -> FileResponse:
    path = await run_in_threadpool(
        history.audio_path,
        history_id,
        user_id=user.id,
    )
    if path is None:
        raise HTTPException(status_code=404, detail="Cached audio not found.")
    return FileResponse(path, filename=path.name)


@router.get(
    "/{history_id}/waveform",
    response_model=HistoryWaveform,
)
async def get_history_waveform(
    request: Request,
    response: Response,
    history_id: str,
    points: int = Query(default=1_200, ge=100, le=4_000),
    settings: Settings = Depends(get_settings),
    history: HistoryStore = Depends(get_history_store),
    user: UserPublic = Depends(get_current_user),
) -> HistoryWaveform:
    path = await run_in_threadpool(
        history.audio_path,
        history_id,
        user_id=user.id,
    )
    if path is None:
        raise HTTPException(status_code=404, detail="Cached audio not found.")
    try:
        async with request.app.state.direct_work_limiter.lease(user.id):
            async with request.app.state.local_compute_gate:
                waveform = await run_in_threadpool(
                    load_waveform,
                    path,
                    points=points,
                    max_duration_s=settings.max_audio_minutes * 60,
                )
                response.headers["Cache-Control"] = "private, no-store"
                return waveform
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:500]) from exc
