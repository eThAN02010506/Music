from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from music_insight.api.accounts import UserPublic
from music_insight.api.contracts.stems import StemStatusResponse, StemTrack
from music_insight.api.dependencies import get_current_user, get_history_store
from music_insight.api.history import HistoryAudioSource, HistoryStore
from music_insight.stems import (
    STEM_LABELS,
    STEM_NAMES,
    StemCacheResult,
    StemSeparationError,
)


router = APIRouter(prefix="/history", tags=["stems"])


async def _source_or_404(
    history: HistoryStore,
    history_id: str,
    user_id: str,
) -> HistoryAudioSource:
    source = await run_in_threadpool(
        history.audio_source,
        history_id,
        user_id=user_id,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Analysis audio not found.")
    return source


def _response(
    history_id: str,
    result: StemCacheResult,
) -> StemStatusResponse:
    tracks = (
        [
            StemTrack(
                name=name,
                label=STEM_LABELS[name],
                audio_url=f"/history/{history_id}/stems/{name}",
            )
            for name in STEM_NAMES
        ]
        if result.state == "ready"
        else []
    )
    return StemStatusResponse(
        status=result.state,
        backend=result.backend,
        model=result.model,
        stems=tracks,
        detail=result.detail,
    )


@router.get("/{history_id}/stems", response_model=StemStatusResponse)
async def get_stem_status(
    request: Request,
    history_id: str,
    history: HistoryStore = Depends(get_history_store),
    user: UserPublic = Depends(get_current_user),
) -> StemStatusResponse:
    source = await _source_or_404(history, history_id, user.id)
    result = await request.app.state.stem_service.status(
        content_key=source.content_key,
    )
    return _response(history_id, result)


@router.post("/{history_id}/stems", response_model=StemStatusResponse)
async def generate_stems(
    request: Request,
    history_id: str,
    history: HistoryStore = Depends(get_history_store),
    user: UserPublic = Depends(get_current_user),
) -> StemStatusResponse:
    source = await _source_or_404(history, history_id, user.id)
    try:
        async with request.app.state.direct_work_limiter.lease(user.id):
            async with request.app.state.stem_compute_gate:
                result = await request.app.state.stem_service.ensure(
                    source=source.path,
                    content_key=source.content_key,
                )
    except StemSeparationError as exc:
        raise HTTPException(
            status_code=504 if exc.timed_out else 503,
            detail=str(exc)[:2500],
        ) from exc
    return _response(history_id, result)


@router.get("/{history_id}/stems/{stem}")
async def get_stem_audio(
    request: Request,
    history_id: str,
    stem: str,
    history: HistoryStore = Depends(get_history_store),
    user: UserPublic = Depends(get_current_user),
) -> FileResponse:
    if stem not in STEM_NAMES:
        raise HTTPException(status_code=404, detail="Stem not found.")
    source = await _source_or_404(history, history_id, user.id)
    path = request.app.state.stem_service.path_for(
        content_key=source.content_key,
        stem=stem,
    )
    if path is None:
        raise HTTPException(status_code=404, detail="Stem not generated.")
    return FileResponse(
        path,
        media_type="audio/wav",
        headers={"Cache-Control": "private, no-store"},
    )
