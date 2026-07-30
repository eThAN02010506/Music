from __future__ import annotations

import re
from typing import Any

from music_insight.teaching.grounding import (
    SourceFact,
    analysis_source_catalog,
)
from music_insight.teaching.models import (
    AnalysisEvidenceRef,
    EmotionalArcPoint,
    KeyMoment,
    LyricsContext,
    MapGenerationContext,
    MusicUnderstandingMap,
    RelistenEvidence,
    RelistenRequest,
    RelistenResult,
    SectionMarker,
    TeachingChatResponse,
    TeachingTimeSpan,
    UnderstandingEvent,
)


def parse_understanding_map(
    payload: dict[str, Any],
    context: MapGenerationContext,
) -> MusicUnderstandingMap:
    """Expand model-selected source IDs into immutable local evidence facts."""

    catalog = analysis_source_catalog(context.result)
    lyrics = {
        f"lyrics:{index}": lyric
        for index, lyric in enumerate(context.result.lyrics)
    }
    emotional_arc = [
        EmotionalArcPoint(
            span=_span(item),
            description=item["description"],
            evidence_refs=[
                _reference(source_id, catalog)
                for source_id in _unique_strings(
                    item.get("evidence_source_ids")
                )
            ],
            confidence=item["confidence"],
        )
        for item in _dict_items(payload.get("emotional_arc"))
    ]
    sections = [
        SectionMarker(
            id=item["id"],
            label=item["label"],
            span=_span(item),
            expressive_role=item["expressive_role"],
            confidence=item["confidence"],
            alternative_labels=item.get("alternative_labels") or [],
        )
        for item in _dict_items(payload.get("sections"))
    ]
    events = [
        UnderstandingEvent(
            id=item["id"],
            start_s=item["start_s"],
            end_s=item["end_s"],
            section=item["section"],
            observation=item["observation"],
            interpretation=item["interpretation"],
            expressive_role=item["expressive_role"],
            audio_evidence=[
                _reference(source_id, catalog)
                for source_id in _unique_strings(
                    item.get("evidence_source_ids")
                )
            ],
            lyrics_context=[
                _lyrics_context(source_id, lyrics)
                for source_id in _unique_strings(
                    item.get("lyrics_source_ids")
                )
            ],
            listening_task=item["listening_task"],
            alternative_readings=item.get("alternative_readings") or [],
            confidence=item["confidence"],
        )
        for item in _dict_items(payload.get("events"))
    ]
    key_moments = [
        KeyMoment.model_validate(item)
        for item in _dict_items(payload.get("key_moments"))
    ]
    return MusicUnderstandingMap(
        output_language=context.output_language,
        core_expression=payload["core_expression"],
        overall_atmosphere=payload["overall_atmosphere"],
        emotional_arc=emotional_arc,
        sections=sections,
        events=events,
        key_moments=key_moments,
        confidence=payload["confidence"],
        warnings=payload.get("warnings") or [],
    )


def parse_teaching_chat_response(
    payload: dict[str, Any],
    *,
    output_language: str = "zh",
) -> TeachingChatResponse:
    return TeachingChatResponse.model_validate(
        {**payload, "output_language": output_language}
    )


def parse_relisten_result(
    payload: dict[str, Any],
    *,
    request: RelistenRequest,
    spans: list[TeachingTimeSpan],
) -> RelistenResult:
    safe_analysis_id = re.sub(
        r"[^A-Za-z0-9_.:-]+",
        "-",
        request.analysis_id,
    ).strip("-")[:80] or "analysis"
    evidence: list[RelistenEvidence] = []
    for index, item in enumerate(_dict_items(payload.get("evidence"))):
        range_index = int(item["range_index"])
        if not 0 <= range_index < len(spans):
            raise ValueError("模型返回了不存在的局部重听片段编号。")
        evidence.append(
            RelistenEvidence(
                id=f"relisten:{safe_analysis_id}:{range_index}:{index}",
                dimension=item["dimension"],
                observation=item["observation"],
                span=spans[range_index],
                confidence=item["confidence"],
            )
        )
    return RelistenResult(
        evidence=evidence,
        warnings=payload.get("warnings") or [],
    )


def _reference(
    source_id: str,
    catalog: dict[str, SourceFact],
) -> AnalysisEvidenceRef:
    fact = catalog.get(source_id)
    if fact is None:
        raise ValueError(f"模型引用了不存在的分析证据：{source_id[:160]}")
    return AnalysisEvidenceRef(
        source_type=fact.source_type,
        source_id=fact.source_id,
        dimension=fact.dimension,
        statement=fact.statement,
        claim_type=fact.claim_type,
        span=fact.span,
        confidence=fact.confidence,
    )


def _lyrics_context(source_id: str, lyrics: dict[str, Any]) -> LyricsContext:
    lyric = lyrics.get(source_id)
    if lyric is None:
        raise ValueError(f"模型引用了不存在的歌词：{source_id[:160]}")
    span = (
        TeachingTimeSpan(
            start_s=lyric.span.start_s,
            end_s=lyric.span.end_s,
        )
        if lyric.span is not None and lyric.span.end_s > lyric.span.start_s
        else None
    )
    return LyricsContext(
        source_id=source_id,
        text=lyric.text,
        span=span,
        language=lyric.language,
        confidence=lyric.confidence,
    )


def _span(item: dict[str, Any]) -> TeachingTimeSpan:
    return TeachingTimeSpan(
        start_s=item["start_s"],
        end_s=item["end_s"],
    )


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _unique_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item not in result:
            result.append(item)
    return result
