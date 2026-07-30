from __future__ import annotations

import re

from music_insight.schemas import VocalPresenceStatus
from music_insight.teaching.grounding import (
    SourceFact,
    analysis_source_catalog,
)
from music_insight.teaching.models import (
    AnalysisEvidenceRef,
    AnswerEvidence,
    AnswerTimeRange,
    AudioDimension,
    EmotionalArcPoint,
    EvidenceClaimType,
    KeyMoment,
    LyricsContext,
    MapGenerationContext,
    MusicUnderstandingMap,
    PlayerAction,
    PlayerActionType,
    SectionMarker,
    TeachingChatContext,
    TeachingChatResponse,
    TeachingTimeSpan,
    UnderstandingEvent,
    ListeningTask,
)


class EvidenceTeachingModel:
    """Conservative fallback that never invents unheard audio details."""

    async def build_understanding_map(
        self,
        context: MapGenerationContext,
    ) -> MusicUnderstandingMap:
        result = context.result
        instrumental = (
            result.vocal_presence.status is VocalPresenceStatus.INSTRUMENTAL
        )
        catalog = analysis_source_catalog(result)
        timed_facts = [fact for fact in catalog.values() if fact.span is not None]
        groups = _cluster_facts(timed_facts)
        sections = _positional_sections(
            context.duration_s,
            instrumental=instrumental,
        )
        events: list[UnderstandingEvent] = []
        for index, group in enumerate(groups[:80]):
            span = TeachingTimeSpan(
                start_s=min(fact.span.start_s for fact in group if fact.span),
                end_s=max(fact.span.end_s for fact in group if fact.span),
            )
            section = _section_for_span(sections, span)
            direct_facts = [
                fact
                for fact in group
                if fact.claim_type
                in {
                    EvidenceClaimType.OBSERVED_FACT,
                    EvidenceClaimType.COMPUTED_FACT,
                }
            ]
            if not direct_facts:
                continue
            facts = _dedupe(fact.statement for fact in direct_facts)
            observation = "；".join(facts[:4])[:1200]
            dimensions = _dedupe(fact.dimension for fact in group)
            supplied_interpretations = _dedupe(
                fact.statement
                for fact in group
                if fact.claim_type
                in {
                    EvidenceClaimType.GROUNDED_INTERPRETATION,
                    EvidenceClaimType.POSSIBLE_READING,
                }
            )
            interpretation = (
                "；".join(supplied_interpretations[:3])[:1200]
                if supplied_interpretations
                else (
                    "这些变化可能共同形成"
                    f"以{'、'.join(_dimension_label(value) for value in dimensions[:3])}"
                    "为重点的听觉感受；具体情绪仍应结合前后段落复听。"
                )
            )
            role = (
                "这一时段为作品表达提供局部声音线索；"
                "现有证据不足以断言创作者的唯一意图。"
            )
            lyrics = (
                []
                if instrumental
                else _lyrics_for_span(result.lyrics, span)
            )
            evidence_refs = [_reference(fact) for fact in group[:12]]
            confidence = _event_confidence(
                group,
                has_grounded_interpretation=bool(supplied_interpretations),
            )
            focus = _listening_focus(
                group,
                direct_facts,
                instrumental=instrumental,
            )
            events.append(
                UnderstandingEvent(
                    id=f"event-{index + 1}",
                    start_s=span.start_s,
                    end_s=span.end_s,
                    section=section.label,
                    observation=observation,
                    interpretation=interpretation,
                    expressive_role=role,
                    audio_evidence=evidence_refs,
                    lyrics_context=lyrics,
                    listening_task=_task_for_dimension(focus),
                    alternative_readings=[
                        "同一声音变化也可能被听成段落推进，而不一定对应固定情绪。"
                    ],
                    confidence=confidence,
                )
            )

        atmosphere_facts = [
            fact.statement
            for source_id, fact in catalog.items()
            if source_id.startswith(("emotion_timeline:", "inferred_atmosphere:"))
        ]
        emotional_arc = _emotional_arc(catalog)
        key_moments = _key_moments(events)
        warnings = [
            "当前导赏地图由已有时间证据保守生成；曲式名称和主观意境可能有其他解释。"
        ]
        if instrumental:
            warnings.append(
                "已按纯器乐模式组织导赏；不会补写歌词、主歌或副歌标签。"
            )
        if len(key_moments) < 3:
            warnings.append("时间证据较少，暂时无法可靠推荐三个关键时刻。")
        if not events:
            warnings.append("缺少带时间范围的证据，暂时不能生成详细理解事件。")
        return MusicUnderstandingMap(
            core_expression=_first_sentence(result.summary),
            overall_atmosphere=(
                "；".join(_dedupe(atmosphere_facts)[:4])[:1600]
                if atmosphere_facts
                else "现有证据不足以给出可靠的整体意境标签，请从具体声音变化开始复听。"
            ),
            emotional_arc=emotional_arc,
            sections=sections,
            events=events,
            key_moments=key_moments,
            confidence=_map_confidence(events),
            warnings=warnings,
        )

    async def answer_music_question(
        self,
        context: TeachingChatContext,
    ) -> TeachingChatResponse:
        ranges = _answer_ranges(context)
        primary = ranges[0]
        relevant = [
            event
            for event in context.nearby_events
            if event.span.overlaps(primary.span, tolerance=1.0)
        ] or context.nearby_events[:1]
        evidence: list[AnswerEvidence] = []
        relisten_facts = [
            item
            for item in context.relisten_evidence
            if item.span.overlaps(primary.span, tolerance=0.75)
        ]
        for index, item in enumerate(relisten_facts[:4]):
            evidence.append(
                AnswerEvidence(
                    id=f"relisten-evidence-{index + 1}",
                    statement=item.observation,
                    claim_type=EvidenceClaimType.OBSERVED_FACT,
                    dimension=item.dimension,
                    source_refs=[item.id],
                    time_range_ids=[primary.id],
                    confidence=item.confidence,
                )
            )
        if relevant:
            event = relevant[0]
            source_refs = [f"understanding_event:{event.id}"]
            evidence.append(
                AnswerEvidence(
                    id="answer-evidence-1",
                    statement=event.observation,
                    claim_type=EvidenceClaimType.GROUNDED_INTERPRETATION,
                    dimension=event.audio_evidence[0].dimension,
                    source_refs=source_refs,
                    time_range_ids=[primary.id],
                    confidence=event.confidence,
                )
            )
            direct = event.interpretation
            fact = "；".join(
                [
                    event.observation,
                    *(item.observation for item in relisten_facts[:3]),
                ]
            )[:1000]
            effect = event.expressive_role
            alternatives = event.alternative_readings
            task_instruction = event.listening_task
            focus = event.audio_evidence[0].dimension
            confidence = min(event.confidence, 0.72)
        else:
            direct = "这段目前没有足够的时间证据支持具体音乐判断。"
            fact = (
                "；".join(item.observation for item in relisten_facts[:4])
                or "现有分析在这个时间范围内没有可核对的声音事件。"
            )
            effect = "因此不应把某一种情绪或创作意图当成确定答案。"
            alternatives = ["你对这段的个人感受仍然有效，可以把它作为下一轮复听假设。"]
            task_instruction = "先只听节奏与音量变化，再用一句话描述你实际听见的变化。"
            focus = AudioDimension.OTHER
            confidence = 0.2
        answer = (
            f"简短结论：{direct}\n"
            f"可直接观察的事实：{fact}\n"
            f"基于事实的表达解释：{effect}\n"
            "主观理解并非唯一答案；可以把下面的其他理解作为复听时的比较。"
        )
        actions = [
            PlayerAction(
                type=PlayerActionType.PLAY_RANGE,
                time_range_id=primary.id,
                label="播放这段证据",
            ),
            PlayerAction(
                type=PlayerActionType.LOOP_RANGE,
                time_range_id=primary.id,
                label="循环复听",
            ),
        ]
        if len(ranges) == 2:
            actions.append(
                PlayerAction(
                    type=PlayerActionType.COMPARE_AB,
                    time_range_id=ranges[0].id,
                    comparison_time_range_id=ranges[1].id,
                    label="A/B 对比两个片段",
                )
            )
        return TeachingChatResponse(
            answer=answer,
            time_ranges=ranges,
            evidence=evidence,
            listening_task=ListeningTask(
                instruction=task_instruction,
                focus=focus,
                time_range_id=primary.id,
            ),
            suggested_questions=[
                "这段最先发生变化的是节奏、音色还是力度？",
                "这段和前一个段落的气氛有什么不同？",
            ],
            player_actions=actions,
            alternative_readings=alternatives[:5],
            confidence=confidence,
            relistened=bool(context.relisten_evidence),
            insufficient_evidence=not bool(relevant),
        )


