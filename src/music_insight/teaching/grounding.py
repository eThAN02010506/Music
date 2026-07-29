from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from music_insight.schemas import AnalysisResult, Evidence, EvidenceType
from music_insight.teaching.models import (
    AudioDimension,
    EvidenceClaimType,
    EvidenceSourceType,
    MusicUnderstandingMap,
    TeachingChatContext,
    TeachingChatResponse,
    TeachingTimeSpan,
)


class GroundingError(ValueError):
    """Raised when generated teaching content cannot be traced to its source."""

    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("；".join(issues[:8]))


@dataclass(frozen=True, slots=True)
class SourceFact:
    source_id: str
    source_type: EvidenceSourceType
    statement: str
    dimension: AudioDimension
    span: TeachingTimeSpan | None
    confidence: float | None
    claim_type: EvidenceClaimType
    evidence: Evidence | None = None


def analysis_result_hash(result: AnalysisResult) -> str:
    """Return a stable hash so teaching maps invalidate after any revision."""

    canonical = json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def analysis_source_catalog(result: AnalysisResult) -> dict[str, SourceFact]:
    catalog: dict[str, SourceFact] = {}
    seen_evidence: set[tuple[object, ...]] = set()
    containers = (
        ("sound_events", result.sound_events, AudioDimension.INSTRUMENTATION),
        ("emotion_timeline", result.emotion_timeline, AudioDimension.DYNAMICS),
        (
            "inferred_atmosphere",
            result.inferred_atmosphere,
            AudioDimension.OTHER,
        ),
        (
            "technical_metrics.energy_curve",
            result.technical_metrics.energy_curve,
            AudioDimension.DYNAMICS,
        ),
        (
            "technical_metrics.evidence",
            result.technical_metrics.evidence,
            AudioDimension.OTHER,
        ),
        ("evidence", result.evidence, AudioDimension.OTHER),
    )
    for prefix, values, default_dimension in containers:
        for index, evidence in enumerate(values):
            if _is_diagnostic_failure(evidence):
                continue
            fingerprint = _evidence_fingerprint(evidence)
            if fingerprint in seen_evidence:
                continue
            seen_evidence.add(fingerprint)
            source_id = f"{prefix}:{index}"
            catalog[source_id] = SourceFact(
                source_id=source_id,
                source_type=EvidenceSourceType.ANALYSIS_EVIDENCE,
                statement=_canonical_evidence_statement(evidence.text),
                dimension=_infer_dimension(evidence.text, default_dimension),
                span=_teaching_span(evidence),
                confidence=evidence.confidence,
                claim_type=_claim_type(evidence.kind),
                evidence=evidence,
            )
    for index, lyric in enumerate(result.lyrics):
        source_id = f"lyrics:{index}"
        catalog[source_id] = SourceFact(
            source_id=source_id,
            source_type=EvidenceSourceType.LYRICS,
            statement=lyric.text,
            dimension=AudioDimension.LYRICS,
            span=(
                TeachingTimeSpan(
                    start_s=lyric.span.start_s,
                    end_s=lyric.span.end_s,
                )
                if lyric.span and lyric.span.end_s > lyric.span.start_s
                else None
            ),
            confidence=lyric.confidence,
            claim_type=EvidenceClaimType.OBSERVED_FACT,
        )
    if result.technical_metrics.bpm is not None:
        catalog["technical_metrics.bpm"] = SourceFact(
            source_id="technical_metrics.bpm",
            source_type=EvidenceSourceType.METRIC,
            statement=f"{result.technical_metrics.bpm:g} BPM",
            dimension=AudioDimension.RHYTHM,
            span=None,
            confidence=result.technical_metrics.bpm_confidence,
            claim_type=EvidenceClaimType.COMPUTED_FACT,
        )
    if result.technical_metrics.key:
        catalog["technical_metrics.key"] = SourceFact(
            source_id="technical_metrics.key",
            source_type=EvidenceSourceType.METRIC,
            statement=result.technical_metrics.key,
            dimension=AudioDimension.HARMONY,
            span=None,
            confidence=result.technical_metrics.key_confidence,
            claim_type=EvidenceClaimType.COMPUTED_FACT,
        )
    return catalog


def _is_diagnostic_failure(evidence: Evidence) -> bool:
    """Keep pipeline failures out of the audible-fact evidence catalog."""

    evidence_id = evidence.id.casefold()
    return (
        not evidence.text.strip()
        or bool(evidence.metadata.get("error_type"))
        or evidence_id.endswith(".error")
        or ".error." in evidence_id
    )


