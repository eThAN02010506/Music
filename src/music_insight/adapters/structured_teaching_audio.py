from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from music_insight.async_utils import run_sync_settled
from music_insight.audio import slice_wav
from music_insight.pipeline.preprocess import Preprocessor
from music_insight.schemas import AudioAsset
from music_insight.teaching.models import (
    RelistenRequest,
    TeachingTimeSpan,
)


async def prepare_relisten_excerpts(
    request: RelistenRequest,
) -> list[tuple[bytes, TeachingTimeSpan]]:
    """Normalize once in a private temporary cache, then slice at most two clips."""

    path = Path(request.audio_path)
    if not path.is_file():
        raise ValueError("局部重听音频不存在。")
    if not 1 <= len(request.ranges) <= 2:
        raise ValueError("局部重听仅支持一至两个片段。")
    if any(span.end_s - span.start_s > 30.0 for span in request.ranges):
        raise ValueError("每个局部重听片段不得超过 30 秒。")

    with TemporaryDirectory(prefix="music-insight-relisten-") as temporary:
        asset = AudioAsset(
            path=path,
            media_type="application/octet-stream",
            size_bytes=path.stat().st_size,
            language_hint=request.language,
        )
        prepared = await Preprocessor(Path(temporary)).prepare(asset)
        if prepared.scene is None:
            detail = (
                prepared.evidence[-1].text
                if prepared.evidence
                else "音频标准化失败。"
            )
            raise ValueError(detail)
        excerpts: list[tuple[bytes, TeachingTimeSpan]] = []
        for span in request.ranges:
            audio_bytes, duration_s = await run_sync_settled(
                slice_wav,
                prepared.scene.path,
                span.start_s,
                span.end_s,
            )
            excerpts.append(
                (
                    audio_bytes,
                    TeachingTimeSpan(
                        start_s=span.start_s,
                        end_s=span.start_s + duration_s,
                    ),
                )
            )
        return excerpts
