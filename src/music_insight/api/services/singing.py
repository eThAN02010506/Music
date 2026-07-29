from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from fastapi import HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from music_insight.api.accounts import AccountStore, SingingSource
from music_insight.api.history import HistoryStore
from music_insight.config import Settings
from music_insight.singing_score import SingingScore, score_singing

from .uploads import save_audio_upload


async def _score_and_record(
    *,
    reference_path: Path,
    performance_path: Path,
    accounts: AccountStore,
    user_id: str,
    source: SingingSource,
    history_id: str | None,
    reference_name: str,
    performance_name: str,
    failure_label: str,
    max_duration_s: float,
    compute_gate: AbstractAsyncContextManager[None] | None,
) -> SingingScore:
    try:
        if compute_gate is None:
            score = await run_in_threadpool(
                score_singing,
                reference_path,
                performance_path,
                max_duration_s,
            )
        else:
            async with compute_gate:
                score = await run_in_threadpool(
                    score_singing,
                    reference_path,
                    performance_path,
                    max_duration_s,
                )
        await run_in_threadpool(
            accounts.record_score,
            user_id,
            score,
            source=source,
            category="entertainment",
            history_id=history_id,
            reference_name=reference_name,
            performance_name=performance_name,
        )
        return score
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"{failure_label}: {str(exc)[:500]}",
        ) from exc


async def score_against_history(
    *,
    history_id: str,
    file: UploadFile,
    settings: Settings,
    history: HistoryStore,
    accounts: AccountStore,
    user_id: str,
    compute_gate: AbstractAsyncContextManager[None] | None = None,
) -> SingingScore:
    entry, reference_path = await asyncio.gather(
        run_in_threadpool(history.get, history_id, user_id=user_id),
        run_in_threadpool(history.audio_path, history_id, user_id=user_id),
    )
    if entry is None or reference_path is None:
        raise HTTPException(status_code=404, detail="Cached reference audio not found.")
    performance_name = file.filename or "performance.wav"
    attempt = await save_audio_upload(
        file,
        None,
        settings,
        user_id,
        temporary=True,
    )
    try:
        return await _score_and_record(
            reference_path=reference_path,
            performance_path=attempt.path,
            accounts=accounts,
            user_id=user_id,
            source="history",
            history_id=history_id,
            reference_name=entry.file_name,
            performance_name=performance_name,
            failure_label="Singing score failed",
            max_duration_s=settings.max_audio_minutes * 60,
            compute_gate=compute_gate,
        )
    finally:
        attempt.path.unlink(missing_ok=True)


async def compare_uploads(
    *,
    reference: UploadFile,
    performance: UploadFile,
    settings: Settings,
    accounts: AccountStore,
    user_id: str,
    compute_gate: AbstractAsyncContextManager[None] | None = None,
) -> SingingScore:
    reference_name = reference.filename or "reference.wav"
    performance_name = performance.filename or "performance.wav"
    reference_asset = await save_audio_upload(
        reference,
        None,
        settings,
        user_id,
        temporary=True,
    )
    performance_asset = None
    try:
        performance_asset = await save_audio_upload(
            performance,
            None,
            settings,
            user_id,
            temporary=True,
        )
        return await _score_and_record(
            reference_path=reference_asset.path,
            performance_path=performance_asset.path,
            accounts=accounts,
            user_id=user_id,
            source="standalone",
            history_id=None,
            reference_name=reference_name,
            performance_name=performance_name,
            failure_label="Singing comparison failed",
            max_duration_s=settings.max_audio_minutes * 60,
            compute_gate=compute_gate,
        )
    finally:
        reference_asset.path.unlink(missing_ok=True)
        if performance_asset is not None:
            performance_asset.path.unlink(missing_ok=True)