def _canonical_evidence_statement(text: str) -> str:
    """Bound the immutable fact exactly as it is shown to a teaching model."""

    return text.strip()[:800]


def _evidence_fingerprint(evidence: Evidence) -> tuple[object, ...]:
    span = evidence.span
    return (
        " ".join(evidence.source.casefold().split()),
        evidence.kind.value,
        " ".join(evidence.text.casefold().split()),
        round(span.start_s, 6) if span is not None else None,
        round(span.end_s, 6) if span is not None else None,
    )


def validate_understanding_map(
    understanding_map: MusicUnderstandingMap,
    *,
    result: AnalysisResult,
    duration_s: float,
) -> None:
    catalog = analysis_source_catalog(result)
    issues: list[str] = []
    for section in understanding_map.sections:
        _check_duration(section.span, duration_s, f"section {section.id}", issues)
    for point_index, point in enumerate(understanding_map.emotional_arc):
        _check_duration(
            point.span,
            duration_s,
            f"emotional_arc[{point_index}]",
            issues,
        )
        if not point.evidence_refs:
            issues.append(f"emotional_arc[{point_index}] has no source evidence")
        for reference in point.evidence_refs:
            _check_analysis_reference(
                reference.source_id,
                reference.source_type,
                reference.statement,
                reference.claim_type,
                reference.span,
                catalog,
                point.span,
                f"emotional_arc[{point_index}]",
                issues,
            )
    for event in understanding_map.events:
        event_span = event.span
        _check_duration(event_span, duration_s, f"event {event.id}", issues)
        timed_support = False
        direct_timed_support = False
        for reference in event.audio_evidence:
            fact = _check_analysis_reference(
                reference.source_id,
                reference.source_type,
                reference.statement,
                reference.claim_type,
                reference.span,
                catalog,
                event_span,
                f"event {event.id}",
                issues,
            )
            if fact is not None and fact.span is not None:
                timed_support = True
                if fact.claim_type in {
                    EvidenceClaimType.OBSERVED_FACT,
                    EvidenceClaimType.COMPUTED_FACT,
                }:
                    direct_timed_support = True
        if not timed_support:
            issues.append(f"event {event.id} has no time-bounded source")
        elif not direct_timed_support:
            issues.append(
                f"event {event.id} has no directly observed or computed source"
            )
        for lyric in event.lyrics_context:
            fact = catalog.get(lyric.source_id)
            if fact is None or fact.source_type != EvidenceSourceType.LYRICS:
                issues.append(
                    f"event {event.id} references unknown lyric {lyric.source_id}"
                )
                continue
            if lyric.text != fact.statement:
                issues.append(
                    f"event {event.id} changes sourced lyric {lyric.source_id}"
                )
            if fact.span is not None and not fact.span.overlaps(event_span, tolerance=0.5):
                issues.append(
                    f"event {event.id} lyric {lyric.source_id} is outside its range"
                )
    event_by_id = {event.id: event for event in understanding_map.events}
    for moment in understanding_map.key_moments:
        moment_span = TeachingTimeSpan(
            start_s=moment.start_s,
            end_s=moment.end_s,
        )
        _check_duration(moment_span, duration_s, f"key moment {moment.id}", issues)
        event = event_by_id.get(moment.event_id)
        if event is not None and not moment_span.overlaps(event.span, tolerance=0.5):
            issues.append(
                f"key moment {moment.id} does not overlap event {moment.event_id}"
            )
    has_map_support = any(
        point.evidence_refs for point in understanding_map.emotional_arc
    ) or any(event.audio_evidence for event in understanding_map.events)
    if not has_map_support and understanding_map.confidence > 0.4:
        issues.append(
            "high-confidence map overview has no traceable event or arc evidence"
        )
    if issues:
        raise GroundingError(issues)


