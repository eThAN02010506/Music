from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any, Literal, TypeAlias
import wave

import httpx

from music_insight.adapters.base import AsrVerifier
from music_insight.adapters.openai_compat_utils import api_path
from music_insight.schemas import (
    AsrVerificationResult,
    AudioAsset,
    Evidence,
    EvidenceType,
    LyricsSegment,
    TimeSpan,
)


class AsrVerificationProtocolError(RuntimeError):
    """The verifier responded, but not with a safe timestamped ASR result."""


class AsrVerificationHttpError(RuntimeError):
    """A redacted upstream HTTP failure."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"ASR 验证接口返回 HTTP {status_code}。")


AsrVerifierDialect: TypeAlias = Literal["openai_whisper", "crisp_asr"]
ParsedSegment: TypeAlias = tuple[LyricsSegment, float | None, float | None]


class OpenAIAsrVerifier(AsrVerifier):
    """Secondary ASR over the OpenAI/Whisper multipart transcription contract."""

    MAX_RESPONSE_BYTES = 10 * 1024 * 1024
    MAX_SEGMENTS = 600
    MAX_SEGMENT_TEXT = 500

    def __init__(
        self,
        *,
        endpoint: str,
        dialect: AsrVerifierDialect,
        transcriptions_path: str = "/v1/audio/transcriptions",
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 600.0,
        vad: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        if dialect not in {"openai_whisper", "crisp_asr"}:
            raise ValueError("Unsupported ASR verifier dialect.")
        self.dialect = dialect
        self.transcriptions_path = api_path(transcriptions_path)
        self.model = model.strip() if model and model.strip() else None
        if self.dialect == "openai_whisper" and self.model is None:
            raise ValueError(
                "model is required for the openai_whisper ASR dialect"
            )
        if self.dialect != "crisp_asr" and vad:
            raise ValueError("vad is available only for the crisp_asr dialect")
        self.api_key = api_key.strip() if api_key and api_key.strip() else None
        self.timeout_seconds = timeout_seconds
        self.vad = vad
        self.transport = transport
        self.source = f"OpenAI-compatible ASR · {self.endpoint}"

    async def verify(self, asset: AudioAsset) -> AsrVerificationResult:
        duration_s = self._wav_duration(asset.path)
        payload = await self._transcribe(asset)
        return self._parse_payload(
            payload,
            duration_s=duration_s,
            language_hint=asset.language_hint,
        )

    async def _transcribe(self, asset: AudioAsset) -> dict[str, Any]:
        data, headers, timeout = self._transcription_request_options(asset)
        url = f"{self.endpoint}{self.transcriptions_path}"
        async with httpx.AsyncClient(
            trust_env=False,
            transport=self.transport,
            limits=httpx.Limits(
                max_connections=1,
                max_keepalive_connections=1,
            ),
        ) as client:
            status_code, response_body = await self._send_transcription(
                client,
                asset=asset,
                url=url,
                data=data,
                headers=headers,
                timeout=timeout,
            )
        return self._decode_transcription_response(
            status_code,
            response_body,
        )

    def _transcription_request_options(
        self,
        asset: AudioAsset,
    ) -> tuple[dict[str, str], dict[str, str] | None, httpx.Timeout]:
        data: dict[str, str] = {
            "response_format": "verbose_json",
            "temperature": "0",
        }
        if self.dialect == "openai_whisper":
            model = self.model
            if model is None:
                raise RuntimeError(
                    "OpenAI Whisper verifier lost its configured model."
                )
            data["model"] = model
            data["timestamp_granularities[]"] = "segment"
        elif self.model:
            data["model"] = self.model
        # ``vad`` is a CrispASR extension. It is never sent for standard
        # Whisper-compatible requests and is opt-in even for CrispASR.
        if self.dialect == "crisp_asr" and self.vad:
            data["vad"] = "true"
        if asset.language_hint:
            data["language"] = asset.language_hint
        headers = (
            {"Authorization": f"Bearer {self.api_key}"}
            if self.api_key
            else None
        )
        timeout = httpx.Timeout(
            self.timeout_seconds,
            connect=min(10.0, self.timeout_seconds),
            write=min(120.0, self.timeout_seconds),
            pool=min(10.0, self.timeout_seconds),
        )
        return data, headers, timeout

    async def _send_transcription(
        self,
        client: httpx.AsyncClient,
        *,
        asset: AudioAsset,
        url: str,
        data: dict[str, str],
        headers: dict[str, str] | None,
        timeout: httpx.Timeout,
    ) -> tuple[int | None, bytes | None]:
        status_code: int | None = None
        response_body: bytes | None = None
        for attempt in range(2):
            try:
                with asset.path.open("rb") as audio:
                    async with client.stream(
                        "POST",
                        url,
                        data=data,
                        files={
                            "file": (
                                asset.path.name,
                                audio,
                                asset.media_type or "audio/wav",
                            )
                        },
                        headers=headers,
                        timeout=timeout,
                    ) as response:
                        status_code = response.status_code
                        response_body = await self._read_bounded(response)
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadError,
                httpx.RemoteProtocolError,
            ):
                if attempt:
                    raise
                await asyncio.sleep(0.25)
                continue
            if status_code not in {429, 502, 503, 504} or attempt:
                break
            await asyncio.sleep(0.25)
        return status_code, response_body

    def _decode_transcription_response(
        self,
        status_code: int | None,
        response_body: bytes | None,
    ) -> dict[str, Any]:
        if status_code is None or response_body is None:
            raise RuntimeError(f"{self.source} 未返回响应。")
        if status_code >= 400:
            # The bounded upstream body has deliberately been consumed and
            # discarded. Never propagate it into logs, evidence, or clients.
            raise AsrVerificationHttpError(status_code)
        try:
            payload = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AsrVerificationProtocolError(
                "ASR 验证接口未返回 JSON。"
            ) from exc
        if not isinstance(payload, dict):
            raise AsrVerificationProtocolError(
                "ASR 验证响应的顶层必须是 JSON 对象。"
            )
        return payload

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = None
            if (
                declared_size is not None
                and declared_size > self.MAX_RESPONSE_BYTES
            ):
                raise AsrVerificationProtocolError(
                    "ASR 验证响应超过大小限制。"
                )
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > self.MAX_RESPONSE_BYTES:
                raise AsrVerificationProtocolError(
                    "ASR 验证响应超过大小限制。"
                )
            body.extend(chunk)
        return bytes(body)

    def _parse_payload(
        self,
        payload: dict[str, Any],
        *,
        duration_s: float | None,
        language_hint: str | None,
    ) -> AsrVerificationResult:
        raw_text, raw_segments = self._validated_transcript_fields(payload)
        (
            segments,
            no_speech_probabilities,
            confidence_values,
            invalid_segments,
        ) = self._parse_segments(
            raw_segments,
            duration_s=duration_s,
            language_hint=language_hint,
        )
        self._validate_parsed_segments(
            raw_text=raw_text,
            raw_segments=raw_segments,
            segments=segments,
            invalid_segments=invalid_segments,
        )
        vocals_detected, vocal_confidence = self._vocal_presence(
            segments,
            no_speech_probabilities,
            top_level_no_speech=self._top_level_no_speech_probability(payload),
        )
        response_duration = self._finite_number(payload.get("duration"))
        observed_duration = (
            duration_s if duration_s is not None else response_duration
        )
        transcript_confidence = (
            sum(confidence_values) / len(confidence_values)
            if segments and len(confidence_values) == len(segments)
            else None
        )
        return AsrVerificationResult(
            model=self.model or self.source,
            segments=segments,
            segments_received=len(raw_segments),
            segments_invalid=invalid_segments,
            duration_s=observed_duration,
            vocals_detected=vocals_detected,
            vocal_confidence=vocal_confidence,
            transcript_confidence=transcript_confidence,
            evidence=[
                Evidence(
                    id="asr.verifier.response",
                    source=self.source,
                    kind=EvidenceType.OBSERVED,
                    text=(
                        f"二次 ASR 已返回 {len(segments)} 个带时间戳片段。"
                        if segments
                        else "二次 ASR 已成功解析，未返回转写片段。"
                    ),
                    confidence=transcript_confidence,
                    metadata={
                        "segments": len(segments),
                        "duration_s": observed_duration,
                        "reported_duration_s": response_duration,
                        "vocals_detected": vocals_detected,
                        "vocal_confidence": vocal_confidence,
                        "invalid_segments": invalid_segments,
                    },
                )
            ],
        )

    def _validated_transcript_fields(
        self,
        payload: dict[str, Any],
    ) -> tuple[str, list[Any]]:
        raw_text_value = payload.get("text")
        if "text" not in payload or not isinstance(raw_text_value, str):
            raise AsrVerificationProtocolError(
                "ASR 验证响应缺少字符串 text 字段。"
            )
        raw_segments = payload.get("segments")
        if "segments" not in payload or not isinstance(raw_segments, list):
            raise AsrVerificationProtocolError(
                "ASR 验证响应中的 segments 必须是数组。"
            )
        if len(raw_segments) > self.MAX_SEGMENTS:
            raise AsrVerificationProtocolError(
                f"ASR 验证响应包含过多时间片段（>{self.MAX_SEGMENTS}）。"
            )
        return raw_text_value.strip(), raw_segments

    def _parse_segments(
        self,
        raw_segments: list[Any],
        *,
        duration_s: float | None,
        language_hint: str | None,
    ) -> tuple[list[LyricsSegment], list[float], list[float], int]:
        segments: list[LyricsSegment] = []
        no_speech_probabilities: list[float] = []
        confidence_values: list[float] = []
        invalid_segments = 0
        for item in raw_segments:
            parsed = self._parse_segment(
                item,
                duration_s=duration_s,
                language_hint=language_hint,
            )
            if parsed is None:
                invalid_segments += 1
                continue
            segment, no_speech_probability, segment_confidence = parsed
            if no_speech_probability is not None:
                no_speech_probabilities.append(no_speech_probability)
            if segment_confidence is not None:
                confidence_values.append(segment_confidence)
            segments.append(segment)
        return (
            segments,
            no_speech_probabilities,
            confidence_values,
            invalid_segments,
        )

    def _parse_segment(
        self,
        item: Any,
        *,
        duration_s: float | None,
        language_hint: str | None,
    ) -> ParsedSegment | None:
        if not isinstance(item, dict):
            return None
        text_value = item.get("text")
        text = text_value.strip() if isinstance(text_value, str) else ""
        if not text:
            return None
        start = self._finite_number(
            item.get("start", item.get("start_s", item.get("start_time")))
        )
        end = self._finite_number(
            item.get("end", item.get("end_s", item.get("end_time")))
        )
        if (
            start is None
            or end is None
            or start < 0
            or end <= start
            or (duration_s is not None and start > duration_s + 1.0)
        ):
            return None
        if duration_s is not None:
            end = min(end, duration_s)
        if end <= start:
            return None
        no_speech_probability = self._probability(item.get("no_speech_prob"))
        segment_confidence = self._segment_confidence(
            item,
            no_speech_probability=no_speech_probability,
        )
        segment = LyricsSegment(
            text=text[: self.MAX_SEGMENT_TEXT],
            span=TimeSpan(start_s=start, end_s=end),
            language=(
                str(item.get("language")).strip()
                if item.get("language")
                else language_hint
            ),
            confidence=segment_confidence,
        )
        return segment, no_speech_probability, segment_confidence

    @staticmethod
    def _validate_parsed_segments(
        *,
        raw_text: str,
        raw_segments: list[Any],
        segments: list[LyricsSegment],
        invalid_segments: int,
    ) -> None:
        if raw_text and not segments:
            raise AsrVerificationProtocolError(
                "ASR 返回了文本但没有可验证的时间片段。"
            )
        if raw_segments and invalid_segments == len(raw_segments) and not raw_text:
            raise AsrVerificationProtocolError(
                "ASR 返回的时间片段全部无效。"
            )

    @staticmethod
    def _vocal_presence(
        segments: list[LyricsSegment],
        no_speech_probabilities: list[float],
        *,
        top_level_no_speech: float | None,
    ) -> tuple[bool | None, float | None]:
        vocal_confidence: float | None = None
        if segments:
            if (
                len(no_speech_probabilities) == len(segments)
                and min(no_speech_probabilities) >= 0.8
            ):
                vocals_detected: bool | None = False
                vocal_confidence = (
                    sum(no_speech_probabilities)
                    / len(no_speech_probabilities)
                )
            elif not no_speech_probabilities or min(no_speech_probabilities) <= 0.6:
                vocals_detected = True
                if len(no_speech_probabilities) == len(segments):
                    vocal_confidence = (
                        sum(1.0 - value for value in no_speech_probabilities)
                        / len(no_speech_probabilities)
                    )
            else:
                vocals_detected = None
        else:
            if top_level_no_speech is not None and top_level_no_speech >= 0.8:
                vocals_detected = False
                vocal_confidence = top_level_no_speech
            else:
                vocals_detected = None
        return vocals_detected, vocal_confidence

    @classmethod
    def _segment_confidence(
        cls,
        item: dict[str, Any],
        *,
        no_speech_probability: float | None,
    ) -> float | None:
        for field in ("confidence", "probability"):
            value = cls._probability(item.get(field))
            if value is not None:
                return value
        average_log_probability = cls._finite_number(
            item.get("avg_logprob", item.get("avg_log_probability"))
        )
        # Some compatible servers emit 0 as a placeholder. Treat only a
        # genuinely negative log-probability as a confidence signal.
        if average_log_probability is None or average_log_probability >= 0:
            return None
        score = min(1.0, max(0.0, math.exp(average_log_probability)))
        if no_speech_probability is not None:
            score *= 1.0 - no_speech_probability
        return score

    @staticmethod
    def _finite_number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        result = float(value)
        return result if math.isfinite(result) else None

    @classmethod
    def _probability(cls, value: Any) -> float | None:
        result = cls._finite_number(value)
        if result is None or not 0 <= result <= 1:
            return None
        return result

    @classmethod
    def _top_level_no_speech_probability(
        cls,
        payload: dict[str, Any],
    ) -> float | None:
        for field in ("no_speech_prob", "no_speech_probability"):
            value = cls._probability(payload.get(field))
            if value is not None:
                return value
        vad_result = payload.get("vad")
        if not isinstance(vad_result, dict):
            return None
        speech_detected = vad_result.get("speech_detected")
        confidence = cls._probability(vad_result.get("confidence"))
        if speech_detected is False and confidence is not None:
            return confidence
        return None

    @staticmethod
    def _wav_duration(path: Path) -> float | None:
        try:
            with wave.open(str(path), "rb") as source:
                frame_rate = source.getframerate()
                if frame_rate <= 0:
                    return None
                return source.getnframes() / frame_rate
        except (OSError, EOFError, wave.Error):
            return None
