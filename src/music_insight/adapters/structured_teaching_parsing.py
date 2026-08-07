from __future__ import annotations

import re
from typing import Any

from music_insight.teaching.grounding import (
    SourceFact,
    analysis_source_catalog,
    chat_source_catalog,
)
from music_insight.teaching.models import (
    AnalysisEvidenceRef,
    AnswerEvidence,
    AnswerTimeRange,
    EmotionalArcPoint,
    KeyMoment,
    LyricsContext,
    MapGenerationContext,
    MusicUnderstandingMap,
    ListeningTask,
    PlayerAction,
    PlayerActionType,
    RelistenEvidence,
    RelistenRequest,
    RelistenResult,
    SectionMarker,
    TeachingChatContext,
    TeachingChatResponse,
    TeachingTimeSpan,
    UnderstandingEvent,
    chat_focus_spans,
    localized_text,
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
    context: TeachingChatContext,
) -> TeachingChatResponse:
    """Expand a compact semantic answer into deterministic player data."""

    catalog = chat_source_catalog(context)
    requested_ids = _unique_strings(payload.get("source_ids"))[:6]
    focus_spans = chat_focus_spans(context)
    invalid_ids = [
        source_id
        for source_id in requested_ids
        if source_id not in catalog
        or catalog[source_id].span is None
        or not any(
            catalog[source_id].span.start_s >= span.start_s - 0.5
            and catalog[source_id].span.end_s <= span.end_s + 0.5
            for span in focus_spans
            if catalog[source_id].span is not None
        )
    ]
    if invalid_ids:
        raise ValueError(
            "模型引用了当前复听范围之外的证据：" + "、".join(invalid_ids[:3])
        )
    selected_facts = [
        catalog[source_id]
        for source_id in requested_ids
    ]
    time_ranges = [
        AnswerTimeRange(
            id=f"range.source.{index}",
            start_s=fact.span.start_s,
            end_s=fact.span.end_s,
            label=_localized_text(
                context.output_language,
                f"Evidence {index + 1}",
                f"证据片段 {index + 1}",
            ),
            purpose=_localized_text(
                context.output_language,
                "Check the cited audible fact",
                "核对被引用的听觉事实",
            ),
        )
        for index, fact in enumerate(selected_facts)
        if fact.span is not None
    ]
    if not time_ranges:
        time_ranges = [_context_time_range(context)]

    evidence = [
        AnswerEvidence(
            id=f"answer.evidence.{index}",
            statement=fact.statement,
            claim_type=fact.claim_type,
            dimension=fact.dimension,
            source_refs=[fact.source_id],
            time_range_ids=[time_ranges[index].id],
            confidence=(
                fact.confidence
                if fact.confidence is not None
                else float(payload["confidence"])
            ),
        )
        for index, fact in enumerate(selected_facts)
    ]
    insufficient = bool(payload["insufficient_evidence"]) or not evidence
    confidence = float(payload["confidence"])
    if insufficient:
        confidence = min(confidence, 0.4)
    focus = evidence[0].dimension if evidence else "other"
    first_range = time_ranges[0]
    return TeachingChatResponse(
        output_language=context.output_language,
        answer=payload["answer"],
        time_ranges=time_ranges,
        evidence=evidence,
        listening_task=ListeningTask(
            instruction=_localized_text(
                context.output_language,
                "Loop this range and follow only the cited sound change.",
                "循环这一时间范围，只跟随被引用的声音变化。",
            ),
            focus=focus,
            time_range_id=first_range.id,
        ),
        suggested_questions=_string_list(payload.get("suggested_questions")),
        player_actions=[
            PlayerAction(
                type=PlayerActionType.LOOP_RANGE,
                time_range_id=first_range.id,
                label=_localized_text(
                    context.output_language,
                    "Loop cited evidence",
                    "循环引用证据",
                ),
            )
        ],
        alternative_readings=_string_list(payload.get("alternative_readings")),
        warnings=[],
        confidence=confidence,
        relistened=any(
            fact.source_id.startswith("relisten:") for fact in selected_facts
        ),
        insufficient_evidence=insufficient,
    )


def _context_time_range(context: TeachingChatContext) -> AnswerTimeRange:
    span = chat_focus_spans(context)[0]
    return AnswerTimeRange(
        id="range.context",
        start_s=span.start_s,
        end_s=span.end_s,
        label=_localized_text(
            context.output_language,
            "Current listening range",
            "当前复听范围",
        ),
        purpose=_localized_text(
            context.output_language,
            "Provide a playback reference for this answer",
            "为本次回答提供播放参照",
        ),
    )


def _localized_text(language: str, english: str, chinese: str) -> str:
    return localized_text(language, english, chinese)


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


def _string_list(value: Any) -> list[str]:
    """Normalize string-or-object items into plain strings.

    The wire schema accepts both a bare string and a small object (label /
    description / text) for suggested questions and alternative readings,
    because the model frequently emits objects there. Normalize those objects
    to their text content so the domain model still sees a string list.
    """

    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = (
                item.get("description")
                or item.get("text")
                or item.get("label")
                or ""
            )
        else:
            continue
        cleaned = str(text).strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result
