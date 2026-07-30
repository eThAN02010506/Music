from __future__ import annotations

import re
from dataclasses import dataclass

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
    RelistenEvidence,
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
        output_language = context.output_language
        instrumental = (
            result.vocal_presence.status is VocalPresenceStatus.INSTRUMENTAL
        )
        catalog = analysis_source_catalog(result)
        timed_facts = [fact for fact in catalog.values() if fact.span is not None]
        groups = _cluster_facts(timed_facts)
        sections = _positional_sections(
            context.duration_s,
            instrumental=instrumental,
            output_language=output_language,
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
            dimensions = _dedupe(fact.dimension for fact in group)
            facts = _dedupe(
                fact.statement
                for fact in direct_facts
                if _matches_output_language(
                    fact.statement,
                    output_language,
                )
            )
            observation = (
                ("; ".join(facts[:4]) if output_language == "en" else "；".join(facts[:4]))[:1200]
                if facts
                else _generic_observation(dimensions, output_language)
            )
            supplied_interpretations = _dedupe(
                fact.statement
                for fact in group
                if fact.claim_type
                in {
                    EvidenceClaimType.GROUNDED_INTERPRETATION,
                    EvidenceClaimType.POSSIBLE_READING,
                }
                and _matches_output_language(
                    fact.statement,
                    output_language,
                )
            )
            interpretation = (
                (
                    "; ".join(supplied_interpretations[:3])
                    if output_language == "en"
                    else "；".join(supplied_interpretations[:3])
                )[:1200]
                if supplied_interpretations
                else _generic_interpretation(
                    dimensions,
                    output_language,
                )
            )
            role = _event_role(output_language)
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
                    listening_task=_task_for_dimension(
                        focus,
                        output_language,
                    ),
                    alternative_readings=[
                        (
                            "The same change may mark structural motion rather "
                            "than one fixed emotion."
                            if output_language == "en"
                            else "同一声音变化也可能被听成段落推进，而不一定对应固定情绪。"
                        )
                    ],
                    confidence=confidence,
                )
            )

        sections = _describe_sections(
            sections,
            events,
            output_language=output_language,
        )
        atmosphere_facts = [
            fact.statement
            for source_id, fact in catalog.items()
            if source_id.startswith(("emotion_timeline:", "inferred_atmosphere:"))
            and _matches_output_language(fact.statement, output_language)
        ]
        emotional_arc = _emotional_arc(
            catalog,
            output_language=output_language,
        )
        key_moments = _key_moments(events)
        warnings = [_message(
            output_language,
            "This guide was conservatively built from existing timed evidence; "
            "formal labels and subjective imagery may have other interpretations.",
            "当前导赏地图由已有时间证据保守生成；曲式名称和主观意境可能有其他解释。",
        )]
        if instrumental:
            warnings.append(
                _message(
                    output_language,
                    "Instrumental mode is active; the guide does not invent "
                    "lyrics, verses, or choruses.",
                    "已按纯器乐模式组织导赏；不会补写歌词、主歌或副歌标签。",
                )
            )
        if len(key_moments) < 3:
            warnings.append(_message(
                output_language,
                "Timed evidence is too sparse to recommend three key moments reliably.",
                "时间证据较少，暂时无法可靠推荐三个关键时刻。",
            ))
        if not events:
            warnings.append(_message(
                output_language,
                "There is not enough time-bounded evidence for detailed listening events.",
                "缺少带时间范围的证据，暂时不能生成详细理解事件。",
            ))
        return MusicUnderstandingMap(
            output_language=output_language,
            core_expression=_localized_core_expression(
                result.summary,
                timed_facts,
                output_language,
            ),
            overall_atmosphere=(
                (
                    "; ".join(_dedupe(atmosphere_facts)[:4])
                    if output_language == "en"
                    else "；".join(_dedupe(atmosphere_facts)[:4])
                )[:1600]
                if atmosphere_facts
                else _message(
                    output_language,
                    "The evidence does not support one reliable overall mood "
                    "label; begin with the timed audible changes.",
                    "现有证据不足以给出可靠的整体意境标签，请从具体声音变化开始复听。",
                )
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
        output_language = context.output_language
        ranges = _answer_ranges(
            context,
            output_language=output_language,
        )
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
                    dimension=_dimension_from_statement(
                        event.observation,
                        event.audio_evidence[0].dimension,
                    ),
                    source_refs=source_refs,
                    time_range_ids=[primary.id],
                    confidence=event.confidence,
                )
            )
            direct = event.interpretation
            localized_facts = [
                value
                for value in [
                    event.observation,
                    *(item.observation for item in relisten_facts[:3]),
                ]
                if _matches_output_language(value, output_language)
            ]
            fact = (
                ("; ".join(localized_facts) if output_language == "en" else "；".join(localized_facts))[:1000]
                if localized_facts
                else _generic_observation(
                    [item.dimension for item in event.audio_evidence],
                    output_language,
                )
            )
            effect = event.expressive_role
            alternatives = event.alternative_readings
            task_instruction = event.listening_task
            focus = _dimension_from_statement(
                event.observation,
                event.audio_evidence[0].dimension,
            )
            confidence = min(event.confidence, 0.72)
            insufficient_evidence = False
        else:
            direct = _message(
                output_language,
                "There is not enough timed evidence for a specific musical claim.",
                "这段目前没有足够的时间证据支持具体音乐判断。",
            )
            relisten_text = [
                item.observation
                for item in relisten_facts[:4]
                if _matches_output_language(
                    item.observation,
                    output_language,
                )
            ]
            fact = (
                ("; ".join(relisten_text) if output_language == "en" else "；".join(relisten_text))
                or _message(
                    output_language,
                    "The saved analysis has no verifiable sound event in this range.",
                    "现有分析在这个时间范围内没有可核对的声音事件。",
                )
            )
            effect = _message(
                output_language,
                "Do not treat one emotion or creative intention as certain.",
                "因此不应把某一种情绪或创作意图当成确定答案。",
            )
            alternatives = [_message(
                output_language,
                "Your own response is still valid as a hypothesis for the next listen.",
                "你对这段的个人感受仍然有效，可以把它作为下一轮复听假设。",
            )]
            task_instruction = _message(
                output_language,
                "Listen only for rhythm and volume, then describe one audible change.",
                "先只听节奏与音量变化，再用一句话描述你实际听见的变化。",
            )
            focus = AudioDimension.OTHER
            confidence = 0.2
            insufficient_evidence = True

        tailored = _tailor_fallback_answer(
            context=context,
            primary=primary,
            relevant=relevant,
            relisten_facts=relisten_facts,
            direct=direct,
            fact=fact,
            effect=effect,
            task_instruction=task_instruction,
            focus=focus,
            alternatives=alternatives,
            confidence=confidence,
            insufficient_evidence=insufficient_evidence,
        )
        answer = (
            (
                f"Short answer: {tailored.direct}\n"
                f"Audible evidence: {tailored.fact}\n"
                f"Expressive effect: {tailored.effect}\n"
                "This is not the only valid interpretation; compare the alternatives while listening."
            )
            if output_language == "en"
            else (
                f"简短结论：{tailored.direct}\n"
                f"可直接观察的事实：{tailored.fact}\n"
                f"基于事实的表达解释：{tailored.effect}\n"
                "主观理解并非唯一答案；可以把下面的其他理解作为复听时的比较。"
            )
        )
        actions = [
            PlayerAction(
                type=PlayerActionType.PLAY_RANGE,
                time_range_id=primary.id,
                label=_message(output_language, "Play this evidence", "播放这段证据"),
            ),
            PlayerAction(
                type=PlayerActionType.LOOP_RANGE,
                time_range_id=primary.id,
                label=_message(output_language, "Loop this range", "循环复听"),
            ),
        ]
        if len(ranges) == 2:
            actions.append(
                PlayerAction(
                    type=PlayerActionType.COMPARE_AB,
                    time_range_id=ranges[0].id,
                    comparison_time_range_id=ranges[1].id,
                    label=_message(output_language, "Compare A/B", "A/B 对比两个片段"),
                )
            )
        return TeachingChatResponse(
            output_language=output_language,
            answer=answer,
            time_ranges=ranges,
            evidence=evidence,
            listening_task=ListeningTask(
                instruction=tailored.task_instruction,
                focus=tailored.focus,
                time_range_id=primary.id,
            ),
            suggested_questions=_suggested_questions(
                context,
                intent=tailored.intent,
                focus=tailored.focus,
            ),
            player_actions=actions,
            alternative_readings=tailored.alternatives[:5],
            confidence=tailored.confidence,
            relistened=bool(context.relisten_evidence),
            insufficient_evidence=tailored.insufficient_evidence,
        )