def _cluster_facts(facts: list[SourceFact]) -> list[list[SourceFact]]:
    ordered = sorted(
        facts,
        key=lambda fact: (
            fact.span.start_s if fact.span else float("inf"),
            fact.span.end_s if fact.span else float("inf"),
            fact.source_id,
        ),
    )
    groups: list[list[SourceFact]] = []
    for fact in ordered:
        if fact.span is None:
            continue
        if not groups:
            groups.append([fact])
            continue
        current_end = max(
            item.span.end_s for item in groups[-1] if item.span is not None
        )
        if fact.span.start_s <= current_end + 2.0 and len(groups[-1]) < 12:
            groups[-1].append(fact)
        else:
            groups.append([fact])
    return groups


def _positional_sections(
    duration_s: float,
    *,
    instrumental: bool,
) -> list[SectionMarker]:
    if duration_s <= 30:
        boundaries = [(0.0, duration_s, "全段")]
    else:
        first = duration_s / 3
        second = first * 2
        labels = (
            ("听觉阶段 1", "听觉阶段 2", "听觉阶段 3")
            if instrumental
            else ("前段", "中段", "后段")
        )
        boundaries = [
            (0.0, first, labels[0]),
            (first, second, labels[1]),
            (second, duration_s, labels[2]),
        ]
    return [
        SectionMarker(
            id=f"section-{index + 1}",
            label=label,
            span=TeachingTimeSpan(start_s=start, end_s=end),
            expressive_role=(
                "用于定位复听的中性时间分区，并非未经证据确认的曲式判断。"
                if instrumental
                else "用于定位复听的时间分区，并非未经证据确认的主歌或副歌判断。"
            ),
            confidence=0.35,
            alternative_labels=["时间分区"],
        )
        for index, (start, end, label) in enumerate(boundaries)
        if end > start
    ]


