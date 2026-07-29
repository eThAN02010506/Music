from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from music_insight.schemas import (
    AsrResult,
    AudioAsset,
    AudioSceneResult,
    DspResult,
    Evidence,
    EvidenceType,
    LiteraryResult,
    LyricsSegment,
    TimeSpan,
    UnifiedAudioResult,
)


ProgressCallback = (
    Callable[[str, float, str], Awaitable[None] | None] | None
)


class StructuredOmniWorkflowAdapter(Protocol):
    """Narrow adapter surface needed by the chunk workflow.

    Keeping this protocol in the workflow module preserves the adapter's
    overridable private methods used by test doubles and local model variants.
    """

    source: str

    async def _model(self) -> str: ...

    def _chunk_count(self, path: Path) -> int: ...

    def _wav_chunks(
        self,
        path: Path,
    ) -> Iterator[tuple[bytes, float, float]]: ...

    async def _notify(
        self,
        callback: ProgressCallback,
        stage: str,
        progress: float,
        message: str,
    ) -> None: ...

    async def _analyze_chunk(
        self,
        model: str,
        audio_bytes: bytes,
        start_s: float,
        end_s: float,
        language_hint: str | None,
    ) -> dict[str, Any]: ...

    def _parse_chunk(
        self,
        payload: dict[str, Any],
        index: int,
        chunk_start: float,
        chunk_end: float,
    ) -> dict[str, Any]: ...

    def _filter_lyrics_quality(
        self,
        values: list[LyricsSegment],
    ) -> tuple[list[LyricsSegment], list[str]]: ...

    async def _recover_lyrics_quality(
        self,
        model: str,
        audio_bytes: bytes,
        duration_s: float,
        language_hint: str | None,
        issues: list[str],
    ) -> dict[str, Any]: ...

    async def _recover_missing(
        self,
        model: str,
        audio_bytes: bytes,
        duration_s: float,
        language_hint: str | None,
        missing: list[str],
    ) -> dict[str, Any]: ...

    def _deduplicate_lyrics(
        self,
        values: list[LyricsSegment],
        limit: int,
    ) -> list[LyricsSegment]: ...

    def _deduplicate(self, values: list[str], limit: int) -> list[str]: ...

    def _deduplicate_evidence(
        self,
        values: list[Evidence],
        limit: int,
    ) -> list[Evidence]: ...

    async def _synthesize_report(
        self,
        model: str,
        lyrics: list[LyricsSegment],
        instruments: list[str],
        sound_events: list[Evidence],
        emotions: list[Evidence],
        chunk_themes: list[str],
        chunk_narratives: list[str],
        dsp: DspResult,
    ) -> tuple[str, list[str], list[Evidence], list[Evidence]]: ...


@dataclass(slots=True)
class ChunkAnalysisState:
    lyrics: list[LyricsSegment] = field(default_factory=list)
    instruments: list[str] = field(default_factory=list)
    sound_events: list[Evidence] = field(default_factory=list)
    emotions: list[Evidence] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    narratives: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)