@dataclass(frozen=True)
class _FallbackAnswer:
    intent: str
    direct: str
    fact: str
    effect: str
    task_instruction: str
    focus: AudioDimension
    alternatives: list[str]
    confidence: float
    insufficient_evidence: bool


def _tailor_fallback_answer(
    *,
    context: TeachingChatContext,
    primary: AnswerTimeRange,
    relevant: list[UnderstandingEvent],
    relisten_facts: list[RelistenEvidence],
    direct: str,
    fact: str,
    effect: str,
    task_instruction: str,
    focus: AudioDimension,
    alternatives: list[str],
    confidence: float,
    insufficient_evidence: bool,
) -> _FallbackAnswer:
    """Keep conservative fallback answers responsive to the actual question."""

    output_language = context.output_language
    intent = _question_intent(context.question, bool(context.compare_ranges))
    dimensions = _available_dimensions(relevant, relisten_facts)
    dimension_text = _join_dimensions(dimensions, output_language)

    if intent == "general" and relevant:
        # A fallback must not turn a broad map interpretation into a fresh,
        # question-specific fact. Keep the direct answer at the dimensions
        # that are actually present in the timed evidence.
        direct = _generic_interpretation(dimensions, output_language)
        effect = _event_role(output_language)
        focus = dimensions[0] if dimensions else focus
        confidence = min(confidence, 0.48)
    elif intent == "change_order":
        if len(dimensions) == 1:
            confirmed = dimensions[0]
            direct = _message(
                output_language,
                (
                    f"The saved evidence confirms a change in "
                    f"{_dimension_label(confirmed, output_language)}, but it "
                    "does not time the other choices precisely enough to say "
                    "which of all of them changed first."
                ),
                (
                    f"现有证据只确认了{_dimension_label(confirmed)}的变化，"
                    "没有把其余选项的变化时刻记录得足够精确，因此还不能断言"
                    "三者中谁最先改变。"
                ),
            )
            effect = _message(
                output_language,
                "This change can shape the sense of motion, but it does not prove simultaneous changes in the other dimensions.",
                "这一变化可以影响推进感，但不能据此推定节奏和音色也同时发生了变化。",
            )
            task_instruction = _message(
                output_language,
                "Replay once for pulse, once for tone color, and once for loudness; mark the first clearly audible change on each pass.",
                "连续复听三遍：第一遍只跟拍点，第二遍只听音色，第三遍只听响度；分别记下第一次明确变化的时刻。",
            )
            focus = confirmed
            confidence = min(confidence, 0.48)
        elif dimensions:
            direct = _message(
                output_language,
                (
                    f"The timed evidence covers {dimension_text}, but its "
                    "ranges are too coarse to establish a reliable first change."
                ),
                (
                    f"现有时间证据涉及{dimension_text}，但时间粒度不足以可靠判断"
                    "哪一项最先变化。"
                ),
            )
            task_instruction = _message(
                output_language,
                "Loop the range three times and timestamp the first change in rhythm, timbre, and loudness separately.",
                "循环这段三遍，分别标记节奏、音色和响度第一次发生变化的时刻，再比较先后。",
            )
            confidence = min(confidence, 0.42)
        else:
            direct = _message(
                output_language,
                "The saved evidence does not distinguish rhythm, timbre, and dynamics here, so their order cannot be determined yet.",
                "现有证据没有分别记录这里的节奏、音色与力度变化，因此暂时无法判断先后。",
            )
            task_instruction = _message(
                output_language,
                "Replay three times, attending to rhythm, timbre, and loudness separately, and mark the first change you can hear.",
                "分三遍复听，分别只关注节奏、音色和响度，并标记各自第一次能听见的变化。",
            )
            focus = AudioDimension.OTHER
            confidence = min(confidence, 0.3)
            insufficient_evidence = True
    elif intent == "compare_previous":
        previous = _previous_event(relevant, context.nearby_events)
        at_opening = primary.start_s <= 0.5 and previous is None
        if at_opening:
            direct = _message(
                output_language,
                "This range starts at the beginning, so there is no previous passage in the recording to compare with it.",
                "这一范围从录音开头开始，前面没有可供比较的段落。",
            )
            fact = _message(
                output_language,
                f"The cited range begins at {primary.start_s:.1f} seconds.",
                f"本次引用范围从 {primary.start_s:.1f} 秒开始。",
            )
            effect = _message(
                output_language,
                "Treat this passage as the reference sound world; compare a later passage against it instead of inventing a preceding mood.",
                "应把这里当作建立参照的声音起点，再用后面的片段与它比较，而不是虚构一个“前段气氛”。",
            )
            task_instruction = _message(
                output_language,
                "Remember the opening's loudness and texture, then jump to the first later turning point and compare them.",
                "先记住开头的响度与织体，再跳到后面第一个明显转折处进行对比。",
            )
            focus = AudioDimension.STRUCTURE
            confidence = 0.4
            insufficient_evidence = True
        elif previous is None:
            direct = _message(
                output_language,
                "Only the current passage is present in the retrieved evidence, so a previous-passage comparison is not yet supportable.",
                "检索到的证据只有当前片段，缺少前一段的同期证据，因此暂时不能可靠比较两段气氛。",
            )
            task_instruction = _message(
                output_language,
                "Set A to the preceding passage and B to this one, then compare loudness, texture, and pulse in that order.",
                "请把前一段设为 A、当前段设为 B，再依次比较响度、织体和拍点。",
            )
            confidence = min(confidence, 0.35)
            insufficient_evidence = True
        else:
            direct = _message(
                output_language,
                "Both passages are available, but the saved evidence is not detailed enough to reduce their difference to one mood label.",
                "前后两段都有时间证据，但现有证据还不足以把差异归结为一个确定的气氛标签。",
            )
            fact = _message(
                output_language,
                (
                    f"The previous event spans {previous.start_s:.1f}–"
                    f"{previous.end_s:.1f} seconds; current evidence covers "
                    f"{primary.start_s:.1f}–{primary.end_s:.1f} seconds."
                ),
                (
                    f"前一事件位于 {previous.start_s:.1f}–{previous.end_s:.1f} 秒；"
                    f"当前证据位于 {primary.start_s:.1f}–{primary.end_s:.1f} 秒。"
                ),
            )
            task_instruction = _message(
                output_language,
                "Alternate the two ranges and compare loudness, density, and instrumental color one dimension at a time.",
                "交替播放前后两段，每次只比较一个维度：响度、织体密度、乐器音色。",
            )
            focus = AudioDimension.STRUCTURE
            confidence = min(confidence, 0.48)
    elif intent == "instrument" and AudioDimension.INSTRUMENTATION not in dimensions:
        direct = _message(
            output_language,
            "The available evidence does not identify an instrument reliably in this range.",
            "这一范围的现有证据没有可靠标出具体乐器，因此不能仅凭当前记录点名铜管或弦乐。",
        )
        effect = _message(
            output_language,
            "An orchestral impression may be a useful listening hypothesis, but it is not yet a verified instrument claim.",
            "“管弦乐感”可以作为复听假设，但目前还不是已经核实的乐器结论。",
        )
        task_instruction = _message(
            output_language,
            "Replay once for sustained tones and once for attacks; describe the sound before naming the instrument.",
            "先听持续音，再听发音瞬间；先描述音色与奏法，再尝试判断乐器。",
        )
        focus = AudioDimension.TIMBRE
        confidence = min(confidence, 0.35)
        insufficient_evidence = True
    elif intent == "lyrics" and not context.nearby_lyrics:
        direct = _message(
            output_language,
            "No reliable lyric segment is available in this range.",
            "这一范围没有可确认的歌词片段，不能根据器乐或能量证据补写歌词。",
        )
        effect = _message(
            output_language,
            "The passage can still be discussed through melody, rhythm, timbre, and dynamics.",
            "仍可以从旋律、节奏、音色和力度理解这一段的表达。",
        )
        task_instruction = _message(
            output_language,
            "Listen for whether a stable vocal line is actually present before trying to transcribe words.",
            "先确认是否真的存在稳定人声线，再尝试听辨词句。",
        )
        focus = AudioDimension.LYRICS
        confidence = min(confidence, 0.3)
        insufficient_evidence = True

    return _FallbackAnswer(
        intent=intent,
        direct=direct,
        fact=fact,
        effect=effect,
        task_instruction=task_instruction,
        focus=focus,
        alternatives=alternatives,
        confidence=min(confidence, 0.4) if insufficient_evidence else confidence,
        insufficient_evidence=insufficient_evidence,
    )