def _section_for_span(
    sections: list[SectionMarker],
    span: TeachingTimeSpan,
) -> SectionMarker:
    midpoint = (span.start_s + span.end_s) / 2
    return min(
        sections,
        key=lambda section: (
            0
            if section.span.start_s <= midpoint <= section.span.end_s
            else 1,
            abs(section.span.start_s - midpoint),
        ),
    )


def _lyrics_for_span(lyrics, span: TeachingTimeSpan) -> list[LyricsContext]:
    values: list[LyricsContext] = []
    for index, lyric in enumerate(lyrics):
        if lyric.span is None or lyric.span.end_s <= lyric.span.start_s:
            continue
        lyric_span = TeachingTimeSpan(
            start_s=lyric.span.start_s,
            end_s=lyric.span.end_s,
        )
        if not lyric_span.overlaps(span, tolerance=0.5):
            continue
        values.append(
            LyricsContext(
                source_id=f"lyrics:{index}",
                text=lyric.text,
                span=lyric_span,
                language=lyric.language,
                confidence=lyric.confidence,
            )
        )
    return values[:12]


def _reference(fact: SourceFact) -> AnalysisEvidenceRef:
    return AnalysisEvidenceRef(
        source_type=fact.source_type,
        source_id=fact.source_id,
        dimension=fact.dimension,
        statement=fact.statement,
        claim_type=fact.claim_type,
        span=fact.span,
        confidence=fact.confidence,
    )


def _emotional_arc(
    catalog: dict[str, SourceFact],
) -> list[EmotionalArcPoint]:
    points: list[EmotionalArcPoint] = []
    for source_id, fact in catalog.items():
        if not source_id.startswith("emotion_timeline:") or fact.span is None:
            continue
        points.append(
            EmotionalArcPoint(
                span=fact.span,
                description=fact.statement[:600],
                evidence_refs=[_reference(fact)],
                confidence=fact.confidence if fact.confidence is not None else 0.45,
            )
        )
    return sorted(points, key=lambda point: point.span.start_s)[:40]


def _key_moments(events: list[UnderstandingEvent]) -> list[KeyMoment]:
    ranked = sorted(
        events,
        key=lambda event: (-event.confidence, event.start_s),
    )[:5]
    return [
        KeyMoment(
            id=f"key-{index + 1}",
            event_id=event.id,
            start_s=event.start_s,
            end_s=event.end_s,
            reason=event.interpretation[:800],
            listening_task=event.listening_task,
            confidence=event.confidence,
        )
        for index, event in enumerate(sorted(ranked, key=lambda event: event.start_s))
    ]


