from __future__ import annotations

from abc import abstractmethod
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
import inspect
from pathlib import Path
from typing import Any

from music_insight.adapters.base import UnifiedAudioAdapter
from music_insight.adapters.openai_compat_utils import parse_json_object
from music_insight.adapters.structured_omni_audio import (
    count_wav_chunks,
    iter_wav_chunks,
)
from music_insight.adapters.structured_omni_parsing import (
    atmosphere_items,
    confidence,
    deduplicate,
    deduplicate_evidence,
    deduplicate_lyrics,
    evidence_items,
    filter_lyrics_quality,
    is_boundary_artifact,
    lyric_units,
    meaningful,
    meaningful_text,
    near_duplicate,
    normalize_label,
    offset_span,
    parse_chunk,
    strings,
)
from music_insight.adapters.structured_omni_requests import (
    chunk_analysis_request,
    lyrics_quality_recovery_request,
    missing_recovery_request,
    synthesis_request,
)
from music_insight.adapters.structured_omni_schemas import (
    chunk_response_format,
    final_response_format,
    recovery_response_format,
)
from music_insight.adapters.structured_output import (
    StructuredOutputError,
    schema_retry_request,
    validate_structured_output,
)
from music_insight.adapters.structured_teaching_audio import (
    prepare_relisten_excerpts,
)
from music_insight.adapters.structured_teaching_parsing import (
    parse_relisten_result,
    parse_teaching_chat_response,
    parse_understanding_map,
)
from music_insight.adapters.structured_teaching_requests import (
    relisten_request,
    teaching_chat_request,
    understanding_map_request,
)
from music_insight.adapters.structured_teaching_schemas import (
    relisten_response_format,
    teaching_chat_response_format,
    understanding_map_response_format,
)
from music_insight.adapters.structured_omni_workflow import (
    StructuredOmniAnalysisWorkflow,
)
from music_insight.schemas import (
    AudioAsset,
    AudioSceneResult,
    DspResult,
    Evidence,
    EvidenceType,
    LiteraryResult,
    LyricsSegment,
    TimeSpan,
    UnifiedAudioResult,
    VerifiedLyricsSynthesisResult,
)
from music_insight.teaching.models import (
    MapGenerationContext,
    MusicUnderstandingMap,
    RelistenRequest,
    RelistenResult,
    TeachingChatContext,
    TeachingChatResponse,
    TeachingTimeSpan,
)