def _question_intent(question: str, has_comparison: bool) -> str:
    folded = " ".join(question.casefold().split())
    if has_comparison:
        return "compare_previous"
    if any(
        token in folded
        for token in (
            "最先发生变化",
            "先发生变化",
            "哪个先变",
            "哪一个先变",
            "which changes first",
            "what changes first",
        )
    ):
        return "change_order"
    if any(
        token in folded
        for token in (
            "前一个段落",
            "前一段",
            "上一段",
            "previous passage",
            "previous section",
            "compared with before",
        )
    ):
        return "compare_previous"
    if any(token in folded for token in ("乐器", "配器", "instrument", "orchestra")):
        return "instrument"
    if any(token in folded for token in ("歌词", "唱了什么", "lyric", "words sung")):
        return "lyrics"
    return "general"


def _available_dimensions(
    relevant: list[UnderstandingEvent],
    relisten_facts: list[RelistenEvidence],
) -> list[AudioDimension]:
    values: list[AudioDimension] = []
    for event in relevant:
        for item in event.audio_evidence:
            values.append(_dimension_from_statement(item.statement, item.dimension))
    for item in relisten_facts:
        values.append(_dimension_from_statement(item.observation, item.dimension))
    return _dedupe(values)


def _dimension_from_statement(
    statement: str,
    declared: AudioDimension,
) -> AudioDimension:
    folded = statement.casefold()
    candidates = (
        (AudioDimension.DYNAMICS, ("能量", "响度", "力度", "energy", "loudness", "dynamic")),
        (AudioDimension.RHYTHM, ("节奏", "节拍", "鼓点", "rhythm", "beat", "tempo")),
        (AudioDimension.TIMBRE, ("音色", "质感", "timbre", "tone color")),
        (AudioDimension.INSTRUMENTATION, ("乐器", "配器", "铜管", "弦乐", "instrument", "orchestra")),
        (AudioDimension.HARMONY, ("和声", "和弦", "harmony", "chord")),
        (AudioDimension.MELODY, ("旋律", "音高", "melody", "pitch")),
    )
    for dimension, keywords in candidates:
        if any(keyword in folded for keyword in keywords):
            return dimension
    return declared


