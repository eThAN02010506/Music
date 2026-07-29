from __future__ import annotations

from fastapi import HTTPException, UploadFile

from music_insight.async_utils import run_sync_settled
from music_insight.audio import (
    AudioDurationExceededError,
    probe_audio_duration,
)
from music_insight.config import Settings
from music_insight.schemas import AudioAsset
from music_insight.storage.local import LocalAudioStore, UploadTooLargeError


async def save_audio_upload(
    file: UploadFile,
    language: str | None,
    settings: Settings,
    user_id: str,
    *,
    temporary: bool = False,
) -> AudioAsset:
    if not file.content_type or not file.content_type.startswith("audio/"):
        raise HTTPException(status_code=415, detail="Only audio uploads are supported.")
    if language not in {None, "zh", "en"}:
        raise HTTPException(status_code=422, detail="Unsupported language hint.")
    directory = "temporary" if temporary else "uploads"
    root = (
        settings.shared_audio_dir
        if not temporary
        and settings.job_backend == "redis"
        and settings.shared_audio_dir is not None
        else settings.workspace_dir
    )
    store = LocalAudioStore(root / "users" / user_id / directory)
    try:
        asset = await store.save_upload(
            file,
            max_bytes=settings.max_upload_mb * 1024 * 1024,
        )
    except UploadTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail="Upload exceeds configured size limit.",
        ) from exc
    max_duration_s = settings.max_audio_minutes * 60
    try:
        duration = await run_sync_settled(
            probe_audio_duration,
            asset.path,
            max_duration_s=max_duration_s,
        )
        if duration is not None and duration > max_duration_s:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Audio duration exceeds the configured "
                    f"{settings.max_audio_minutes:g} minute limit."
                ),
            )
    except (HTTPException, AudioDurationExceededError) as exc:
        asset.path.unlink(missing_ok=True)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            status_code=413,
            detail=(
                "Audio duration exceeds the configured "
                f"{settings.max_audio_minutes:g} minute limit."
            ),
        ) from exc
    except BaseException as exc:
        asset.path.unlink(missing_ok=True)
        if not isinstance(exc, Exception):
            raise
        raise HTTPException(
            status_code=415,
            detail="The upload is not a decodable audio file.",
        ) from exc
    return asset.model_copy(
        update={
            "language_hint": language,
            "max_duration_s": max_duration_s,
        }
    )
