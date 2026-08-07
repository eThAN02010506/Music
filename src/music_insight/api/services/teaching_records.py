from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from music_insight.api.contracts.teaching import (
    TeachingChatRequest,
    TeachingConversation,
    TeachingGuideResponse,
    TeachingMessage,
)
from music_insight.api.teaching import TeachingDataError
from music_insight.schemas import AnalysisResult
from music_insight.teaching.models import (
    ConversationTurn,
    MusicUnderstandingMap,
    TeachingTimeSpan,
    localized_text,
)


TEACHING_SCHEMA_VERSION = 3


def guide_from_record(
    record: Mapping[str, Any],
    *,
    current_source_hash: str,
    cached: bool,
) -> TeachingGuideResponse:
    source_hash = record.get("source_result_hash")
    pending_hash = record.get("pending_source_result_hash")
    effective_hash = str(source_hash or pending_hash or current_source_hash)
    payload = record.get("map_payload")
    try:
        schema_version = int(record.get("schema_version") or 1)
        understanding_map = (
            MusicUnderstandingMap.model_validate(payload)
            if payload is not None
            else None
        )
        return TeachingGuideResponse(
            analysis_id=str(record["analysis_id"]),
            schema_version=schema_version,
            source_result_hash=effective_hash,
            status=str(record.get("status") or "failed"),
            understanding_map=understanding_map,
            stale=(
                source_hash != current_source_hash
                or schema_version != TEACHING_SCHEMA_VERSION
            ),
            cached=cached,
            error=record.get("last_error"),
            updated_at=datetime_or_none(record.get("updated_at")),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=500,
            detail="已保存的导赏地图格式无效。",
        ) from exc


def conversation_from_record(
    record: Mapping[str, Any],
) -> TeachingConversation:
    summary = record.get("summary")
    normalized_summary: str | None = None
    if isinstance(summary, Mapping):
        candidate = summary.get("text") or summary.get("summary")
        if isinstance(candidate, str):
            normalized_summary = candidate[:2000]
    elif isinstance(summary, str):
        normalized_summary = summary[:2000]
    try:
        return TeachingConversation(
            id=str(record["id"]),
            analysis_id=str(record["analysis_id"]),
            title=record.get("title"),
            summary=normalized_summary,
            message_count=int(record.get("message_count") or 0),
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise TeachingDataError("已保存的导赏对话格式无效。") from exc


def message_from_record(record: Mapping[str, Any]) -> TeachingMessage:
    try:
        return TeachingMessage(
            id=str(record["id"]),
            conversation_id=str(record["conversation_id"]),
            sequence=int(record["sequence"]),
            status=str(record["status"]),
            client_request_id=str(record["client_request_id"]),
            request=record.get("request_payload") or {},
            response=record.get("response_payload"),
            error=record.get("error"),
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise TeachingDataError("已保存的导赏消息格式无效。") from exc


def turns_from_records(records: list[dict[str, Any]]) -> list[ConversationTurn]:
    turns: list[ConversationTurn] = []
    for record in records:
        if record.get("status") != "complete":
            continue
        request = record.get("request_payload")
        response = record.get("response_payload")
        if not isinstance(request, Mapping) or not isinstance(response, Mapping):
            continue
        question = request.get("message")
        answer = response.get("answer")
        if not isinstance(question, str) or not isinstance(answer, str):
            continue
        try:
            turns.append(
                ConversationTurn(
                    question=question,
                    answer=answer,
                    created_at=record["created_at"],
                )
            )
        except (KeyError, ValidationError):
            continue
    return turns[-12:]


def validate_request_duration(
    payload: TeachingChatRequest,
    duration_s: float,
) -> None:
    if payload.current_time_s > duration_s + 0.5:
        raise HTTPException(status_code=422, detail="当前播放位置超过音频时长。")
    ranges = [*payload.compare_ranges]
    if payload.selected_range is not None:
        ranges.append(payload.selected_range)
    if any(span.end_s > duration_s + 0.5 for span in ranges):
        raise HTTPException(status_code=422, detail="选中的时间范围超过音频时长。")


def duration_for_entry(
    duration_s: float | None,
    result: AnalysisResult,
) -> float:
    if duration_s is not None and duration_s > 0:
        return duration_s
    endpoints = [
        item.span.end_s
        for item in [
            *result.lyrics,
            *result.sound_events,
            *result.emotion_timeline,
            *result.inferred_atmosphere,
            *result.technical_metrics.energy_curve,
            *result.evidence,
        ]
        if item.span is not None
    ]
    if endpoints:
        return max(endpoints)
    raise HTTPException(status_code=422, detail="缺少可用的音频时长。")


def bounded_clip(
    span: TeachingTimeSpan,
    *,
    duration_s: float,
) -> TeachingTimeSpan:
    if span.end_s - span.start_s <= 30:
        return span
    midpoint = (span.start_s + span.end_s) / 2
    start_s = max(0.0, midpoint - 15)
    end_s = min(duration_s, start_s + 30)
    start_s = max(0.0, end_s - 30)
    return TeachingTimeSpan(start_s=start_s, end_s=end_s)


def bounded_error(error: BaseException, limit: int) -> str:
    return (str(error).strip() or error.__class__.__name__)[:limit]


def localized(language: str, english: str, chinese: str) -> str:
    return localized_text(language, english, chinese)


def datetime_or_none(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise ValueError("invalid datetime")