def _join_dimensions(
    dimensions: list[AudioDimension],
    output_language: str,
) -> str:
    labels = [_dimension_label(item, output_language) for item in dimensions]
    if not labels:
        return _message(output_language, "no specific dimension", "没有具体维度")
    return (", ".join(labels) if output_language == "en" else "、".join(labels))


def _previous_event(
    relevant: list[UnderstandingEvent],
    nearby: list[UnderstandingEvent],
) -> UnderstandingEvent | None:
    if not relevant:
        return None
    current = relevant[0]
    candidates = [
        event
        for event in nearby
        if event.id != current.id and event.end_s <= current.start_s + 0.25
    ]
    return max(candidates, key=lambda event: event.end_s, default=None)


def _suggested_questions(
    context: TeachingChatContext,
    *,
    intent: str,
    focus: AudioDimension,
) -> list[str]:
    output_language = context.output_language
    by_intent = {
        "change_order": [
            _message(
                output_language,
                "At what moment does the clearest change begin?",
                "最清楚的变化从哪一秒开始？",
            ),
            _message(
                output_language,
                "If loudness is matched, does the tone color still change?",
                "如果把音量差异忽略掉，音色是否仍然变化？",
            ),
        ],
        "compare_previous": [
            _message(
                output_language,
                "Where is the first clear turning point after this opening?",
                "这个开头之后，第一个明显转折出现在什么时候？",
            ),
            _message(
                output_language,
                "What material from the opening returns later?",
                "开头建立的哪些声音材料后来再次出现？",
            ),
        ],
        "instrument": [
            _message(
                output_language,
                "Which audible feature best distinguishes the instrument here?",
                "这里哪种音色特征最能帮助判断乐器？",
            ),
            _message(
                output_language,
                "Does the sound enter as one layer or several layers?",
                "这个声音是单一声部进入，还是多个声部叠加？",
            ),
        ],
        "lyrics": [
            _message(
                output_language,
                "Is a stable vocal line actually audible here?",
                "这里是否真的能听到稳定的人声线？",
            ),
            _message(
                output_language,
                "How does the accompaniment shape this passage without relying on lyrics?",
                "不依赖歌词时，伴奏怎样塑造这一段的表达？",
            ),
        ],
        "general": [
            _message(
                output_language,
                f"How does {_dimension_label(focus, output_language)} change across this range?",
                f"这一段的{_dimension_label(focus)}是怎样变化的？",
            ),
            _message(
                output_language,
                "Which later passage would make the clearest A/B comparison?",
                "后面哪一段最适合与这里做 A/B 对比？",
            ),
        ],
    }
    excluded = {
        _normalize_question(context.question),
        *(
            _normalize_question(turn.question)
            for turn in context.conversation_history[-8:]
        ),
    }
    return [
        question
        for question in by_intent[intent]
        if _normalize_question(question) not in excluded
    ][:2]


