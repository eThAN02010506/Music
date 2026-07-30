from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from music_insight.api.contracts.teaching import TeachingChatRequest
from music_insight.schemas import (
    AnalysisResult,
    DspResult,
    Evidence,
    EvidenceType,
    LyricsSegment,
    TimeSpan,
    VocalPresenceResult,
    VocalPresenceStatus,
)
from music_insight.teaching.fallback import EvidenceTeachingModel
from music_insight.teaching.grounding import (
    GroundingError,
    analysis_source_catalog,
    validate_chat_response,
    validate_understanding_map,
)
from music_insight.teaching.models import (
    AnswerEvidence,
    AnswerTimeRange,
    AudioDimension,
    EmotionalArcPoint,
    EvidenceClaimType,
    ListeningTask,
    ListenerProfile,
    MapGenerationContext,
    TeachingChatResponse,
    TeachingChatContext,
    TeachingTimeSpan,
)
from music_insight.teaching.retrieval import (
    nearby_analysis_evidence,
    nearby_events,
    nearby_lyrics,
    section_at_time,
)


def _result() -> AnalysisResult:
    observed = Evidence(
        id="scene.piano",
        source="audio-model",
        kind=EvidenceType.OBSERVED,
        text="钢琴由稀疏单音变为连续和弦",
        confidence=0.88,
        span=TimeSpan(start_s=2, end_s=10),
    )
    emotion = Evidence(
        id="scene.emotion",
        source="audio-model",
        kind=EvidenceType.INFERRED,
        text="情绪从克制逐渐变得明亮",
        confidence=0.72,
        span=TimeSpan(start_s=8, end_s=18),
    )
    energy = Evidence(
        id="dsp.energy",
        source="librosa",
        kind=EvidenceType.COMPUTED,
        text="能量曲线在中段持续上升",
        confidence=0.91,
        span=TimeSpan(start_s=6, end_s=16),
    )
    return AnalysisResult(
        summary="歌曲以克制的开场逐渐走向明亮，但仍保留开放的解释空间。",
        lyrics=[
            LyricsSegment(
                text="I can see the light",
                span=TimeSpan(start_s=7, end_s=11),
                language="en",
                confidence=0.9,
            )
        ],
        instruments=["piano"],
        sound_events=[observed],
        emotion_timeline=[emotion],
        inferred_atmosphere=[],
        themes=["希望"],
        technical_metrics=DspResult(
            bpm=78,
            bpm_confidence=0.8,
            key="C major",
            key_confidence=0.7,
            energy_curve=[energy],
        ),
        evidence=[observed, emotion, energy],
    )


def _understanding_map():
    result = _result()
    context = MapGenerationContext(
        analysis_id="song-1",
        result=result,
        duration_s=20,
        listener_profile=ListenerProfile(),
    )
    return result, asyncio.run(
        EvidenceTeachingModel().build_understanding_map(context)
    )


def test_teaching_contracts_reject_unknown_fields_and_invalid_comparison():
    with pytest.raises(ValidationError):
        TeachingChatRequest.model_validate(
            {
                "client_request_id": "request-123",
                "message": "比较两段",
                "current_time_s": 4,
                "unexpected": True,
            }
        )
    with pytest.raises(ValidationError):
        TeachingChatRequest(
            client_request_id="request-123",
            message="比较两段",
            current_time_s=4,
            compare_ranges=[TeachingTimeSpan(start_s=1, end_s=2)],
        )


def test_fallback_map_is_structured_and_grounded_to_source_evidence():
    result, understanding_map = _understanding_map()

    validate_understanding_map(
        understanding_map,
        result=result,
        duration_s=20,
    )

    assert understanding_map.core_expression.startswith("歌曲以克制")
    assert understanding_map.events
    assert understanding_map.key_moments
    event = understanding_map.events[0]
    assert "钢琴" in event.observation
    assert "情绪" not in event.observation
    assert any(
        reference.source_id == "sound_events:0"
        for reference in event.audio_evidence
    )
    assert event.listening_task


