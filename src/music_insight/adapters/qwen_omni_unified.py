from __future__ import annotations

import base64
from collections.abc import Iterator
import copy
import io
import json
from pathlib import Path
from typing import Any
import wave

import httpx

from music_insight.adapters.base import UnifiedAudioAdapter
from music_insight.adapters.openai_compat_utils import (
    api_path,
    discover_model,
    extract_chat_content,
    parse_json_object,
)
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


class QwenOmniUnifiedAdapter(UnifiedAudioAdapter):
    """Single-service music analysis through an OpenAI-compatible Qwen Omni API."""

    source = "Qwen Omni"
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
        completions_path: str = "/v1/chat/completions",
        models_path: str = "/v1/models",
        model: str | None = None,
        chunk_seconds: float = 30.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.source = f"Qwen Omni · {self.endpoint}"
        self.completions_path = api_path(completions_path)
        self.models_path = api_path(models_path)
        self._resolved_model = model
        self.chunk_seconds = max(5.0, min(float(chunk_seconds), 60.0))

    async def analyze(self, asset: AudioAsset, dsp: DspResult) -> UnifiedAudioResult:
        model = await self._model()
        lyrics: list[LyricsSegment] = []
        instruments: list[str] = []
        sound_events: list[Evidence] = []
        emotions: list[Evidence] = []
        chunk_themes: list[str] = []
        chunk_narratives: list[str] = []
        evidence: list[Evidence] = []

        for index, (audio_bytes, start_s, end_s) in enumerate(
            self._wav_chunks(asset.path), start=1
        ):
            try:
                payload = await self._analyze_chunk(
                    model=model,
                    audio_bytes=audio_bytes,
                    start_s=start_s,
                    end_s=end_s,
                    language_hint=asset.language_hint,
                )
                parsed = self._parse_chunk(payload, index, start_s, end_s)
            except Exception as exc:
                evidence.append(
                    Evidence(
                        id=f"omni.chunk.{index}.error",
                        source=self.source,
                        kind=EvidenceType.OBSERVED,
                        text=(
                            f"第 {index} 个音频分块分析失败："
                            f"{(str(exc).strip() or exc.__class__.__name__)[:500]}"
                        ),
                        confidence=0.0,
                        span=TimeSpan(start_s=start_s, end_s=end_s),
                        metadata={"error_type": exc.__class__.__name__},
                    )
                )
                continue

            if not parsed["lyrics"]:
                missing = ["lyrics"]
                try:
                    recovered_payload = await self._recover_missing(
                        model=model,
                        audio_bytes=audio_bytes,
                        duration_s=end_s - start_s,
                        language_hint=asset.language_hint,
                        missing=missing,
                    )
                    recovered = self._parse_chunk(
                        recovered_payload, index, start_s, end_s
                    )
                    recovered_fields = []
                    if not parsed["lyrics"]:
                        parsed["lyrics"] = recovered["lyrics"]
                        if recovered["lyrics"]:
                            recovered_fields.append("lyrics")
                    if recovered_fields:
                        evidence.append(
                            Evidence(
                                id=f"omni.chunk.{index}.recovery",
                                source=self.source,
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
                        evidence.append(
                            Evidence(
                                id=f"omni.chunk.{index}.recovery.unavailable",
                                source=self.source,
                                kind=EvidenceType.OBSERVED,
                                text="定向重听仍未确认可靠歌词或人声情绪。",
                                confidence=0.0,
                                span=TimeSpan(start_s=start_s, end_s=end_s),
                                metadata={"requested_fields": missing},
                            )
                        )
                except Exception as exc:
                    evidence.append(
                        Evidence(
                            id=f"omni.chunk.{index}.recovery.unavailable",
                            source=self.source,
                            kind=EvidenceType.OBSERVED,
                            text=f"定向重听未返回可解析结果：{str(exc)[:500]}",
                            confidence=0.0,
                            span=TimeSpan(start_s=start_s, end_s=end_s),
                            metadata={"requested_fields": missing},
                        )
                    )

            lyrics.extend(parsed["lyrics"])
            instruments.extend(parsed["instruments"])
            sound_events.extend(parsed["sound_events"])
            emotions.extend(parsed["emotions"])
            chunk_themes.extend(parsed["themes"])
            if parsed["narrative"]:
                chunk_narratives.append(parsed["narrative"])
            evidence.append(
                Evidence(
                    id=f"omni.chunk.{index}.analysis",
                    source=self.source,
                    kind=EvidenceType.INFERRED,
                    text=parsed["narrative"] or f"已完成第 {index} 个音频分块分析。",
                    confidence=0.7,
                    span=TimeSpan(start_s=start_s, end_s=end_s),
                    metadata={"model": model, "basis": "direct audio"},
                )
            )

        lyrics = self._deduplicate_lyrics(lyrics, 80)
        instruments = self._deduplicate(instruments, 16)
        sound_events = self._deduplicate_evidence(sound_events, 48)
        emotions = self._deduplicate_evidence(emotions, 48)
        chunk_themes = self._deduplicate(chunk_themes, 10)
        narrative, themes, inferred_atmosphere, synthesis_evidence = (
            await self._synthesize_report(
                model=model,
                lyrics=lyrics,
                instruments=instruments,
                sound_events=sound_events,
                emotions=emotions,
                chunk_themes=chunk_themes,
                chunk_narratives=chunk_narratives,
                dsp=dsp,
            )
        )
        evidence.extend(synthesis_evidence)

        return UnifiedAudioResult(
            asr=AsrResult(
                model=self.source,
                lyrics=lyrics,
                evidence=[
                    Evidence(
                        id="omni.transcript",
                        source=self.source,
                        kind=EvidenceType.INFERRED,
                        text=(
                            "；".join(item.text for item in lyrics)
                            if lyrics
                            else "统一模型未确认可靠歌词。"
                        ),
                        metadata={"model": model},
                    )
                ],
            ),
            scene=AudioSceneResult(
                model=self.source,
                instruments=instruments,
                sound_events=sound_events,
                emotion_timeline=emotions,
                inferred_atmosphere=inferred_atmosphere,
                themes=chunk_themes,
                narrative=" ".join(chunk_narratives) or None,
                evidence=evidence,
            ),
            literary=LiteraryResult(
                model=self.source,
                themes=themes,
                narrative=narrative,
                evidence=[],
            ),
        )

    async def _analyze_chunk(
        self,
        model: str,
        audio_bytes: bytes,
        start_s: float,
        end_s: float,
        language_hint: str | None,
    ) -> dict[str, Any]:
        duration_s = end_s - start_s
        encoded = base64.b64encode(audio_bytes).decode("ascii")
        language_instruction = {
            "zh": "歌词若存在，逐字输出中文原文，不翻译。",
            "en": "If lyrics exist, transcribe the original English verbatim.",
        }.get(language_hint, "歌词若存在，保留原语言，不翻译。")
        instruction = (
            f"分析这段 {duration_s:.2f} 秒的音乐音频。{language_instruction}"
            "不要补写听不清的歌词。所有时间戳使用当前分块内的相对秒数，"
            f"范围必须在 0 到 {duration_s:.2f} 之间。每类时间事件最多 6 项。"
            "返回 JSON 对象，且只能包含这些字段：lyrics、instruments、"
            "sound_events、emotion_timeline、themes、narrative。"
            "lyrics、sound_events、emotion_timeline 的每项可包含 text、start_s、"
            "end_s、confidence；lyrics 还可包含 language。"
            "每个 lyrics 项只包含一行连续歌词，不得换行，"
            "不得补全分块之外的内容。"
            "只填入实际听到的内容，不要复述字段说明；无法确认时使用空数组或空字符串。"
        )
        request = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是严谨的音乐听觉分析器。只报告音频支持的内容，"
                        "区分听见与推断；只输出 JSON，不使用 Markdown。"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": encoded, "format": "wav"},
                        },
                    ],
                },
            ],
            "response_format": self._chunk_response_format(),
            "temperature": 0,
            "max_tokens": 1800,
        }
        return await self._chat_json(request, timeout=600.0)

    async def _recover_missing(
        self,
        model: str,
        audio_bytes: bytes,
        duration_s: float,
        language_hint: str | None,
        missing: list[str],
    ) -> dict[str, Any]:
        encoded = base64.b64encode(audio_bytes).decode("ascii")
        language_instruction = {
            "zh": "只转写实际听见的中文原词，不翻译。",
            "en": "Transcribe only the English words actually heard; do not translate.",
        }.get(language_hint, "保留歌词原语言，不翻译。")
        instruction = (
            f"重新仔细听这段 {duration_s:.2f} 秒音频，只补充这些缺失字段："
            f"{', '.join(missing)}。{language_instruction}"
            "听不清时必须留空，不得根据语境补写。情绪必须依据人声的音高、力度、"
            "音色或演唱方式；没有可确认人声时留空。时间戳必须位于音频时长内。"
            "返回只含所请字段的 JSON 对象。每项字段为 text、"
            "start_s、end_s、confidence；lyrics 项还可包含 language。"
            "每个 lyrics 项只包含一行连续歌词。不要复述字段说明。"
        )
        request = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是保守的歌声转写与人声情绪分析器。"
                        "宁可留空也不猜测；只输出 JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": encoded, "format": "wav"},
                        },
                    ],
                },
            ],
            "response_format": self._recovery_response_format(missing),
            "temperature": 0,
            "max_tokens": 1000,
        }
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

        compact = {
            "lyrics": [item.model_dump(mode="json") for item in lyrics[:40]],
            "instruments": instruments,
            "sound_events": [item.model_dump(mode="json") for item in sound_events[:30]],
            "emotion_timeline": [item.model_dump(mode="json") for item in emotions[:30]],
            "chunk_themes": chunk_themes,
            "chunk_descriptions": chunk_narratives[:20],
            "dsp": {
                "bpm": dsp.bpm,
                "bpm_confidence": dsp.bpm_confidence,
                "key": dsp.key,
                "key_confidence": dsp.key_confidence,
            },
        }
        request = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是音乐证据融合器。只根据提供的证据写结论，"
                        "冲突或不确定时明确说明。只输出 JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "返回只含 themes、narrative 和 inferred_atmosphere 的 JSON 对象。"
                        "themes 填入实际主题，narrative 写四至八句综合分析；"
                        "inferred_atmosphere 是根据歌词、能量、分块描述和已有情绪证据"
                        "推断的整体氛围，最多 4 项；每项包含 text、confidence 和 basis。"
                        "同义或高度重叠的氛围必须合并，不要同时输出近义词。"
                        "它不是直接听觉观测，证据不足时留空，不得伪造时间戳。"
                        "不要复制这些字段说明。证据："
                        + json.dumps(compact, ensure_ascii=False)
                    ),
                },
            ],
            "response_format": self._final_response_format(),
            "temperature": 0,
            "max_tokens": 1400,
        }
        try:
            parsed = await self._chat_json(request, timeout=420.0)
            narrative = str(parsed.get("narrative") or "").strip()
            if not self._meaningful(narrative):
                raise ValueError("最终融合缺少 narrative。")
            themes = self._strings(parsed.get("themes"), 8)
            inferred_atmosphere = self._atmosphere_items(
                parsed.get("inferred_atmosphere"), model
            )
            evidence = [
                Evidence(
                    id="omni.final.synthesis",
                    source=self.source,
                    kind=EvidenceType.INTERPRETIVE,
                    text=narrative,
                    confidence=0.7,
                    metadata={"model": model, "basis": "model chunks + local DSP"},
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

    async def _chat(self, request: dict[str, Any], timeout: float) -> str:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                f"{self.endpoint}{self.completions_path}", json=request
            )
            if (
                response.status_code in {400, 422}
                and request.get("response_format", {}).get("type") == "json_schema"
            ):
                fallback_request = dict(request)
                fallback_request["response_format"] = {"type": "json_object"}
                response = await client.post(
                    f"{self.endpoint}{self.completions_path}",
                    json=fallback_request,
                )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip().replace("\n", " ")[:500]
            raise RuntimeError(
                f"{self.source} HTTP {response.status_code}: {detail}"
            ) from exc
        return extract_chat_content(response.json())

    async def _chat_json(
        self, request: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        content = await self._chat(request, timeout)
        try:
            return parse_json_object(content)
        except ValueError:
            retry_request = copy.deepcopy(request)
            retry_request["response_format"] = {"type": "json_object"}
            retry_request["max_tokens"] = min(
                int(retry_request.get("max_tokens", 1200)), 1200
            )
            messages = retry_request.get("messages") or []
            if messages and isinstance(messages[0].get("content"), str):
                messages[0]["content"] += (
                    " 上一次输出不是可解析 JSON。请缩短结果，"
                    "严格检查逗号、引号和括号，只输出一个 JSON 对象。"
                )
            retry_content = await self._chat(retry_request, timeout)
            return parse_json_object(retry_content)

    @staticmethod
    def _chunk_response_format() -> dict[str, Any]:
        timed_item = {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "start_s": {"type": "number", "minimum": 0},
                "end_s": {"type": "number", "minimum": 0},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["text"],
            "additionalProperties": False,
        }
        lyric_item = {
            **timed_item,
            "properties": {
                **timed_item["properties"],
                "language": {"type": "string"},
            },
        }
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "music_chunk_analysis",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "lyrics": {"type": "array", "items": lyric_item},
                        "instruments": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "sound_events": {"type": "array", "items": timed_item},
                        "emotion_timeline": {
                            "type": "array",
                            "items": timed_item,
                        },
                        "themes": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "narrative": {"type": "string"},
                    },
                    "required": [
                        "lyrics",
                        "instruments",
                        "sound_events",
                        "emotion_timeline",
                        "themes",
                        "narrative",
                    ],
                    "additionalProperties": False,
                },
            },
        }

    @classmethod
    def _recovery_response_format(
        cls, missing: list[str] | None = None
    ) -> dict[str, Any]:
        chunk_schema = cls._chunk_response_format()["json_schema"]["schema"]
        requested = [
            field
            for field in (missing or ["lyrics", "emotion_timeline"])
            if field in {"lyrics", "emotion_timeline"}
        ]
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "music_missing_fields",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        field: chunk_schema["properties"][field]
                        for field in requested
                    },
                    "required": requested,
                    "additionalProperties": False,
                },
            },
        }

    @staticmethod
    def _final_response_format() -> dict[str, Any]:
        atmosphere_item = {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "basis": {"type": "string"},
            },
            "required": ["text", "confidence", "basis"],
            "additionalProperties": False,
        }
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "music_final_report",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "themes": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "narrative": {"type": "string"},
                        "inferred_atmosphere": {
                            "type": "array",
                            "items": atmosphere_item,
                        },
                    },
                    "required": ["themes", "narrative", "inferred_atmosphere"],
                    "additionalProperties": False,
                },
            },
        }

    async def _model(self) -> str:
        if not self._resolved_model:
            self._resolved_model = await discover_model(
                self.endpoint, self.models_path
            )
        return self._resolved_model

    def _wav_chunks(self, path: Path) -> Iterator[tuple[bytes, float, float]]:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            total_frames = source.getnframes()
            frames_per_chunk = max(1, int(self.chunk_seconds * sample_rate))
            start_frame = 0
            while start_frame < total_frames:
                source.setpos(start_frame)
                frame_count = min(frames_per_chunk, total_frames - start_frame)
                frames = source.readframes(frame_count)
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as output:
                    output.setnchannels(channels)
                    output.setsampwidth(sample_width)
                    output.setframerate(sample_rate)
                    output.writeframes(frames)
                start_s = start_frame / sample_rate
                end_s = (start_frame + frame_count) / sample_rate
                yield buffer.getvalue(), start_s, end_s
                start_frame += frame_count

    def _parse_chunk(
        self,
        payload: dict[str, Any],
        index: int,
        chunk_start: float,
        chunk_end: float,
    ) -> dict[str, Any]:
        duration = chunk_end - chunk_start
        lyrics = []
        raw_lyrics = payload.get("lyrics") or []
        if isinstance(raw_lyrics, str):
            raw_lyrics = [{"text": raw_lyrics}]
        if isinstance(raw_lyrics, list):
            for item in raw_lyrics[:20]:
                if isinstance(item, str):
                    item = {"text": item}
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                if not self._meaningful(text):
                    continue
                span = self._offset_span(item, chunk_start, duration)
                if span is None:
                    span = TimeSpan(start_s=chunk_start, end_s=chunk_end)
                lines = [
                    line.strip()
                    for line in text.splitlines()
                    if self._meaningful(line.strip())
                ]
                line_duration = (span.end_s - span.start_s) / max(1, len(lines))
                for line_index, line in enumerate(lines):
                    line_span = span
                    if len(lines) > 1:
                        line_span = TimeSpan(
                            start_s=span.start_s + line_index * line_duration,
                            end_s=span.start_s + (line_index + 1) * line_duration,
                        )
                    lyrics.append(
                        LyricsSegment(
                            text=line,
                            span=line_span,
                            language=item.get("language") or item.get("lang"),
                            confidence=self._confidence(item.get("confidence")),
                        )
                    )

        sound_events = self._evidence_items(
            payload.get("sound_events"),
            f"omni.chunk.{index}.sound",
            chunk_start,
            duration,
        )
        emotions = self._evidence_items(
            payload.get("emotion_timeline"),
            f"omni.chunk.{index}.emotion",
            chunk_start,
            duration,
        )
        return {
            "lyrics": lyrics,
            "instruments": self._strings(payload.get("instruments"), 12),
            "sound_events": sound_events,
            "emotions": emotions,
            "themes": self._strings(payload.get("themes"), 6),
            "narrative": self._meaningful_text(payload.get("narrative")),
        }

    def _evidence_items(
        self,
        value: Any,
        prefix: str,
        chunk_start: float,
        duration: float,
    ) -> list[Evidence]:
        if not isinstance(value, list):
            return []
        results = []
        for index, item in enumerate(value[:6], start=1):
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict):
                continue
            text = str(
                item.get("text")
                or item.get("label")
                or item.get("description")
                or ""
            ).strip()
            if not self._meaningful(text):
                continue
            start_value = item.get("start_s", item.get("start_time"))
            if isinstance(start_value, (int, float)) and start_value > duration:
                continue
            span = self._offset_span(item, chunk_start, duration)
            if self._is_boundary_artifact(prefix, text, span, chunk_start, duration):
                continue
            results.append(
                Evidence(
                    id=f"{prefix}.{index}",
                    source=self.source,
                    kind=EvidenceType.INFERRED,
                    text=text,
                    confidence=self._confidence(item.get("confidence")),
                    span=span,
                )
            )
        return results

    def _atmosphere_items(self, value: Any, model: str) -> list[Evidence]:
        if not isinstance(value, list):
            return []
        results = []
        seen = set()
        for index, item in enumerate(value[:4], start=1):
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            basis = str(item.get("basis") or "").strip()
            if not self._meaningful(text) or not self._meaningful(basis):
                continue
            canonical = self.atmosphere_aliases.get(text.casefold(), text.casefold())
            if canonical in seen:
                continue
            seen.add(canonical)
            results.append(
                Evidence(
                    id=f"omni.final.atmosphere.{index}",
                    source=self.source,
                    kind=EvidenceType.INTERPRETIVE,
                    text=text,
                    confidence=self._confidence(item.get("confidence")),
                    metadata={"basis": basis, "model": model},
                )
            )
        return results

    def _is_boundary_artifact(
        self,
        prefix: str,
        text: str,
        span: TimeSpan | None,
        chunk_start: float,
        duration: float,
    ) -> bool:
        if ".sound" not in prefix or span is None:
            return False
        label = " ".join(text.casefold().split())
        if label not in {
            "click",
            "clicking",
            "click sound",
            "pop",
            "popping",
            "点击声",
            "咔哒声",
            "爆音",
        }:
            return False
        event_duration = span.end_s - span.start_s
        local_start = span.start_s - chunk_start
        local_end = span.end_s - chunk_start
        at_internal_start = chunk_start > 0 and local_start <= 0.05
        at_full_chunk_end = (
            duration >= self.chunk_seconds - 0.05
            and duration - local_end <= 0.05
        )
        return event_duration <= 0.25 and (at_internal_start or at_full_chunk_end)

    @staticmethod
    def _offset_span(
        item: dict[str, Any], chunk_start: float, duration: float
    ) -> TimeSpan | None:
        start = item.get("start_s", item.get("start_time"))
        end = item.get("end_s", item.get("end_time"))
        if (
            isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and 0 <= start <= end
            and start <= duration
        ):
            return TimeSpan(
                start_s=chunk_start + float(start),
                end_s=chunk_start + min(float(end), duration),
            )
        return None

    @staticmethod
    def _confidence(value: Any) -> float | None:
        if not isinstance(value, (int, float)):
            return None
        return min(1.0, max(0.0, float(value)))

    @classmethod
    def _strings(cls, value: Any, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        output = []
        for item in value:
            text = cls._normalize_label(str(item).strip())
            if cls._meaningful(text):
                output.append(text)
            if len(output) >= limit:
                break
        return output

    @classmethod
    def _normalize_label(cls, value: str) -> str:
        compact = " ".join(value.split())
        return cls.label_aliases.get(compact.casefold(), compact)

    @classmethod
    def _meaningful(cls, value: str) -> bool:
        text = value.strip()
        return bool(text) and text.casefold() not in {
            item.casefold() for item in cls.placeholder_values
        }

    @classmethod
    def _meaningful_text(cls, value: Any) -> str:
        text = str(value or "").strip()
        return text if cls._meaningful(text) else ""

    @staticmethod
    def _deduplicate(values: list[str], limit: int) -> list[str]:
        seen = set()
        output = []
        for value in values:
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            output.append(value)
            if len(output) >= limit:
                break
        return output

    @staticmethod
    def _deduplicate_lyrics(
        values: list[LyricsSegment], limit: int
    ) -> list[LyricsSegment]:
        seen = set()
        output = []
        for item in values:
            span_key = (
                round(item.span.start_s, 1),
                round(item.span.end_s, 1),
            ) if item.span else (None, None)
            key = (" ".join(item.text.casefold().split()), span_key)
            if key in seen:
                continue
            seen.add(key)
            output.append(item)
            if len(output) >= limit:
                break
        return output

    @staticmethod
    def _deduplicate_evidence(values: list[Evidence], limit: int) -> list[Evidence]:
        seen = set()
        output = []
        for item in values:
            span_key = (
                round(item.span.start_s, 1),
                round(item.span.end_s, 1),
            ) if item.span else (None, None)
            key = (" ".join(item.text.casefold().split()), span_key)
            if key in seen:
                continue
            seen.add(key)
            output.append(item)
            if len(output) >= limit:
                break
        return output
