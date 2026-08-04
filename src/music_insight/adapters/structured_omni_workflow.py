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
        timeout: float | None = None,
    ) -> dict[str, Any]: ...

    def should_abort_chunking(self, error: Exception) -> bool: ...

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
        timeout: float | None = None,
    ) -> dict[str, Any]: ...

    async def _recover_missing(
        self,
        model: str,
        audio_bytes: bytes,
        duration_s: float,
        language_hint: str | None,
        missing: list[str],
        timeout: float | None = None,
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
    vocal_observations: list[tuple[bool, float, int]] = field(
        default_factory=list
    )
    expected_chunks: int = 0
    batch_aborted: bool = False
    consecutive_error_family: str | None = None
    consecutive_error_count: int = 0


class StructuredOmniAnalysisWorkflow:
    """Coordinate chunk analysis, recovery, aggregation, and final synthesis."""

    def __init__(self, adapter: StructuredOmniWorkflowAdapter) -> None:
        self.adapter = adapter

    async def analyze(
        self,
        asset: AudioAsset,
        dsp: DspResult,
        progress: ProgressCallback = None,
        *,
        deadline_at: float | None = None,
    ) -> UnifiedAudioResult:
        model = await self.adapter._model()
        total_chunks = self.adapter._chunk_count(asset.path)
        state = ChunkAnalysisState(expected_chunks=total_chunks)

        for index, (audio_bytes, start_s, end_s) in enumerate(
            self.adapter._wav_chunks(asset.path),
            start=1,
        ):
            should_continue = await self._process_chunk(
                state=state,
                model=model,
                index=index,
                total_chunks=total_chunks,
                audio_bytes=audio_bytes,
                start_s=start_s,
                end_s=end_s,
                language_hint=asset.language_hint,
                progress=progress,
                deadline_at=deadline_at,
            )
            if not should_continue:
                break

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
        deadline_at: float | None = None,
    ) -> bool:
        started_at = perf_counter()
        await self.adapter._notify(
            progress,
            "audio_analysis",
            (index - 1) / max(1, total_chunks) * 0.88,
            f"模型正在分析第 {index}/{total_chunks} 个音频分块",
        )
        remaining = (
            max(0.0, deadline_at - perf_counter())
            if deadline_at is not None
            else None
        )
        if remaining is not None and remaining <= 0:
            self._record_batch_abort(
                state,
                index=index,
                total_chunks=total_chunks,
                start_s=start_s,
                end_s=end_s,
                error=TimeoutError("整体分析时限已到"),
            )
            await self._notify_chunk_complete(
                index=index,
                total_chunks=total_chunks,
                started_at=started_at,
                progress=progress,
            )
            return False
        try:
            payload = await self.adapter._analyze_chunk(
                model=model,
                audio_bytes=audio_bytes,
                start_s=start_s,
                end_s=end_s,
                language_hint=language_hint,
                timeout=remaining,
            )
            parsed = self.adapter._parse_chunk(
                payload,
                index,
                start_s,
                end_s,
            )
        except Exception as exc:
            if self.adapter.should_abort_chunking(exc):
                self._record_batch_abort(
                    state,
                    index=index,
                    total_chunks=total_chunks,
                    start_s=start_s,
                    end_s=end_s,
                    error=exc,
                )
                should_continue = False
            elif self._should_abort_consecutive_errors(state, exc):
                self._record_batch_abort(
                    state,
                    index=index,
                    total_chunks=total_chunks,
                    start_s=start_s,
                    end_s=end_s,
                    error=exc,
                )
                should_continue = False
            else:
                self._record_chunk_error(
                    state,
                    index=index,
                    start_s=start_s,
                    end_s=end_s,
                    error=exc,
                )
                should_continue = True
            await self._notify_chunk_complete(
                index=index,
                total_chunks=total_chunks,
                started_at=started_at,
                progress=progress,
            )
            return should_continue

        try:
            lyrics_recovery_attempted = await self._recover_bad_lyrics(
                state=state,
                parsed=parsed,
                model=model,
                index=index,
                audio_bytes=audio_bytes,
                start_s=start_s,
                end_s=end_s,
                language_hint=language_hint,
                timeout=remaining,
            )
            if (
                not parsed["lyrics"]
                and not lyrics_recovery_attempted
                and not _confident_no_vocals(parsed)
            ):
                await self._recover_missing_lyrics(
                    state=state,
                    parsed=parsed,
                    model=model,
                    index=index,
                    audio_bytes=audio_bytes,
                    start_s=start_s,
                    end_s=end_s,
                    language_hint=language_hint,
                    timeout=remaining,
                )
        except Exception as exc:
            if not self.adapter.should_abort_chunking(exc):
                raise
            self._record_batch_abort(
                state,
                index=index,
                total_chunks=total_chunks,
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
            return False
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
        state.consecutive_error_family = None
        state.consecutive_error_count = 0

        state.lyrics.extend(parsed["lyrics"])
        state.instruments.extend(parsed["instruments"])
        state.sound_events.extend(parsed["sound_events"])
        state.emotions.extend(parsed["emotions"])
        state.themes.extend(parsed["themes"])
        vocals_detected = parsed["vocals_detected"]
        vocal_confidence = parsed["vocal_confidence"]
        if vocals_detected is not None and vocal_confidence is not None:
            state.vocal_observations.append(
                (vocals_detected, vocal_confidence, index)
            )
            state.evidence.append(
                Evidence(
                    id=f"omni.chunk.{index}.vocal_presence",
                    source=self.adapter.source,
                    kind=EvidenceType.INFERRED,
                    text=(
                        "该音频分块检测到人声。"
                        if vocals_detected
                        else "该音频分块未检测到人声。"
                    ),
                    confidence=vocal_confidence,
                    span=TimeSpan(start_s=start_s, end_s=end_s),
                    metadata={
                        "model": model,
                        "vocals_detected": vocals_detected,
                        "basis": "direct audio",
                    },
                )
            )
        if parsed["narrative"]:
            state.narratives.append(parsed["narrative"])
        narrative = parsed["narrative"]
        if narrative:
            support_confidences = [
                item.confidence
                for item in parsed["sound_events"]
                if item.confidence is not None
            ]
            narrative_confidence = (
                min(
                    0.65,
                    sum(support_confidences) / len(support_confidences),
                )
                if support_confidences
                else 0.55
            )
            state.evidence.append(
                Evidence(
                    id=f"omni.chunk.{index}.analysis",
                    source=self.adapter.source,
                    kind=EvidenceType.INFERRED,
                    text=narrative,
                    confidence=narrative_confidence,
                    span=TimeSpan(start_s=start_s, end_s=end_s),
                    metadata={
                        "model": model,
                        "basis": "direct audio",
                        "teaching_dimension": (
                            "instrumentation"
                            if parsed["instruments"]
                            else "other"
                        ),
                    },
                )
            )
        elif parsed["instruments"]:
            state.evidence.append(
                Evidence(
                    id=f"omni.chunk.{index}.instrumentation",
                    source=self.adapter.source,
                    kind=EvidenceType.INFERRED,
                    text=(
                        "该分块识别到的乐器或声部："
                        + "、".join(parsed["instruments"][:8])
                        + "。"
                    ),
                    confidence=0.55,
                    span=TimeSpan(start_s=start_s, end_s=end_s),
                    metadata={
                        "model": model,
                        "basis": "direct audio",
                        "teaching_dimension": "instrumentation",
                    },
                )
            )
        elif not parsed["sound_events"] and not parsed["emotions"]:
            state.evidence.append(
                Evidence(
                    id=f"omni.chunk.{index}.analysis.inconclusive",
                    source=self.adapter.source,
                    kind=EvidenceType.OBSERVED,
                    text="该分块未返回可用于导赏的语义声音证据。",
                    confidence=0.0,
                    span=TimeSpan(start_s=start_s, end_s=end_s),
                    metadata={"model": model, "diagnostic": True},
                )
            )
        await self._notify_chunk_complete(
            index=index,
            total_chunks=total_chunks,
            started_at=started_at,
            progress=progress,
        )
        return True

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
        timeout: float | None = None,
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
                timeout=timeout,
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
            _merge_recovered_vocal_presence(parsed, recovered)
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
            if self.adapter.should_abort_chunking(exc):
                raise
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
        timeout: float | None = None,
    ) -> None:
        missing = ["lyrics"]
        if (
            parsed["vocals_detected"] is None
            or parsed["vocal_confidence"] is None
        ):
            missing.extend(["vocals_detected", "vocal_confidence"])
        try:
            recovered_payload = await self.adapter._recover_missing(
                model=model,
                audio_bytes=audio_bytes,
                duration_s=end_s - start_s,
                language_hint=language_hint,
                missing=missing,
                timeout=timeout,
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
            if _merge_recovered_vocal_presence(parsed, recovered):
                recovered_fields.extend(
                    ["vocals_detected", "vocal_confidence"]
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
                            "diagnostic": True,
                        },
                    )
                )
            if not parsed["lyrics"] and not _confident_no_vocals(parsed):
                vocals_detected = parsed["vocals_detected"]
                text = (
                    "定向重听检测到人声，但仍未确认可靠歌词。"
                    if vocals_detected is True
                    else "定向重听仍无法确认可靠歌词或人声状态。"
                )
                state.evidence.append(
                    Evidence(
                        id=f"omni.chunk.{index}.recovery.inconclusive",
                        source=self.adapter.source,
                        kind=EvidenceType.OBSERVED,
                        text=text,
                        confidence=0.0,
                        span=TimeSpan(start_s=start_s, end_s=end_s),
                        metadata={
                            "requested_fields": missing,
                            "diagnostic": True,
                        },
                    )
                )
        except Exception as exc:
            if self.adapter.should_abort_chunking(exc):
                raise
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

    def _record_batch_abort(
        self,
        state: ChunkAnalysisState,
        *,
        index: int,
        total_chunks: int,
        start_s: float,
        end_s: float,
        error: Exception,
    ) -> None:
        state.batch_aborted = True
        remaining = max(0, total_chunks - index)
        detail = (str(error).strip() or error.__class__.__name__)[:500]
        state.evidence.append(
            Evidence(
                id="omni.batch.error",
                source=self.adapter.source,
                kind=EvidenceType.OBSERVED,
                text=(
                    f"统一模型在第 {index} 个音频分块不可用；"
                    f"已停止提交剩余 {remaining} 个分块，"
                    f"避免 Worker busy 级联。原始错误：{detail}"
                ),
                confidence=0.0,
                span=TimeSpan(start_s=start_s, end_s=end_s),
                metadata={
                    "error_type": error.__class__.__name__,
                    "failed_chunk": index,
                    "skipped_chunks": remaining,
                },
            )
        )

    @staticmethod
    def _should_abort_consecutive_errors(
        state: ChunkAnalysisState,
        error: Exception,
    ) -> bool:
        """Abort a batch after repeated transport-level failures.

        ``should_abort_chunking`` handles a single error that proves the whole
        endpoint is unusable (Comni). This state machine catches the other
        common failure shape for OpenAI-compatible endpoints: a dead or half-dead
        server keeps raising the same transport error for every chunk, so each
        chunk wastes a full connect/read timeout before failing.
        """

        family = _error_family(error)
        if state.consecutive_error_family == family:
            state.consecutive_error_count += 1
        else:
            state.consecutive_error_family = family
            state.consecutive_error_count = 1
        return state.consecutive_error_count >= 3

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
        if state.batch_aborted:
            narrative = " ".join(state.narratives).strip()
            if not narrative:
                narrative = "统一模型服务中断；本地 DSP 指标仍可用。"
            await self.adapter._notify(
                progress,
                "model_synthesis",
                1.0,
                "模型服务不可用，已跳过远端综合并保留已有证据",
            )
            return narrative, state.themes[:8], []
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
        vocals_detected, vocal_confidence, vocal_evidence = (
            _aggregate_vocal_presence(state, self.adapter.source)
        )
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
                vocals_detected=vocals_detected,
                vocal_confidence=vocal_confidence,
                evidence=[*state.evidence, *vocal_evidence],
            ),
            literary=LiteraryResult(
                model=self.adapter.source,
                themes=themes,
                narrative=narrative,
                evidence=[],
            ),
        )