def validate_chat_response(
    response: TeachingChatResponse,
    *,
    context: TeachingChatContext,
) -> None:
    analysis_sources = analysis_source_catalog_from_context(context)
    sources: dict[str, SourceFact] = dict(analysis_sources)
    sources.update(
        {
            f"understanding_event:{event.id}": SourceFact(
                source_id=f"understanding_event:{event.id}",
                source_type=EvidenceSourceType.UNDERSTANDING_EVENT,
                statement=event.observation,
                dimension=(
                    event.audio_evidence[0].dimension
                    if event.audio_evidence
                    else AudioDimension.OTHER
                ),
                span=event.span,
                confidence=event.confidence,
                claim_type=EvidenceClaimType.GROUNDED_INTERPRETATION,
            )
            for event in context.nearby_events
        }
    )
    sources.update(
        {
            evidence.id: SourceFact(
                source_id=evidence.id,
                source_type=EvidenceSourceType.RELISTEN,
                statement=evidence.observation,
                dimension=evidence.dimension,
                span=evidence.span,
                confidence=evidence.confidence,
                claim_type=EvidenceClaimType.OBSERVED_FACT,
            )
            for evidence in context.relisten_evidence
        }
    )
    ranges = {item.id: item.span for item in response.time_ranges}
    issues: list[str] = []
    for range_id, span in ranges.items():
        _check_duration(span, context.duration_s, f"time range {range_id}", issues)
    if not response.insufficient_evidence and not response.evidence:
        issues.append("normal answer has no evidence")
    if response.insufficient_evidence and response.confidence > 0.4:
        issues.append("insufficient-evidence answer confidence exceeds 0.4")
    for evidence in response.evidence:
        referenced_ranges = [
            ranges[range_id]
            for range_id in evidence.time_range_ids
            if range_id in ranges
        ]
        for source_id in evidence.source_refs:
            fact = sources.get(source_id)
            if fact is None:
                issues.append(
                    f"answer evidence {evidence.id} uses unknown source {source_id}"
                )
                continue
            source_span = fact.span
            source_claim_type = fact.claim_type
            if (
                evidence.claim_type
                in {
                    EvidenceClaimType.OBSERVED_FACT,
                    EvidenceClaimType.COMPUTED_FACT,
                }
                and source_claim_type != evidence.claim_type
            ):
                issues.append(
                    f"answer evidence {evidence.id} changes the epistemic "
                    f"type of {source_id}"
                )
            if evidence.claim_type in {
                EvidenceClaimType.OBSERVED_FACT,
                EvidenceClaimType.COMPUTED_FACT,
            }:
                if len(evidence.source_refs) != 1:
                    issues.append(
                        f"answer evidence {evidence.id} combines direct facts; "
                        "each observed/computed fact must use one source"
                    )
                if evidence.statement.strip() != fact.statement.strip():
                    issues.append(
                        f"answer evidence {evidence.id} changes the sourced "
                        f"statement for {source_id}"
                    )
                if evidence.dimension != fact.dimension:
                    issues.append(
                        f"answer evidence {evidence.id} changes the source "
                        f"dimension for {source_id}"
                    )
            if source_span is not None and not any(
                source_span.overlaps(span, tolerance=0.75)
                for span in referenced_ranges
            ):
                issues.append(
                    f"answer evidence {evidence.id} source {source_id} "
                    "does not overlap its cited time"
                )
    if response.relistened and not context.relisten_evidence:
        issues.append("answer claims a relisten without relisten evidence")
    if any(
        source_id.startswith("relisten:")
        for evidence in response.evidence
        for source_id in evidence.source_refs
    ) and not response.relistened:
        issues.append("answer cites relisten evidence but relistened is false")
    if issues:
        raise GroundingError(issues)


def analysis_source_catalog_from_context(
    context: TeachingChatContext,
) -> dict[str, SourceFact]:
    """Build the subset catalog supplied to a chat model.

    Chat deliberately has no complete ``AnalysisResult``.  The service passes
    nearby evidence only, so a provider cannot silently reinterpret the whole
    song for every question.
    """

    catalog: dict[str, SourceFact] = {}
    for index, evidence in enumerate(context.nearby_analysis_evidence):
        source_id = str(evidence.metadata.get("teaching_source_id") or "")
        if not source_id:
            source_id = f"context_evidence:{index}"
        catalog[source_id] = SourceFact(
            source_id=source_id,
            source_type=EvidenceSourceType.ANALYSIS_EVIDENCE,
            statement=_canonical_evidence_statement(evidence.text),
            dimension=_context_evidence_dimension(evidence),
            span=_teaching_span(evidence),
            confidence=evidence.confidence,
            claim_type=_claim_type(evidence.kind),
            evidence=evidence,
        )
    for lyric in context.nearby_lyrics:
        catalog[lyric.source_id] = SourceFact(
            source_id=lyric.source_id,
            source_type=EvidenceSourceType.LYRICS,
            statement=lyric.text,
            dimension=AudioDimension.LYRICS,
            span=lyric.span,
            confidence=lyric.confidence,
            claim_type=EvidenceClaimType.OBSERVED_FACT,
        )
    return catalog