class StructuredOmniAdapter(UnifiedAudioAdapter):
    """Provider-neutral structured music-analysis workflow.

    Subclasses own service discovery and wire transport. This class owns only
    chunking, result parsing, quality filters, and workflow entry points, so
    OpenAI HTTP and Comni WebSocket protocols remain isolated.
    """

    source = "统一音频模型"
    placeholder_values = {
        "原文",
        "歌词",
        "声源",
        "主题",
        "事件及依据",
        "情绪及依据",
        "声音支持的主题",
        "两至四句局部声音描述",
        "四至八句综合音乐分析",
        "音乐播放",
        "actual lyrics",
        "instrument",
        "sound event",
        "emotion",
        "theme",
    }
    label_aliases = {
        "electricguitar": "electric guitar",
        "acousticguitar": "acoustic guitar",
        "bassguitar": "bass guitar",
        "indierock": "indie rock",
    }
    atmosphere_aliases = {
        "愉快": "欢快",
        "快乐": "欢快",
        "平静": "宁静",
        "peaceful": "calm",
        "joyful": "cheerful",
        "happy": "cheerful",
    }

    def __init__(
        self,
        endpoint: str,
        *,
        model: str | None = None,
        chunk_seconds: float = 30.0,
        chunk_overlap_seconds: float = 1.5,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._resolved_model = model
        self.chunk_seconds = max(5.0, min(float(chunk_seconds), 60.0))
        self.chunk_overlap_seconds = max(
            0.0,
            min(float(chunk_overlap_seconds), self.chunk_seconds / 3),
        )

    @asynccontextmanager
    async def _request_scope(self) -> AsyncIterator[None]:
        """Keep optional provider resources alive for one workflow invocation."""

        yield

    async def retry_lyrics(
        self,
        audio_bytes: bytes,
        duration_s: float,
        language_hint: str | None,
    ) -> tuple[list[LyricsSegment], list[str]]:
        async with self._request_scope():
            model = await self._model()
            payload = await self._recover_lyrics_quality(
                model=model,
                audio_bytes=audio_bytes,
                duration_s=duration_s,
                language_hint=language_hint,
                issues=["用户请求重新聆听并校准此分块"],
            )
        parsed = self._parse_chunk(payload, 1, 0.0, duration_s)
        return self._filter_lyrics_quality(parsed["lyrics"])

    async def analyze(
        self,
        asset: AudioAsset,
        dsp: DspResult,
        progress: Callable[[str, float, str], Awaitable[None] | None] | None = None,
    ) -> UnifiedAudioResult:
        async with self._request_scope():
            return await StructuredOmniAnalysisWorkflow(self).analyze(
                asset,
                dsp,
                progress,
            )

    async def resynthesize_verified_lyrics(
        self,
        lyrics: list[LyricsSegment],
        scene: AudioSceneResult,
        dsp: DspResult,
    ) -> VerifiedLyricsSynthesisResult:
        """Rebuild lyric-sensitive conclusions without re-listening to audio."""

        async with self._request_scope():
            model = await self._model()
            narrative, themes, atmosphere, evidence = (
                await self._synthesize_report(
                    model=model,
                    lyrics=lyrics,
                    instruments=scene.instruments,
                    sound_events=scene.sound_events,
                    emotions=scene.emotion_timeline,
                    # Chunk themes/descriptions may have been inferred from the
                    # primary transcript, so they are intentionally excluded.
                    chunk_themes=[],
                    chunk_narratives=[],
                    dsp=dsp,
                )
            )
        if any(item.id.endswith(".error") for item in evidence):
            raise RuntimeError("统一模型无法基于已验证歌词重新综合报告。")

        rebased_evidence = [
            item.model_copy(
                update={
                    "id": item.id.replace(
                        "omni.final.",
                        "omni.final.verified_lyrics.",
                        1,
                    ),
                    "metadata": {
                        **item.metadata,
                        "basis": "verified lyrics + audio scene + local DSP",
                    },
                }
            )
            for item in evidence
        ]
        rebased_atmosphere = [
            item.model_copy(
                update={
                    "id": item.id.replace(
                        "omni.final.",
                        "omni.final.verified_lyrics.",
                        1,
                    ),
                    "metadata": {
                        **item.metadata,
                        "basis_type": "verified_lyrics_resynthesis",
                    },
                }
            )
            for item in atmosphere
        ]
        return VerifiedLyricsSynthesisResult(
            literary=LiteraryResult(
                model=self.source,
                themes=themes,
                narrative=narrative,
                evidence=rebased_evidence,
            ),
            inferred_atmosphere=rebased_atmosphere,
            evidence=rebased_evidence,
        )

    async def build_understanding_map(
        self,
        context: MapGenerationContext,
    ) -> MusicUnderstandingMap:
        """Generate a structured guide through the public teaching capability."""

        async with self._request_scope():
            model = await self._model()
            request = understanding_map_request(
                model=model,
                context=context,
                response_format=understanding_map_response_format(),
            )
            payload = await self._chat_json(request, timeout=420.0)
        return parse_understanding_map(payload, context)

    async def answer_music_question(
        self,
        context: TeachingChatContext,
    ) -> TeachingChatResponse:
        """Answer from bounded, time-local context without reanalyzing a song."""

        async with self._request_scope():
            model = await self._model()
            request = teaching_chat_request(
                model=model,
                context=context,
                response_format=teaching_chat_response_format(),
            )
            payload = await self._chat_json(request, timeout=300.0)
        return parse_teaching_chat_response(payload)

    async def listen_to_excerpts(
        self,
        request: RelistenRequest,
    ) -> RelistenResult:
        """Re-listen to at most two locally bounded excerpts, never a whole song."""

        excerpts = await self._prepare_teaching_excerpts(request)
        spans = [span for _, span in excerpts]
        async with self._request_scope():
            model = await self._model()
            model_request = relisten_request(
                model=model,
                question=request.question,
                excerpts=excerpts,
                language=request.language,
                response_format=relisten_response_format(
                    excerpt_count=len(excerpts)
                ),
            )
            payload = await self._chat_json(model_request, timeout=300.0)
        return parse_relisten_result(
            payload,
            request=request,
            spans=spans,
        )

    async def _prepare_teaching_excerpts(
        self,
        request: RelistenRequest,
    ) -> list[tuple[bytes, TeachingTimeSpan]]:
        return await prepare_relisten_excerpts(request)

    @staticmethod
    async def _notify(
        callback: Callable[[str, float, str], Awaitable[None] | None] | None,
        stage: str,
        progress: float,
        message: str,
    ) -> None:
        if callback is None:
            return
        observed = callback(stage, max(0.0, min(float(progress), 1.0)), message)
        if inspect.isawaitable(observed):
            await observed

    @abstractmethod
    async def _model(self) -> str:
        raise NotImplementedError

    async def _chat_json(
        self,
        request: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        async with asyncio.timeout(timeout):
            content = await self._chat(request, timeout)
            try:
                payload = parse_json_object(content)
                validate_structured_output(payload, request)
                return payload
            except StructuredOutputError as first_error:
                retry = schema_retry_request(
                    request,
                    first_error,
                    json_object_fallback=False,
                )
            except ValueError as first_error:
                retry = schema_retry_request(
                    request,
                    first_error,
                    json_object_fallback=True,
                )

            retry_content = await self._chat(retry, timeout)
            try:
                repaired = parse_json_object(retry_content)
                validate_structured_output(repaired, request)
            except (StructuredOutputError, ValueError) as second_error:
                raise StructuredOutputError(
                    "模型连续两次未返回符合结构契约的 JSON；"
                    f"第二次错误：{str(second_error)[:500]}"
                ) from second_error
            return repaired

    @abstractmethod
    async def _chat(self, request: dict[str, Any], timeout: float) -> str:
        raise NotImplementedError

    async def _analyze_chunk(
        self,
        model: str,
        audio_bytes: bytes,
        start_s: float,
        end_s: float,
        language_hint: str | None,
    ) -> dict[str, Any]:
        duration_s = end_s - start_s
        request = chunk_analysis_request(
            model=model,
            audio_bytes=audio_bytes,
            duration_s=duration_s,
            language_hint=language_hint,
            response_format=self._chunk_response_format(),
        )
        return await self._chat_json(request, timeout=600.0)

    async def _recover_missing(
        self,
        model: str,
        audio_bytes: bytes,
        duration_s: float,
        language_hint: str | None,
        missing: list[str],
    ) -> dict[str, Any]:
        request = missing_recovery_request(
            model=model,
            audio_bytes=audio_bytes,
            duration_s=duration_s,
            language_hint=language_hint,
            missing=missing,
            response_format=self._recovery_response_format(missing),
        )
        return await self._chat_json(request, timeout=600.0)

    async def _recover_lyrics_quality(
        self,
        model: str,
        audio_bytes: bytes,
        duration_s: float,
        language_hint: str | None,
        issues: list[str],
    ) -> dict[str, Any]:
        request = lyrics_quality_recovery_request(
            model=model,
            audio_bytes=audio_bytes,
            duration_s=duration_s,
            language_hint=language_hint,
            issues=issues,
            response_format=self._recovery_response_format(["lyrics"]),
        )
        return await self._chat_json(request, timeout=600.0)

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
    ) -> tuple[str, list[str], list[Evidence], list[Evidence]]:
        if not any(
            [lyrics, instruments, sound_events, emotions, chunk_narratives]
        ):
            error = Evidence(
                id="omni.final.error",
                source=self.source,
                kind=EvidenceType.OBSERVED,
                text="统一模型的所有音频分块均未返回可融合的结构化证据。",
                confidence=0.0,
            )
            return "统一模型未生成可靠分析；本地 DSP 指标仍可用。", [], [], [error]

        request = synthesis_request(
            model=model,
            lyrics=lyrics,
            instruments=instruments,
            sound_events=sound_events,
            emotions=emotions,
            chunk_themes=chunk_themes,
            chunk_narratives=chunk_narratives,
            dsp=dsp,
            response_format=self._final_response_format(),
        )
        try:
            parsed = await self._chat_json(request, timeout=420.0)
            narrative = str(parsed.get("narrative") or "").strip()
            if not self._meaningful(narrative):
                raise ValueError("最终融合缺少 narrative。")
            themes = self._strings(parsed.get("themes"), 8)
            inferred_atmosphere = self._atmosphere_items(
                parsed.get("inferred_atmosphere"),
                model,
            )
            evidence = [
                Evidence(
                    id="omni.final.synthesis",
                    source=self.source,
                    kind=EvidenceType.INTERPRETIVE,
                    text=narrative,
                    confidence=0.7,
                    metadata={
                        "model": model,
                        "basis": "model chunks + local DSP",
                    },
                )
            ]
            return narrative, themes, inferred_atmosphere, evidence
        except Exception as exc:
            narrative = " ".join(chunk_narratives).strip()
            if not narrative:
                narrative = "统一模型未生成完整综合报告；本地 DSP 指标仍可用。"
            error = Evidence(
                id="omni.final.error",
                source=self.source,
                kind=EvidenceType.OBSERVED,
                text=f"统一模型最终融合失败，已使用分块描述：{str(exc)[:500]}",
                confidence=0.0,
                metadata={"error_type": exc.__class__.__name__},
            )
            return narrative, chunk_themes[:8], [], [error]

    def _wav_chunks(self, path: Path) -> Iterator[tuple[bytes, float, float]]:
        return iter_wav_chunks(
            path,
            self.chunk_seconds,
            self.chunk_overlap_seconds,
        )

    def _chunk_count(self, path: Path) -> int:
        return count_wav_chunks(
            path,
            self.chunk_seconds,
            self.chunk_overlap_seconds,
        )

    def _parse_chunk(
        self,
        payload: dict[str, Any],
        index: int,
        chunk_start: float,
        chunk_end: float,
    ) -> dict[str, Any]:
        return parse_chunk(
            payload,
            index,
            chunk_start,
            chunk_end,
            source=self.source,
            chunk_seconds=self.chunk_seconds,
            placeholder_values=self.placeholder_values,
            label_aliases=self.label_aliases,
        )

    def _evidence_items(
        self,
        value: Any,
        prefix: str,
        chunk_start: float,
        duration: float,
    ) -> list[Evidence]:
        return evidence_items(
            value,
            prefix,
            chunk_start,
            duration,
            source=self.source,
            chunk_seconds=self.chunk_seconds,
            placeholder_values=self.placeholder_values,
        )

    def _atmosphere_items(self, value: Any, model: str) -> list[Evidence]:
        return atmosphere_items(
            value,
            model,
            source=self.source,
            placeholder_values=self.placeholder_values,
            atmosphere_aliases=self.atmosphere_aliases,
        )

    def _is_boundary_artifact(
        self,
        prefix: str,
        text: str,
        span: TimeSpan | None,
        chunk_start: float,
        duration: float,
    ) -> bool:
        return is_boundary_artifact(
            prefix,
            text,
            span,
            chunk_start,
            duration,
            self.chunk_seconds,
        )

    @staticmethod
    def _offset_span(
        item: dict[str, Any],
        chunk_start: float,
        duration: float,
    ) -> TimeSpan | None:
        return offset_span(item, chunk_start, duration)

    @staticmethod
    def _confidence(value: Any) -> float | None:
        return confidence(value)

    @classmethod
    def _strings(cls, value: Any, limit: int) -> list[str]:
        return strings(
            value,
            limit,
            placeholder_values=cls.placeholder_values,
            label_aliases=cls.label_aliases,
        )

    @classmethod
    def _normalize_label(cls, value: str) -> str:
        return normalize_label(value, cls.label_aliases)

    @classmethod
    def _meaningful(cls, value: str) -> bool:
        return meaningful(value, cls.placeholder_values)

    @classmethod
    def _meaningful_text(cls, value: Any) -> str:
        return meaningful_text(value, cls.placeholder_values)

    @staticmethod
    def _deduplicate(values: list[str], limit: int) -> list[str]:
        return deduplicate(values, limit)

    @staticmethod
    def _deduplicate_lyrics(
        values: list[LyricsSegment],
        limit: int,
    ) -> list[LyricsSegment]:
        return deduplicate_lyrics(values, limit)

    @classmethod
    def _filter_lyrics_quality(
        cls,
        values: list[LyricsSegment],
    ) -> tuple[list[LyricsSegment], list[str]]:
        return filter_lyrics_quality(values)

    @staticmethod
    def _lyric_units(text: str) -> int:
        return lyric_units(text)

    @classmethod
    def _near_duplicate(
        cls,
        first: LyricsSegment,
        second: LyricsSegment,
    ) -> bool:
        return near_duplicate(first, second)

    @staticmethod
    def _deduplicate_evidence(
        values: list[Evidence],
        limit: int,
    ) -> list[Evidence]:
        return deduplicate_evidence(values, limit)

    @staticmethod
    def _chunk_response_format() -> dict[str, Any]:
        return chunk_response_format()

    @classmethod
    def _recovery_response_format(
        cls,
        missing: list[str] | None = None,
    ) -> dict[str, Any]:
        return recovery_response_format(missing)

    @staticmethod
    def _final_response_format() -> dict[str, Any]:
        return final_response_format()