def _error_family(error: Exception) -> str:
    """Return a coarse family used to detect repeated transport failures.

    HTTP status errors carry their status code so a persistent 5xx is its own
    family; connection/read errors share one transport family because they all
    mean the endpoint is unreachable from the worker's perspective.
    """

    module = type(error).__module__
    name = type(error).__name__
    status = getattr(error, "response", None)
    status_code = getattr(status, "status_code", None)
    if isinstance(status_code, int) and status_code >= 500:
        return f"http:{status_code}"
    if "httpx" in module or name in {
        "ConnectionError",
        "TimeoutError",
        "ReadTimeout",
        "ConnectTimeout",
    }:
        return "transport"
    return f"{module}.{name}"


def _confident_no_vocals(parsed: dict[str, Any]) -> bool:
    """Return whether a chunk has an explicit, sufficiently strong no-voice result."""

    return (
        parsed.get("vocals_detected") is False
        and isinstance(parsed.get("vocal_confidence"), (int, float))
        and float(parsed["vocal_confidence"]) >= 0.7
    )


def _merge_recovered_vocal_presence(
    parsed: dict[str, Any],
    recovered: dict[str, Any],
) -> bool:
    """Fill an incomplete vocal judgment only from a complete recovered pair."""

    if (
        parsed.get("vocals_detected") is not None
        and parsed.get("vocal_confidence") is not None
    ):
        return False
    if (
        recovered.get("vocals_detected") is None
        or recovered.get("vocal_confidence") is None
    ):
        return False
    parsed["vocals_detected"] = recovered["vocals_detected"]
    parsed["vocal_confidence"] = recovered["vocal_confidence"]
    return True