def _normalize_question(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


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
    output_language: str,
) -> list[SectionMarker]:
    if duration_s <= 30:
        boundaries = [(
            0.0,
            duration_s,
            _message(output_language, "Full excerpt", "全段"),
        )]
    else:
        first = duration_s / 3
        second = first * 2
        labels = (
            (
                ("Listening stage 1", "Listening stage 2", "Listening stage 3")
                if instrumental
                else ("Opening", "Middle", "Closing")
            )
            if output_language == "en"
            else (
                ("听觉阶段 1", "听觉阶段 2", "听觉阶段 3")
                if instrumental
                else ("前段", "中段", "后段")
            )
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
                _message(
                    output_language,
                    "A neutral listening window, not an unsupported formal label.",
                    "用于定位复听的中性时间分区，并非未经证据确认的曲式判断。",
                )
            ),
            confidence=0.35,
            alternative_labels=[
                _message(output_language, "Listening window", "时间分区")
            ],
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
    *,
    output_language: str,
) -> list[EmotionalArcPoint]:
    points: list[EmotionalArcPoint] = []
    for source_id, fact in catalog.items():
        if not source_id.startswith("emotion_timeline:") or fact.span is None:
            continue
        if not _matches_output_language(fact.statement, output_language):
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


def _answer_ranges(
    context: TeachingChatContext,
    *,
    output_language: str,
) -> list[AnswerTimeRange]:
    if context.compare_ranges:
        return [
            AnswerTimeRange(
                id=f"range-{index + 1}",
                start_s=span.start_s,
                end_s=span.end_s,
                label=(
                    f"Comparison {'AB'[index]}"
                    if output_language == "en"
                    else f"对比片段 {'AB'[index]}"
                ),
                purpose=_message(
                    output_language,
                    "Compare audible and expressive changes",
                    "用于比较声音与表达变化",
                ),
            )
            for index, span in enumerate(context.compare_ranges[:2])
        ]
    if context.selected_range is not None:
        span = context.selected_range
        label = _message(
            output_language,
            "Selected excerpt",
            "用户选中的片段",
        )
    else:
        start_s = max(0.0, context.current_time_s - 7.5)
        end_s = min(context.duration_s, context.current_time_s + 7.5)
        if end_s <= start_s:
            start_s = max(0.0, context.duration_s - 15.0)
            end_s = context.duration_s
        span = TeachingTimeSpan(start_s=start_s, end_s=end_s)
        label = _message(
            output_language,
            "Around the current position",
            "当前播放位置附近",
        )
    return [
        AnswerTimeRange(
            id="range-1",
            start_s=span.start_s,
            end_s=span.end_s,
            label=label,
            purpose=_message(
                output_language,
                "Locate the audible evidence for this answer",
                "定位本次回答的听觉证据",
            ),
        )
    ]


