from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from music_insight.adapters.local_omni import LocalOmniServer
from music_insight.adapters.base import LyricsRetryAdapter
from music_insight.async_utils import run_sync_settled
from music_insight.api.contracts.history import (
    HistoryDetail,
    HistoryLyricsRetryRequest,
    HistoryLyricsRetryResult,
    HistoryLyricsUpdate,
)
from music_insight.api.history import (
    HistoryStore,
)
from music_insight.api.orchestrator_factory import build_orchestrator
from music_insight.audio import slice_wav
from music_insight.config import Settings
from music_insight.pipeline.preprocess import Preprocessor
from music_insight.schemas import AudioAsset


def require_history(
    history: HistoryStore,
    history_id: str,
    user_id: str,
) -> HistoryDetail:
    entry = history.get(history_id, user_id=user_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Analysis history not found.")
    return entry


def revise_lyrics(
    *,
    history: HistoryStore,
    history_id: str,
    payload: HistoryLyricsUpdate,
    user_id: str,
) -> HistoryDetail:
    entry = history.get(history_id, user_id=user_id)
    if entry is None or entry.result is None:
        raise HTTPException(status_code=404, detail="Analysis history not found.")
    if any(not segment.text.strip() for segment in payload.lyrics):
        raise HTTPException(status_code=422, detail="Lyrics text cannot be empty.")
    duration = entry.duration_s
    if duration is not None and any(
        segment.span and segment.span.end_s > duration + 0.5
        for segment in payload.lyrics
    ):
        raise HTTPException(
            status_code=422,
            detail="Lyrics timestamp exceeds audio duration.",
        )
    ordered = sorted(
        payload.lyrics,
        key=lambda segment: (
            segment.span.start_s if segment.span else float("inf"),
            segment.span.end_s if segment.span else float("inf"),
        ),
    )
    revised = history.update_lyrics(history_id, ordered, user_id=user_id)
    if revised is None:
        raise HTTPException(status_code=404, detail="Analysis history not found.")
    return revised


async def retry_lyrics(
    *,
    history: HistoryStore,
    history_id: str,
    payload: HistoryLyricsRetryRequest,
    settings: Settings,
    user_id: str,
    local_server: LocalOmniServer | None = None,
    compute_gate: AbstractAsyncContextManager[None] | None = None,
) -> HistoryLyricsRetryResult:
    entry, audio_path = await asyncio.gather(
        run_in_threadpool(history.get, history_id, user_id=user_id),
        run_in_threadpool(history.audio_path, history_id, user_id=user_id),
    )
    if entry is None or entry.result is None or audio_path is None:
        raise HTTPException(status_code=404, detail="Cached analysis audio not found.")
    if payload.end_s <= payload.start_s:
        raise HTTPException(
            status_code=422,
            detail="Retry end time must be after start time.",
        )
    if payload.end_s - payload.start_s > settings.omni_chunk_seconds + 0.5:
        raise HTTPException(
            status_code=422,
            detail=f"Retry range cannot exceed {settings.omni_chunk_seconds:g} seconds.",
        )
    if entry.duration_s is not None and payload.end_s > entry.duration_s + 0.5:
        raise HTTPException(
            status_code=422,
            detail="Retry range exceeds audio duration.",
        )

    asset = AudioAsset(
        path=audio_path,
        media_type="audio/wav",
        size_bytes=audio_path.stat().st_size,
        language_hint=entry.language,
        max_duration_s=settings.max_audio_minutes * 60,
    )
    preprocessor = Preprocessor(settings.workspace_dir)
    if compute_gate is None:
        prepared = await preprocessor.prepare(asset)
    else:
        async with compute_gate:
            prepared = await preprocessor.prepare(asset)
    try:
        audio_bytes, clip_duration = await run_sync_settled(
            slice_wav,
            prepared.scene.path,
            payload.start_s,
            payload.end_s,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Unable to prepare retry audio: {str(exc)[:300]}",
        ) from exc

    orchestrator = build_orchestrator(
        settings,
        model_source=entry.model_source,
        model_endpoint=(
            entry.model_location if entry.model_source == "network" else None
        ),
        local_model_path=(
            entry.model_location if entry.model_source == "local" else None
        ),
        local_server=local_server,
    )
    adapter = orchestrator.unified
    if not isinstance(adapter, LyricsRetryAdapter):
        raise HTTPException(status_code=422, detail="Model does not support retry.")
    try:
        if orchestrator.model_gate is None:
            lyrics, issues = await adapter.retry_lyrics(
                audio_bytes,
                clip_duration,
                entry.language,
            )
        else:
            async with orchestrator.model_gate:
                lyrics, issues = await adapter.retry_lyrics(
                    audio_bytes,
                    clip_duration,
                    entry.language,
                )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Model retry failed: {str(exc)[:500]}",
        ) from exc

    shifted = [
        lyric.model_copy(
            update={
                "span": (
                    lyric.span.model_copy(
                        update={
                            "start_s": lyric.span.start_s + payload.start_s,
                            "end_s": lyric.span.end_s + payload.start_s,
                        }
                    )
                    if lyric.span
                    else None
                )
            }
        )
        for lyric in lyrics
    ]
    return HistoryLyricsRetryResult(
        start_s=payload.start_s,
        end_s=payload.start_s + clip_duration,
        lyrics=shifted,
        issues=issues,
        source=adapter.source,
    )