def _aggregate_vocal_presence(
    state: ChunkAnalysisState,
    source: str,
) -> tuple[bool | None, float | None, list[Evidence]]:
    """Aggregate chunk judgments without treating an empty transcript as silence."""

    expected = max(1, state.expected_chunks)
    present = [
        (confidence, index)
        for detected, confidence, index in state.vocal_observations
        if detected and confidence >= 0.6
    ]
    if present:
        confidence = max(value for value, _ in present)
        detected: bool | None = True
        reason = "至少一个音频分块以足够置信度检测到人声。"
    else:
        absent = [
            (confidence, index)
            for detected, confidence, index in state.vocal_observations
            if not detected and confidence >= 0.7
        ]
        coverage = len(absent) / expected
        if coverage >= 0.8:
            confidence = min(
                sum(value for value, _ in absent) / len(absent),
                coverage,
            )
            detected = False
            reason = "绝大多数音频分块一致且高置信地报告无人声。"
        else:
            return None, None, []
    supporting_ids = [
        f"omni.chunk.{index}.vocal_presence"
        for _, index in (present if detected else absent)
    ]
    return detected, confidence, [
        Evidence(
            id="omni.vocal_presence",
            source=source,
            kind=EvidenceType.INFERRED,
            text=reason,
            confidence=confidence,
            metadata={
                "vocals_detected": detected,
                "expected_chunks": state.expected_chunks,
                "classified_chunks": len(state.vocal_observations),
                "supporting_evidence_ids": supporting_ids,
                "aggregation": "conservative_chunk_consensus",
            },
        )
    ]
