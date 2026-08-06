from music_insight.schemas import (
    AnalysisResult,
    AsrResult,
    AudioSceneResult,
    DspResult,
    Evidence,
    LiteraryResult,
    VocalPresenceResult,
    VocalPresenceStatus,
)


class FusionEngine:
    def merge(
        self,
        asr: AsrResult,
        scene: AudioSceneResult,
        literary: LiteraryResult,
        dsp: DspResult,
    ) -> AnalysisResult:
        evidence_candidates: list[Evidence] = [
            *asr.evidence,
            *scene.evidence,
            *scene.sound_events,
            *scene.emotion_timeline,
            *scene.inferred_atmosphere,
            *literary.evidence,
            *dsp.evidence,
            *dsp.energy_curve,
        ]
        evidence = list({item.id: item for item in evidence_candidates}.values())
        vocal_presence = _resolve_vocal_presence(asr, scene, evidence)

        warnings = []
        if not asr.lyrics:
            if vocal_presence.status is VocalPresenceStatus.UNKNOWN:
                warnings.append(
                    "未确认可靠歌词，也没有足够证据断言为纯器乐；"
                    "声景与本地 DSP 分析仍可用。"
                )
            elif vocal_presence.status is VocalPresenceStatus.VOCALS:
                warnings.append(
                    "音频证据提示存在人声，但尚未确认可靠歌词；"
                    "不会根据人声存在自动补写歌词。"
                )
        error_evidence = [item for item in evidence if item.id.endswith(".error")]
        if error_evidence:
            warnings.append(_summarize_error_warning(error_evidence))
        rejected_evidence = [item for item in evidence if item.id.endswith(".rejected")]
        if rejected_evidence:
            warnings.append(
                "歌词质量检查未通过："
                + "；".join(item.text for item in rejected_evidence)
            )
        unavailable_evidence = [
            item for item in evidence if item.id.endswith(".unavailable")
        ]
        if unavailable_evidence:
            warnings.append(_summarize_quality_warning(unavailable_evidence))
        inconclusive_evidence = [
            item for item in evidence if item.id.endswith(".inconclusive")
        ]
        if inconclusive_evidence:
            warnings.append(_summarize_quality_warning(inconclusive_evidence))

        return AnalysisResult(
            summary=literary.narrative,
            lyrics=asr.lyrics,
            instruments=scene.instruments,
            sound_events=scene.sound_events,
            emotion_timeline=scene.emotion_timeline,
            inferred_atmosphere=scene.inferred_atmosphere,
            themes=literary.themes,
            technical_metrics=dsp,
            evidence=evidence,
            vocal_presence=vocal_presence,
            warnings=warnings,
        )


def _resolve_vocal_presence(
    asr: AsrResult,
    scene: AudioSceneResult,
    evidence: list[Evidence],
) -> VocalPresenceResult:
    verified_silence = next(
        (
            item
            for item in evidence
            if item.id == "asr.verifier.verified_silence"
            and item.confidence is not None
            and item.confidence >= 0.8
        ),
        None,
    )
    if verified_silence is not None:
        return VocalPresenceResult(
            status=VocalPresenceStatus.INSTRUMENTAL,
            confidence=verified_silence.confidence,
            reason="专用 ASR 提供了高置信无人声证据。",
            evidence_ids=[verified_silence.id],
        )

    if asr.lyrics:
        known_confidences = [
            lyric.confidence
            for lyric in asr.lyrics
            if lyric.confidence is not None
        ]
        confidence = (
            sum(known_confidences) / len(known_confidences)
            if known_confidences
            else 0.7
        )
        transcript_ids = [
            item.id
            for item in asr.evidence
            if item.id.startswith(("omni.transcript", "asr.verifier.verified"))
        ]
        return VocalPresenceResult(
            status=VocalPresenceStatus.VOCALS,
            confidence=confidence,
            reason="已确认到通过质量检查的歌词片段。",
            evidence_ids=transcript_ids,
        )

    scene_evidence = next(
        (item for item in evidence if item.id == "omni.vocal_presence"),
        None,
    )
    if (
        scene.vocals_detected is True
        and scene.vocal_confidence is not None
        and scene.vocal_confidence >= 0.6
    ):
        return VocalPresenceResult(
            status=VocalPresenceStatus.VOCALS,
            confidence=scene.vocal_confidence,
            reason="逐块音频分析至少在一个时段检测到人声。",
            evidence_ids=[scene_evidence.id] if scene_evidence else [],
        )
    if (
        scene.vocals_detected is False
        and scene.vocal_confidence is not None
        and scene.vocal_confidence >= 0.75
    ):
        return VocalPresenceResult(
            status=VocalPresenceStatus.INSTRUMENTAL,
            confidence=scene.vocal_confidence,
            reason="逐块音频分析在足够覆盖范围内一致报告无人声。",
            evidence_ids=[scene_evidence.id] if scene_evidence else [],
        )
    return VocalPresenceResult()


def _summarize_quality_warning(values: list[Evidence]) -> str:
    """Collapse repeated per-chunk diagnostics into one bounded user warning."""

    unique_texts: list[str] = []
    for item in values:
        text = item.text.strip()
        if text and text not in unique_texts:
            unique_texts.append(text)
    chunk_count = sum(".chunk." in item.id for item in values)
    summary = "；".join(unique_texts[:3]) or "部分质量检查没有得到确定结论。"
    if chunk_count:
        summary += f"（涉及 {chunk_count} 个音频分块）"
    if len(unique_texts) > 3:
        summary += f"；另有 {len(unique_texts) - 3} 类提示"
    return "质量提示：" + summary


def _summarize_error_warning(values: list[Evidence]) -> str:
    """Collapse per-chunk model failures by error type instead of by index.

    Chunk error text embeds the chunk number (``第 N 个音频分块分析失败``), so
    plain text deduplication never collapses 12 identical failures. Grouping by
    the recorded ``error_type`` produces one readable warning with a chunk
    count, matching FR-PV-006.
    """

    by_type: dict[str, list[Evidence]] = {}
    for item in values:
        error_type = item.metadata.get("error_type") or "unknown"
        by_type.setdefault(str(error_type), []).append(item)
    parts: list[str] = []
    for error_type, items in sorted(by_type.items()):
        chunk_count = sum(".chunk." in item.id for item in items)
        sample = next(
            (item.text for item in items if item.text.strip()),
            error_type,
        )
        suffix = f"（涉及 {chunk_count} 个音频分块）" if chunk_count > 1 else ""
        parts.append(f"{sample}{suffix}")
    summary = "；".join(parts[:5])
    if len(parts) > 5:
        summary += f"；另有 {len(parts) - 5} 类失败原因"
    return "部分模块调用失败，已返回其余可用结果：" + summary