def _context_evidence_dimension(evidence: Evidence) -> AudioDimension:
    raw_dimension = evidence.metadata.get("teaching_dimension")
    if isinstance(raw_dimension, str):
        try:
            return AudioDimension(raw_dimension)
        except ValueError:
            pass
    return _infer_dimension(evidence.text, AudioDimension.OTHER)


def _check_analysis_reference(
    source_id: str,
    source_type: EvidenceSourceType,
    reference_statement: str,
    reference_claim_type: EvidenceClaimType,
    reference_span: TeachingTimeSpan | None,
    catalog: dict[str, SourceFact],
    container_span: TeachingTimeSpan,
    label: str,
    issues: list[str],
) -> SourceFact | None:
    if source_type not in {
        EvidenceSourceType.ANALYSIS_EVIDENCE,
        EvidenceSourceType.LYRICS,
        EvidenceSourceType.METRIC,
    }:
        issues.append(f"{label} uses unsupported map source type {source_type}")
        return None
    fact = catalog.get(source_id)
    if fact is None or fact.source_type != source_type:
        issues.append(f"{label} references unknown source {source_id}")
        return None
    if reference_statement.strip() != fact.statement.strip():
        issues.append(f"{label} changes the sourced statement for {source_id}")
    if reference_claim_type != fact.claim_type:
        issues.append(f"{label} changes the epistemic type for {source_id}")
    if reference_span is not None and not reference_span.overlaps(
        container_span,
        tolerance=0.5,
    ):
        issues.append(f"{label} reference {source_id} is outside its range")
    if fact.span is not None:
        if reference_span is None:
            issues.append(f"{label} omits the source time for {source_id}")
        effective_span = reference_span or fact.span
        if not fact.span.overlaps(effective_span, tolerance=0.5):
            issues.append(f"{label} changes the source time for {source_id}")
        if not fact.span.overlaps(container_span, tolerance=0.5):
            issues.append(f"{label} source {source_id} is outside its range")
    return fact


def _check_duration(
    span: TeachingTimeSpan,
    duration_s: float,
    label: str,
    issues: list[str],
) -> None:
    if span.end_s > duration_s + 0.5:
        issues.append(f"{label} exceeds the audio duration")


def _teaching_span(evidence: Evidence) -> TeachingTimeSpan | None:
    if evidence.span is None or evidence.span.end_s <= evidence.span.start_s:
        return None
    return TeachingTimeSpan(
        start_s=evidence.span.start_s,
        end_s=evidence.span.end_s,
    )


def _claim_type(kind: EvidenceType) -> EvidenceClaimType:
    if kind == EvidenceType.COMPUTED:
        return EvidenceClaimType.COMPUTED_FACT
    if kind == EvidenceType.OBSERVED:
        return EvidenceClaimType.OBSERVED_FACT
    if kind == EvidenceType.INTERPRETIVE:
        return EvidenceClaimType.POSSIBLE_READING
    return EvidenceClaimType.GROUNDED_INTERPRETATION


def _infer_dimension(
    text: str,
    default: AudioDimension,
) -> AudioDimension:
    folded = text.casefold()
    keywords = (
        (AudioDimension.RHYTHM, ("节拍", "节奏", "鼓", "bpm", "rhythm", "beat")),
        (AudioDimension.MELODY, ("旋律", "音高", "melody", "pitch")),
        (AudioDimension.HARMONY, ("和声", "和弦", "调性", "harmony", "chord", "key")),
        (AudioDimension.TIMBRE, ("音色", "timbre", "质感")),
        (AudioDimension.DYNAMICS, ("力度", "能量", "响度", "dynamic", "energy")),
        (AudioDimension.SPACE, ("空间", "混响", "声场", "reverb", "space")),
        (AudioDimension.LYRICS, ("歌词", "人声内容", "lyric")),
        (
            AudioDimension.INSTRUMENTATION,
            ("乐器", "吉他", "钢琴", "贝斯", "鼓组", "instrument"),
        ),
        (AudioDimension.STRUCTURE, ("主歌", "副歌", "桥段", "段落", "section")),
    )
    for dimension, candidates in keywords:
        if any(candidate in folded for candidate in candidates):
            return dimension
    return default
