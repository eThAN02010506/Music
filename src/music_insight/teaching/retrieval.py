from __future__ import annotations

from music_insight.schemas import AnalysisResult, Evidence, EvidenceType
from music_insight.teaching.grounding import analysis_source_catalog
from music_insight.teaching.models import (
    LyricsContext,
    MusicUnderstandingMap,
    SectionMarker,
    TeachingTimeSpan,
    UnderstandingEvent,
)


def focus_span(
    *,
    current_time_s: float,
    duration_s: float,
    selected_range: TeachingTimeSpan | None,
    radius_s: float = 15.0,
) -> TeachingTimeSpan:
    if selected_range is not None:
        return selected_range
    start_s = max(0.0, min(current_time_s, duration_s) - radius_s)
    end_s = min(duration_s, max(0.0, current_time_s) + radius_s)
    if end_s <= start_s:
        start_s = max(0.0, duration_s - min(radius_s * 2, duration_s))
        end_s = duration_s
    return TeachingTimeSpan(start_s=start_s, end_s=end_s)


def section_at_time(
    understanding_map: MusicUnderstandingMap,
    time_s: float,
) -> SectionMarker | None:
    if not understanding_map.sections:
        return None
    containing = [
        section
        for section in understanding_map.sections
        if section.span.start_s <= time_s <= section.span.end_s
    ]
    if containing:
        return min(containing, key=lambda item: item.span.end_s - item.span.start_s)
    return min(
        understanding_map.sections,
        key=lambda item: _distance_to_span(time_s, item.span),
    )


def nearby_events(
    understanding_map: MusicUnderstandingMap,
    *,
    target: TeachingTimeSpan,
    comparison_ranges: list[TeachingTimeSpan] | None = None,
    limit: int = 8,
    max_distance_s: float = 30.0,
) -> list[UnderstandingEvent]:
    targets = [target, *(comparison_ranges or [])]
    ranked = sorted(
        understanding_map.events,
        key=lambda event: (
            0 if any(event.span.overlaps(span) for span in targets) else 1,
            min(_span_distance(event.span, span) for span in targets),
            event.start_s,
        ),
    )
    selected = [
        event
        for event in ranked
        if any(event.span.overlaps(span) for span in targets)
        or min(_span_distance(event.span, span) for span in targets)
        <= max_distance_s
    ][: max(1, min(limit, 12))]
    if not selected and ranked:
        selected = ranked[:1]
    return sorted(selected, key=lambda event: event.start_s)


def nearby_lyrics(
    result: AnalysisResult,
    *,
    targets: list[TeachingTimeSpan],
    limit: int = 16,
) -> list[LyricsContext]:
    ranked: list[tuple[float, int, LyricsContext]] = []
    for index, lyric in enumerate(result.lyrics):
        if lyric.span is None or lyric.span.end_s <= lyric.span.start_s:
            continue
        span = TeachingTimeSpan(
            start_s=lyric.span.start_s,
            end_s=lyric.span.end_s,
        )
        distance = min(_span_distance(span, target) for target in targets)
        if distance > 15:
            continue
        ranked.append(
            (
                distance,
                index,
                LyricsContext(
                    source_id=f"lyrics:{index}",
                    text=lyric.text,
                    span=span,
                    language=lyric.language,
                    confidence=lyric.confidence,
                ),
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1]))
    chosen = ranked[: max(1, min(limit, 20))]
    return sorted(
        (item[2] for item in chosen),
        key=lambda lyric: lyric.span.start_s if lyric.span else float("inf"),
    )


def nearby_analysis_evidence(
    result: AnalysisResult,
    *,
    targets: list[TeachingTimeSpan],
    limit: int = 24,
) -> list[Evidence]:
    bounded_limit = max(1, min(limit, 30))
    catalog = analysis_source_catalog(result)
    ranked: list[tuple[float, str, Evidence]] = []
    for source_id, fact in catalog.items():
        if fact.evidence is None or fact.span is None:
            continue
        distance = min(_span_distance(fact.span, target) for target in targets)
        if distance > 20:
            continue
        enriched = fact.evidence.model_copy(
            update={
                "text": fact.statement,
                "metadata": {
                    **fact.evidence.metadata,
                    "teaching_source_id": source_id,
                    "teaching_dimension": fact.dimension.value,
                }
            }
        )
        ranked.append((distance, source_id, enriched))
    ranked.sort(key=lambda item: (item[0], item[1]))
    global_metrics: list[Evidence] = []
    for source_id in ("technical_metrics.bpm", "technical_metrics.key"):
        fact = catalog.get(source_id)
        if fact is None:
            continue
        global_metrics.append(
            Evidence(
                id=f"teaching.{source_id}",
                source="music-insight-dsp",
                kind=EvidenceType.COMPUTED,
                text=fact.statement,
                confidence=fact.confidence,
                metadata={
                    "teaching_source_id": source_id,
                    "teaching_dimension": fact.dimension.value,
                },
            )
        )
    reserved_metrics = global_metrics[:bounded_limit]
    local_limit = max(0, bounded_limit - len(reserved_metrics))
    return [
        *(item[2] for item in ranked[:local_limit]),
        *reserved_metrics,
    ]


def _distance_to_span(time_s: float, span: TeachingTimeSpan) -> float:
    if span.start_s <= time_s <= span.end_s:
        return 0.0
    return min(abs(time_s - span.start_s), abs(time_s - span.end_s))


def _span_distance(left: TeachingTimeSpan, right: TeachingTimeSpan) -> float:
    if left.overlaps(right):
        return 0.0
    return min(
        abs(left.end_s - right.start_s),
        abs(right.end_s - left.start_s),
    )