def test_teaching_catalog_deduplicates_aggregate_evidence_and_ignores_errors():
    result = _result()
    failure = Evidence(
        id="omni.chunk.1.error",
        source="audio-model",
        kind=EvidenceType.OBSERVED,
        text="第 1 个音频分块分析失败：ReadError",
        confidence=0,
        span=TimeSpan(start_s=0, end_s=20),
        metadata={"error_type": "ReadError"},
    )
    placeholder = Evidence(
        id="omni.chunk.2.analysis",
        source="audio-model",
        kind=EvidenceType.INFERRED,
        text="已完成第 2 个音频分块分析。",
        confidence=0.7,
        span=TimeSpan(start_s=10, end_s=20),
    )
    inconclusive = Evidence(
        id="omni.chunk.2.recovery.inconclusive",
        source="audio-model",
        kind=EvidenceType.OBSERVED,
        text="定向重听仍无法确认可靠歌词或人声状态。",
        confidence=0,
        span=TimeSpan(start_s=10, end_s=20),
    )
    result = result.model_copy(
        update={
            "evidence": [
                *result.evidence,
                failure,
                placeholder,
                inconclusive,
            ]
        }
    )

    catalog = analysis_source_catalog(result)
    statements = [fact.statement for fact in catalog.values()]

    assert statements.count("钢琴由稀疏单音变为连续和弦") == 1
    assert statements.count("能量曲线在中段持续上升") == 1
    assert all("ReadError" not in statement for statement in statements)
    assert all("已完成第" not in statement for statement in statements)
    assert all("定向重听仍无法确认" not in statement for statement in statements)

    understanding_map = asyncio.run(
        EvidenceTeachingModel().build_understanding_map(
            MapGenerationContext(
                analysis_id="song-1",
                result=result,
                duration_s=20,
                listener_profile=ListenerProfile(),
            )
        )
    )
    references = [
        reference
        for event in understanding_map.events
        for reference in event.audio_evidence
    ]
    reference_keys = {
        (
            reference.statement,
            reference.span.start_s if reference.span is not None else None,
            reference.span.end_s if reference.span is not None else None,
            reference.claim_type,
        )
        for reference in references
    }
    assert len(references) == len(reference_keys)


def test_instrumental_fallback_uses_instrument_tasks_and_conservative_confidence():
    base = _result()
    instrumentation = Evidence(
        id="omni.chunk.1.instrumentation",
        source="audio-model",
        kind=EvidenceType.INFERRED,
        text="该分块识别到的乐器或声部：弦乐组、铜管组。",
        confidence=0.55,
        span=TimeSpan(start_s=0, end_s=20),
        metadata={"teaching_dimension": "instrumentation"},
    )
    instrumental = base.model_copy(
        update={
            "summary": "管弦乐织体逐步增厚，形成持续推进的听觉过程。",
            "lyrics": [],
            "sound_events": [instrumentation],
            "evidence": [instrumentation, *base.technical_metrics.energy_curve],
            "vocal_presence": VocalPresenceResult(
                status=VocalPresenceStatus.INSTRUMENTAL,
                confidence=0.9,
                reason="逐块音频分析一致报告无人声。",
                evidence_ids=["omni.vocal_presence"],
            ),
        }
    )
    understanding_map = asyncio.run(
        EvidenceTeachingModel().build_understanding_map(
            MapGenerationContext(
                analysis_id="instrumental-1",
                result=instrumental,
                duration_s=20,
                listener_profile=ListenerProfile(),
            )
        )
    )

    validate_understanding_map(
        understanding_map,
        result=instrumental,
        duration_s=20,
    )
    assert understanding_map.confidence <= 0.65
    assert all(not event.lyrics_context for event in understanding_map.events)
    assert all(
        "歌词" not in event.listening_task
        for event in understanding_map.events
    )
    assert any(
        event.audio_evidence[0].dimension
        is AudioDimension.INSTRUMENTATION
        or event.listening_task.startswith("每次只追踪一种乐器")
        for event in understanding_map.events
    )

    invalid_event = understanding_map.events[0].model_copy(
        update={"listening_task": "先只听歌词重音，再比较伴奏。"}
    )
    invalid_map = understanding_map.model_copy(
        update={"events": [invalid_event, *understanding_map.events[1:]]}
    )
    with pytest.raises(GroundingError, match="vocal listening task"):
        validate_understanding_map(
            invalid_map,
            result=instrumental,
            duration_s=20,
        )


def test_long_evidence_uses_one_bounded_canonical_statement_everywhere():
    result = _result()
    long_fact = result.sound_events[0].model_copy(
        update={"text": "钢" * 900}
    )
    result = result.model_copy(
        update={
            "sound_events": [long_fact],
            "evidence": [long_fact, *result.evidence[1:]],
        }
    )

    catalog = analysis_source_catalog(result)
    nearby = nearby_analysis_evidence(
        result,
        targets=[TeachingTimeSpan(start_s=2, end_s=10)],
    )
    nearby_by_id = {
        item.metadata.get("teaching_source_id"): item
        for item in nearby
    }

    assert len(catalog["sound_events:0"].statement) == 800
    assert nearby_by_id["sound_events:0"].text == (
        catalog["sound_events:0"].statement
    )


def test_map_grounding_rejects_unknown_source_and_out_of_duration():
    result, understanding_map = _understanding_map()
    event = understanding_map.events[0]
    invalid_reference = event.audio_evidence[0].model_copy(
        update={"source_id": "sound_events:999"}
    )
    invalid_event = event.model_copy(
        update={
            "end_s": 24,
            "audio_evidence": [invalid_reference, *event.audio_evidence[1:]],
        }
    )
    invalid_map = understanding_map.model_copy(update={"events": [invalid_event]})

    with pytest.raises(GroundingError) as error:
        validate_understanding_map(
            invalid_map,
            result=result,
            duration_s=20,
        )

    assert "unknown source" in str(error.value)
    assert "duration" in str(error.value)