def _answer_ranges(context: TeachingChatContext) -> list[AnswerTimeRange]:
    if context.compare_ranges:
        return [
            AnswerTimeRange(
                id=f"range-{index + 1}",
                start_s=span.start_s,
                end_s=span.end_s,
                label=f"对比片段 {'AB'[index]}",
                purpose="用于比较声音与表达变化",
            )
            for index, span in enumerate(context.compare_ranges[:2])
        ]
    if context.selected_range is not None:
        span = context.selected_range
        label = "用户选中的片段"
    else:
        start_s = max(0.0, context.current_time_s - 7.5)
        end_s = min(context.duration_s, context.current_time_s + 7.5)
        if end_s <= start_s:
            start_s = max(0.0, context.duration_s - 15.0)
            end_s = context.duration_s
        span = TeachingTimeSpan(start_s=start_s, end_s=end_s)
        label = "当前播放位置附近"
    return [
        AnswerTimeRange(
            id="range-1",
            start_s=span.start_s,
            end_s=span.end_s,
            label=label,
            purpose="定位本次回答的听觉证据",
        )
    ]


def _first_sentence(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "现有证据不足以概括作品的核心表达。"
    return re.split(r"(?<=[。！？.!?])\s*", stripped, maxsplit=1)[0][:1000]


def _task_for_dimension(dimension: AudioDimension) -> str:
    labels = {
        AudioDimension.MELODY: "只跟随主旋律的走向，留意它何时上行、停留或回落。",
        AudioDimension.HARMONY: "暂时忽略歌词，听和声色彩何时变得明亮、紧张或松弛。",
        AudioDimension.RHYTHM: "跟着拍点轻敲，注意鼓点或重音在什么位置发生变化。",
        AudioDimension.TIMBRE: "只比较声音质感，辨认它何时变厚、变亮或变得粗糙。",
        AudioDimension.DYNAMICS: "关注音量与能量轮廓，找出增强或收束的瞬间。",
        AudioDimension.INSTRUMENTATION: "每次只追踪一种乐器，辨认它何时进入、退出或改变奏法。",
        AudioDimension.SPACE: "戴耳机听声音的远近、左右位置与混响尾音。",
        AudioDimension.LYRICS: "先只听歌词重音，再比较它与伴奏重音是否对齐。",
        AudioDimension.STRUCTURE: "比较这一段开头和结尾，找出段落边界的声音提示。",
        AudioDimension.OTHER: "先写下实际听到的两个变化，再描述它们带来的感受。",
    }
    return labels[dimension]


def _dimension_label(dimension: AudioDimension) -> str:
    return {
        AudioDimension.MELODY: "旋律",
        AudioDimension.HARMONY: "和声",
        AudioDimension.RHYTHM: "节奏",
        AudioDimension.TIMBRE: "音色",
        AudioDimension.DYNAMICS: "力度",
        AudioDimension.INSTRUMENTATION: "配器",
        AudioDimension.SPACE: "空间感",
        AudioDimension.LYRICS: "歌词",
        AudioDimension.STRUCTURE: "段落结构",
        AudioDimension.OTHER: "声音变化",
    }[dimension]


def _map_confidence(events: list[UnderstandingEvent]) -> float:
    if not events:
        return 0.15
    return min(0.65, sum(event.confidence for event in events) / len(events))


def _event_confidence(
    facts: list[SourceFact],
    *,
    has_grounded_interpretation: bool,
) -> float:
    """Keep acoustic certainty separate from expressive interpretation support."""

    known = [
        fact.confidence
        for fact in facts
        if fact.confidence is not None
    ]
    acoustic_support = sum(known) / len(known) if known else 0.4
    dimensions = {fact.dimension for fact in facts}
    ceiling = 0.65 if has_grounded_interpretation else 0.45
    if len(dimensions) <= 1:
        ceiling = min(ceiling, 0.5)
    return max(0.0, min(acoustic_support, ceiling))


def _listening_focus(
    facts: list[SourceFact],
    direct_facts: list[SourceFact],
    *,
    instrumental: bool,
) -> AudioDimension:
    preferred = [
        fact.dimension
        for fact in facts
        if fact.claim_type
        in {
            EvidenceClaimType.GROUNDED_INTERPRETATION,
            EvidenceClaimType.POSSIBLE_READING,
        }
        and fact.dimension not in {AudioDimension.OTHER, AudioDimension.LYRICS}
    ]
    candidates = [
        *preferred,
        *(fact.dimension for fact in direct_facts),
    ]
    for dimension in candidates:
        if instrumental and dimension is AudioDimension.LYRICS:
            continue
        return dimension
    return AudioDimension.OTHER


def _dedupe(values) -> list:
    seen = set()
    output = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output