class StructuredOmniAnalysisWorkflow:
    """Coordinate chunk analysis, recovery, aggregation, and final synthesis."""

    def __init__(self, adapter: StructuredOmniWorkflowAdapter) -> None:
        self.adapter = adapter

    async def analyze(
        self,
        asset: AudioAsset,
        dsp: DspResult,
        progress: ProgressCallback = None,
    ) -> UnifiedAudioResult:
        model = await self.adapter._model()
        total_chunks = self.adapter._chunk_count(asset.path)
        state = ChunkAnalysisState()

        for index, (audio_bytes, start_s, end_s) in enumerate(
            self.adapter._wav_chunks(asset.path),
            start=1,
        ):
            await self._process_chunk(
                state=state,
                model=model,
                index=index,
                total_chunks=total_chunks,
                audio_bytes=audio_bytes,
                start_s=start_s,
                end_s=end_s,
                language_hint=asset.language_hint,
                progress=progress,
            )

        self._deduplicate_state(state)
        narrative, themes, inferred_atmosphere = await self._synthesize(
            state=state,
            model=model,
            dsp=dsp,
            progress=progress,
        )
        return self._result(
            state=state,
            model=model,
            narrative=narrative,
            themes=themes,
            inferred_atmosphere=inferred_atmosphere,
        )

    async def _process_chunk(
        self,
        *,
        state: ChunkAnalysisState,
        model: str,
        index: int,
        total_chunks: int,
        audio_bytes: bytes,
        start_s: float,
        end_s: float,
        language_hint: str | None,
        progress: ProgressCallback,
    ) -> None:
        started_at = perf_counter()
        await self.adapter._notify(
            progress,
            "audio_analysis",
            (index - 1) / max(1, total_chunks) * 0.88,
            f"模型正在分析第 {index}/{total_chunks} 个音频分块",
        )
        try:
            payload = await self.adapter._analyze_chunk(
                model=model,
                audio_bytes=audio_bytes,
                start_s=start_s,
                end_s=end_s,
                language_hint=language_hint,
            )
            parsed = self.adapter._parse_chunk(
                payload,
                index,
                start_s,
                end_s,
            )
        except Exception as exc:
            self._record_chunk_error(
                state,
                index=index,
                start_s=start_s,
                end_s=end_s,
                error=exc,
            )
            await self._notify_chunk_complete(
                index=index,
                total_chunks=total_chunks,
                started_at=started_at,
                progress=progress,
            )
            return

        lyrics_recovery_attempted = await self._recover_bad_lyrics(
            state=state,
            parsed=parsed,
            model=model,
            index=index,
            audio_bytes=audio_bytes,
            start_s=start_s,
            end_s=end_s,
            language_hint=language_hint,
        )
        if not parsed["lyrics"] and not lyrics_recovery_attempted:
            await self._recover_missing_lyrics(
                state=state,
                parsed=parsed,
                model=model,
                index=index,
                audio_bytes=audio_bytes,
                start_s=start_s,
                end_s=end_s,
                language_hint=language_hint,
            )
        parsed["lyrics"] = self._owned_overlap_lyrics(
            parsed["lyrics"],
            index=index,
            total_chunks=total_chunks,
            chunk_start=start_s,
            chunk_end=end_s,
        )
        parsed["sound_events"] = self._owned_overlap_evidence(
            parsed["sound_events"],
            index=index,
            total_chunks=total_chunks,
            chunk_start=start_s,
            chunk_end=end_s,
        )
        parsed["emotions"] = self._owned_overlap_evidence(
            parsed["emotions"],
            index=index,
            total_chunks=total_chunks,
            chunk_start=start_s,
            chunk_end=end_s,
        )

        state.lyrics.extend(parsed["lyrics"])
        state.instruments.extend(parsed["instruments"])
        state.sound_events.extend(parsed["sound_events"])
        state.emotions.extend(parsed["emotions"])
        state.themes.extend(parsed["themes"])
        if parsed["narrative"]:
            state.narratives.append(parsed["narrative"])
        state.evidence.append(
            Evidence(
                id=f"omni.chunk.{index}.analysis",
                source=self.adapter.source,
                kind=EvidenceType.INFERRED,
                text=(
                    parsed["narrative"]
                    or f"已完成第 {index} 个音频分块分析。"
                ),
                confidence=0.7,
                span=TimeSpan(start_s=start_s, end_s=end_s),
                metadata={"model": model, "basis": "direct audio"},
            )
        )
        await self._notify_chunk_complete(
            index=index,
            total_chunks=total_chunks,
            started_at=started_at,
            progress=progress,
        )

    async def _recover_bad_lyrics(
        self,
        *,
        state: ChunkAnalysisState,
        parsed: dict[str, Any],
        model: str,
        index: int,
        audio_bytes: bytes,
        start_s: float,
        end_s: float,
        language_hint: str | None,
    ) -> bool:
        original_lyrics = list(parsed["lyrics"])
        cleaned_lyrics, quality_issues = self.adapter._filter_lyrics_quality(
            parsed["lyrics"]
        )
        if not quality_issues:
            parsed["lyrics"] = cleaned_lyrics
            return False

        try:
            recovered_payload = await self.adapter._recover_lyrics_quality(
                model=model,
                audio_bytes=audio_bytes,
                duration_s=end_s - start_s,
                language_hint=language_hint,
                issues=quality_issues,
            )
            recovered = self.adapter._parse_chunk(
                recovered_payload,
                index,
                start_s,
                end_s,
            )
            recovered_lyrics, recovered_issues = (
                self.adapter._filter_lyrics_quality(recovered["lyrics"])
            )
            if recovered_lyrics and not recovered_issues:
                parsed["lyrics"] = recovered_lyrics
                outcome = "定向重听已替换异常歌词时间轴。"
            else:
                parsed["lyrics"] = cleaned_lyrics
                outcome = "定向重听仍有异常，已仅保留原结果中的可靠歌词行。"
            state.evidence.append(
                Evidence(
                    id=f"omni.chunk.{index}.quality_retry",
                    source=self.adapter.source,
                    kind=EvidenceType.OBSERVED,
                    text=outcome,
                    confidence=0.6 if parsed["lyrics"] else 0.0,
                    span=TimeSpan(start_s=start_s, end_s=end_s),
                    metadata={
                        "issues": quality_issues,
                        "recovered_issues": recovered_issues,
                        "kept_lyrics": len(parsed["lyrics"]),
                        "original_lyrics": [
                            item.model_dump(mode="json")
                            for item in original_lyrics
                        ],
                        "recovered_lyrics": [
                            item.model_dump(mode="json")
                            for item in recovered["lyrics"]
                        ],
                        "model": model,
                    },
                )
            )
        except Exception as exc:
            parsed["lyrics"] = cleaned_lyrics
            state.evidence.append(
                Evidence(
                    id=f"omni.chunk.{index}.quality_retry.unavailable",
                    source=self.adapter.source,
                    kind=EvidenceType.OBSERVED,
                    text=(
                        "歌词时间轴异常，定向重听失败；"
                        "已仅保留原结果中的可靠歌词行。"
                    ),
                    confidence=0.0,
                    span=TimeSpan(start_s=start_s, end_s=end_s),
                    metadata={
                        "issues": quality_issues,
                        "original_lyrics": [
                            item.model_dump(mode="json")
                            for item in original_lyrics
                        ],
                        "error_type": exc.__class__.__name__,
                        "error": str(exc)[:500],
                    },
                )
            )
        return True

    async def _recover_missing_lyrics(
        self,
        *,
        state: ChunkAnalysisState,
        parsed: dict[str, Any],
        model: str,
        index: int,
        audio_bytes: bytes,
        start_s: float,
        end_s: float,
        language_hint: str | None,
    ) -> None:
        missing = ["lyrics"]
        try:
            recovered_payload = await self.adapter._recover_missing(
                model=model,
                audio_bytes=audio_bytes,
                duration_s=end_s - start_s,
                language_hint=language_hint,
                missing=missing,
            )
            recovered = self.adapter._parse_chunk(
                recovered_payload,
                index,
                start_s,
                end_s,
            )
            recovered_fields: list[str] = []
            if not parsed["lyrics"]:
                cleaned, quality_issues = (
                    self.adapter._filter_lyrics_quality(recovered["lyrics"])
                )
                parsed["lyrics"] = cleaned
                if cleaned:
                    recovered_fields.append("lyrics")
                if quality_issues:
                    state.evidence.append(
                        Evidence(
                            id=f"omni.chunk.{index}.recovery.quality",
                            source=self.adapter.source,
                            kind=EvidenceType.OBSERVED,
                            text=(
                                "定向重听返回了异常歌词；"
                                "已丢弃过密、重叠或重复的行。"
                            ),
                            confidence=0.0,
                            span=TimeSpan(start_s=start_s, end_s=end_s),
                            metadata={"issues": quality_issues},
                        )
                    )
            if recovered_fields:
                state.evidence.append(
                    Evidence(
                        id=f"omni.chunk.{index}.recovery",
                        source=self.adapter.source,
                        kind=EvidenceType.INFERRED,
                        text="定向重听补充了：" + "、".join(recovered_fields),
                        confidence=0.6,
                        span=TimeSpan(start_s=start_s, end_s=end_s),
                        metadata={
                            "requested_fields": missing,
                            "recovered_fields": recovered_fields,
                            "model": model,
                        },
                    )
                )
            else:
                state.evidence.append(
                    Evidence(
                        id=f"omni.chunk.{index}.recovery.unavailable",
                        source=self.adapter.source,
                        kind=EvidenceType.OBSERVED,
                        text="定向重听仍未确认可靠歌词或人声情绪。",
                        confidence=0.0,
                        span=TimeSpan(start_s=start_s, end_s=end_s),
                        metadata={"requested_fields": missing},
                    )
                )
        except Exception as exc:
            state.evidence.append(
                Evidence(
                    id=f"omni.chunk.{index}.recovery.unavailable",
                    source=self.adapter.source,
                    kind=EvidenceType.OBSERVED,
                    text=f"定向重听未返回可解析结果：{str(exc)[:500]}",
                    confidence=0.0,
                    span=TimeSpan(start_s=start_s, end_s=end_s),
                    metadata={"requested_fields": missing},
                )
            )

    def _record_chunk_error(
        self,
        state: ChunkAnalysisState,
        *,
        index: int,
        start_s: float,
        end_s: float,
        error: Exception,
    ) -> None:
        state.evidence.append(
            Evidence(
                id=f"omni.chunk.{index}.error",
                source=self.adapter.source,
                kind=EvidenceType.OBSERVED,
                text=(
                    f"第 {index} 个音频分块分析失败："
                    f"{(str(error).strip() or error.__class__.__name__)[:500]}"
                ),
                confidence=0.0,
                span=TimeSpan(start_s=start_s, end_s=end_s),
                metadata={"error_type": error.__class__.__name__},
            )
        )

    async def _notify_chunk_complete(
        self,
        *,
        index: int,
        total_chunks: int,
        started_at: float,
        progress: ProgressCallback,
    ) -> None:
        await self.adapter._notify(
            progress,
            "audio_analysis",
            index / max(1, total_chunks) * 0.88,
            (
                f"第 {index}/{total_chunks} 个分块完成，"
                f"耗时 {perf_counter() - started_at:.1f} 秒"
            ),
        )

    def _deduplicate_state(self, state: ChunkAnalysisState) -> None:
        limits = {
            "lyrics": (len(state.lyrics), 240),
            "sound_events": (len(state.sound_events), 96),
            "emotions": (len(state.emotions), 96),
        }
        truncated = {
            name: {"observed": count, "kept": limit}
            for name, (count, limit) in limits.items()
            if count > limit
        }
        if truncated:
            state.evidence.append(
                Evidence(
                    id="omni.aggregate.truncated",
                    source=self.adapter.source,
                    kind=EvidenceType.OBSERVED,
                    text="长音频证据超过安全上限，报告已明确截断部分明细。",
                    confidence=0.0,
                    metadata=truncated,
                )
            )
        state.lyrics = self.adapter._deduplicate_lyrics(state.lyrics, 240)
        state.instruments = self.adapter._deduplicate(state.instruments, 16)
        state.sound_events = self.adapter._deduplicate_evidence(
            state.sound_events,
            96,
        )
        state.emotions = self.adapter._deduplicate_evidence(state.emotions, 96)
        state.themes = self.adapter._deduplicate(state.themes, 10)

    def _owned_overlap_lyrics(
        self,
        lyrics: list[LyricsSegment],
        *,
        index: int,
        total_chunks: int,
        chunk_start: float,
        chunk_end: float,
    ) -> list[LyricsSegment]:
        overlap = float(
            getattr(self.adapter, "chunk_overlap_seconds", 0.0)
        )
        if overlap <= 0 or total_chunks <= 1:
            return lyrics
        owned_start = (
            chunk_start if index == 1 else chunk_start + overlap / 2
        )
        owned_end = (
            chunk_end
            if index == total_chunks
            else chunk_end - overlap / 2
        )
        return [
            lyric
            for lyric in lyrics
            if lyric.span is None
            or owned_start
            <= (lyric.span.start_s + lyric.span.end_s) / 2
            <= owned_end
        ]

    def _owned_overlap_evidence(
        self,
        values: list[Evidence],
        *,
        index: int,
        total_chunks: int,
        chunk_start: float,
        chunk_end: float,
    ) -> list[Evidence]:
        overlap = float(
            getattr(self.adapter, "chunk_overlap_seconds", 0.0)
        )
        if overlap <= 0 or total_chunks <= 1:
            return values
        owned_start = (
            chunk_start if index == 1 else chunk_start + overlap / 2
        )
        owned_end = (
            chunk_end
            if index == total_chunks
            else chunk_end - overlap / 2
        )
        return [
            item
            for item in values
            if item.span is None
            or owned_start
            <= (item.span.start_s + item.span.end_s) / 2
            <= owned_end
        ]

    async def _synthesize(
        self,
        *,
        state: ChunkAnalysisState,
        model: str,
        dsp: DspResult,
        progress: ProgressCallback,
    ) -> tuple[str, list[str], list[Evidence]]:
        synthesis_started_at = perf_counter()
        await self.adapter._notify(
            progress,
            "model_synthesis",
            0.9,
            "模型正在综合全部分块证据",
        )
        narrative, themes, inferred_atmosphere, synthesis_evidence = (
            await self.adapter._synthesize_report(
                model=model,
                lyrics=state.lyrics,
                instruments=state.instruments,
                sound_events=state.sound_events,
                emotions=state.emotions,
                chunk_themes=state.themes,
                chunk_narratives=state.narratives,
                dsp=dsp,
            )
        )
        state.evidence.extend(synthesis_evidence)
        await self.adapter._notify(
            progress,
            "model_synthesis",
            1.0,
            f"模型综合完成，耗时 {perf_counter() - synthesis_started_at:.1f} 秒",
        )
        return narrative, themes, inferred_atmosphere

    def _result(
        self,
        *,
        state: ChunkAnalysisState,
        model: str,
        narrative: str,
        themes: list[str],
        inferred_atmosphere: list[Evidence],
    ) -> UnifiedAudioResult:
        return UnifiedAudioResult(
            asr=AsrResult(
                model=self.adapter.source,
                lyrics=state.lyrics,
                evidence=[
                    Evidence(
                        id="omni.transcript",
                        source=self.adapter.source,
                        kind=EvidenceType.INFERRED,
                        text=(
                            "；".join(item.text for item in state.lyrics)
                            if state.lyrics
                            else "统一模型未确认可靠歌词。"
                        ),
                        metadata={"model": model},
                    )
                ],
            ),
            scene=AudioSceneResult(
                model=self.adapter.source,
                instruments=state.instruments,
                sound_events=state.sound_events,
                emotion_timeline=state.emotions,
                inferred_atmosphere=inferred_atmosphere,
                themes=state.themes,
                narrative=" ".join(state.narratives) or None,
                evidence=state.evidence,
            ),
            literary=LiteraryResult(
                model=self.adapter.source,
                themes=themes,
                narrative=narrative,
                evidence=[],
            ),
        )