def _first_sentence(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "现有证据不足以概括作品的核心表达。"
    return re.split(r"(?<=[。！？.!?])\s*", stripped, maxsplit=1)[0][:1000]


def _task_for_dimension(
    dimension: AudioDimension,
    output_language: str,
) -> str:
    english = {
        AudioDimension.MELODY: "Follow only the main melodic contour; notice when it rises, holds, or falls.",
        AudioDimension.HARMONY: "Set lyrics aside and notice when the harmony sounds brighter, tenser, or more relaxed.",
        AudioDimension.RHYTHM: "Tap the pulse and locate changes in accents or percussion.",
        AudioDimension.TIMBRE: "Compare sound texture only; notice when it becomes thicker, brighter, or rougher.",
        AudioDimension.DYNAMICS: "Follow the loudness and energy contour; locate the moment it grows or recedes.",
        AudioDimension.INSTRUMENTATION: "Track one instrument at a time and mark when it enters, exits, or changes articulation.",
        AudioDimension.SPACE: "Use headphones to compare distance, stereo position, and reverberation tails.",
        AudioDimension.LYRICS: "Listen first for lyrical stress, then compare it with the accompaniment accents.",
        AudioDimension.STRUCTURE: "Compare the start and end of this range for audible boundary cues.",
        AudioDimension.OTHER: "Write down two audible changes before describing how they feel.",
    }
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
    return english[dimension] if output_language == "en" else labels[dimension]


def _dimension_label(
    dimension: AudioDimension,
    output_language: str = "zh",
) -> str:
    chinese = {
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
    }
    english = {
        AudioDimension.MELODY: "melody",
        AudioDimension.HARMONY: "harmony",
        AudioDimension.RHYTHM: "rhythm",
        AudioDimension.TIMBRE: "timbre",
        AudioDimension.DYNAMICS: "dynamics",
        AudioDimension.INSTRUMENTATION: "instrumentation",
        AudioDimension.SPACE: "space",
        AudioDimension.LYRICS: "lyrics",
        AudioDimension.STRUCTURE: "structure",
        AudioDimension.OTHER: "sound",
    }
    return english[dimension] if output_language == "en" else chinese[dimension]


def _describe_sections(
    sections: list[SectionMarker],
    events: list[UnderstandingEvent],
    *,
    output_language: str,
) -> list[SectionMarker]:
    roles_en = (
        "establishes the initial sound world and the material used for later comparison",
        "develops or contrasts earlier material, making changes in tension and texture easier to hear",
        "gathers, redirects, or releases the preceding motion to create a sense of arrival",
    )
    roles_zh = (
        "建立作品的声音起点，并提供后续复听时可比较的主要材料",
        "延续或对照前段材料，让张力与织体的变化逐步显现",
        "汇总、转向或释放此前的推进，形成抵达或收束的方向",
    )
    updated: list[SectionMarker] = []
    for index, section in enumerate(sections):
        local_events = [
            event
            for event in events
            if event.span.overlaps(section.span, tolerance=0.5)
        ]
        dimensions = _dedupe(
            reference.dimension
            for event in local_events
            for reference in event.audio_evidence
        )
        focus = (
            ", ".join(
                _dimension_label(value, output_language)
                for value in dimensions[:3]
            )
            if output_language == "en"
            else "、".join(
                _dimension_label(value, output_language)
                for value in dimensions[:3]
            )
        )
        if not focus:
            role = _message(
                output_language,
                "Timed evidence is still too sparse to assign a specific expressive role.",
                "这一时段的时间证据仍然不足，暂时不能赋予更具体的表达作用。",
            )
        elif len(sections) == 1:
            role = _message(
                output_language,
                f"Evidence here centers on {focus}; use it as one complete listening window rather than a confirmed formal section.",
                f"这一完整时段的证据主要集中在{focus}；应把它视为复听窗口，而不是已经确认的曲式段落。",
            )
        else:
            position = min(index, 2)
            role = (
                f"Changes in {focus} are most evident here; this passage "
                f"{roles_en[position]}. This is a listening function, not a "
                "confirmed formal label."
                if output_language == "en"
                else (
                    f"这一时段可确认的变化主要集中在{focus}；它在聆听进程中"
                    f"{roles_zh[position]}。这是基于时间证据的听觉作用，而不是"
                    "未经确认的曲式命名。"
                )
            )
        updated.append(section.model_copy(update={"expressive_role": role[:800]}))
    return updated


def _generic_observation(
    dimensions: list[AudioDimension],
    output_language: str,
) -> str:
    values = _dedupe(dimensions)[:3] or [AudioDimension.OTHER]
    labels = (
        ", ".join(_dimension_label(value, output_language) for value in values)
        if output_language == "en"
        else "、".join(_dimension_label(value, output_language) for value in values)
    )
    return _message(
        output_language,
        f"Timed evidence records changes in {labels} during this interval.",
        f"带时间的证据记录了这一时段在{labels}方面的变化。",
    )


def _generic_interpretation(
    dimensions: list[AudioDimension],
    output_language: str,
) -> str:
    values = _dedupe(dimensions)[:3] or [AudioDimension.OTHER]
    labels = (
        ", ".join(_dimension_label(value, output_language) for value in values)
        if output_language == "en"
        else "、".join(_dimension_label(value, output_language) for value in values)
    )
    return _message(
        output_language,
        f"Together, these changes may shape a listening impression led by {labels}; compare the surrounding passages before assigning one emotion.",
        f"这些变化可能共同形成以{labels}为重点的听觉感受；具体情绪仍应结合前后段落复听。",
    )


def _event_role(output_language: str) -> str:
    return _message(
        output_language,
        "This interval supplies local audible evidence for the work's unfolding; it does not prove one creative intention.",
        "这一时段为作品表达提供局部声音线索；现有证据不足以断言创作者的唯一意图。",
    )


def _localized_core_expression(
    summary: str,
    facts: list[SourceFact],
    output_language: str,
) -> str:
    if _matches_output_language(summary, output_language):
        return _first_sentence(summary)
    dimensions = _dedupe(fact.dimension for fact in facts)[:3]
    labels = (
        ", ".join(_dimension_label(value, output_language) for value in dimensions)
        if output_language == "en"
        else "、".join(_dimension_label(value, output_language) for value in dimensions)
    ) or _message(output_language, "timed sound changes", "带时间的声音变化")
    return _message(
        output_language,
        f"The clearest supported expression comes from the work's evolving {labels}.",
        f"现有证据最清楚支持的是作品在{labels}方面持续变化的听觉过程。",
    )


def _matches_output_language(text: str, output_language: str) -> bool:
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if output_language == "en":
        return cjk == 0 and latin >= 3
    return cjk >= 2 and not (latin >= 12 and latin > cjk * 2)


def _message(output_language: str, english: str, chinese: str) -> str:
    return english if output_language == "en" else chinese


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