def test_emotional_arc_requires_at_least_one_source_reference():
    with pytest.raises(ValidationError, match="evidence_refs"):
        EmotionalArcPoint(
            span=TeachingTimeSpan(start_s=0, end_s=10),
            description="没有来源的确定情绪判断",
            evidence_refs=[],
            confidence=0.9,
        )


def test_time_retrieval_prefers_overlap_and_keeps_exact_lyrics():
    result, understanding_map = _understanding_map()
    target = TeachingTimeSpan(start_s=7, end_s=12)

    events = nearby_events(understanding_map, target=target)
    lyrics = nearby_lyrics(result, targets=[target])
    section = section_at_time(understanding_map, 9)

    assert events
    assert events[0].span.overlaps(target)
    assert [item.text for item in lyrics] == ["I can see the light"]
    assert lyrics[0].source_id == "lyrics:0"
    assert section is not None


def test_time_retrieval_always_includes_global_bpm_and_key_facts():
    evidence = nearby_analysis_evidence(
        _result(),
        targets=[TeachingTimeSpan(start_s=7, end_s=12)],
    )
    by_source_id = {
        item.metadata.get("teaching_source_id"): item
        for item in evidence
    }

    assert by_source_id["technical_metrics.bpm"].text == "78 BPM"
    assert by_source_id["technical_metrics.key"].text == "C major"
    assert by_source_id["technical_metrics.bpm"].kind == EvidenceType.COMPUTED
    assert by_source_id["technical_metrics.key"].kind == EvidenceType.COMPUTED
    assert by_source_id["technical_metrics.bpm"].span is None
    assert by_source_id["technical_metrics.key"].span is None


def _chat_response(
    *,
    evidence: list[AnswerEvidence],
    confidence: float = 0.8,
    insufficient_evidence: bool = False,
) -> TeachingChatResponse:
    return TeachingChatResponse(
        answer="先核对这一时段的声音事实。",
        time_ranges=[
            AnswerTimeRange(
                id="range-main",
                start_s=2,
                end_s=10,
                label="主要时段",
                purpose="核对声音变化",
            )
        ],
        evidence=evidence,
        listening_task=ListeningTask(
            instruction="循环播放，只记录实际听见的变化。",
            focus=AudioDimension.OTHER,
            time_range_id="range-main",
        ),
        confidence=confidence,
        insufficient_evidence=insufficient_evidence,
    )


def test_chat_response_requires_evidence_or_explicit_low_confidence_mode():
    with pytest.raises(ValidationError, match="normal teaching answer"):
        _chat_response(evidence=[])

    insufficient = _chat_response(
        evidence=[],
        confidence=0.25,
        insufficient_evidence=True,
    )
    assert insufficient.insufficient_evidence is True

    with pytest.raises(ValidationError, match="confidence"):
        _chat_response(
            evidence=[],
            confidence=0.8,
            insufficient_evidence=True,
        )


def test_chat_grounding_rejects_mutated_direct_fact_statement():
    result = _result()
    nearby = nearby_analysis_evidence(
        result,
        targets=[TeachingTimeSpan(start_s=2, end_s=10)],
    )
    context = TeachingChatContext(
        analysis_id="song-1",
        question="这里是什么乐器？",
        current_time_s=6,
        nearby_analysis_evidence=nearby,
        listener_profile=ListenerProfile(),
        analysis_summary=result.summary,
        duration_s=20,
    )
    response = _chat_response(
        evidence=[
            AnswerEvidence(
                id="claim-1",
                statement="这一段出现了猛烈的鼓点",
                claim_type=EvidenceClaimType.OBSERVED_FACT,
                dimension=AudioDimension.RHYTHM,
                source_refs=["sound_events:0"],
                time_range_ids=["range-main"],
                confidence=0.9,
            )
        ]
    )

    with pytest.raises(GroundingError, match="changes the sourced statement"):
        validate_chat_response(response, context=context)


def test_fallback_chat_answer_passes_time_and_evidence_grounding():
    result, understanding_map = _understanding_map()
    context = TeachingChatContext(
        analysis_id="song-1",
        question="为什么这一段感觉逐渐明亮？",
        current_time_s=10,
        current_section=section_at_time(understanding_map, 10),
        nearby_events=understanding_map.events,
        listener_profile=ListenerProfile(),
        analysis_summary=result.summary,
        duration_s=20,
    )

    response = asyncio.run(
        EvidenceTeachingModel().answer_music_question(context)
    )
    validate_chat_response(response, context=context)

    assert response.time_ranges[0].start_s == pytest.approx(2.5)
    assert response.player_actions
    assert response.listening_task.time_range_id == response.time_ranges[0].id
    assert "可直接观察的事实" in response.answer
    assert response.relistened is False
    assert response.evidence[0].claim_type == (
        EvidenceClaimType.GROUNDED_INTERPRETATION
    )
