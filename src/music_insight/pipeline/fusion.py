from music_insight.schemas import (
    AnalysisResult,
    AsrResult,
    AudioSceneResult,
    DspResult,
    Evidence,
    LiteraryResult,
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

        warnings = []
        if not asr.lyrics:
            warnings.append(
                "统一模型未能确认可靠歌词；声景与本地 DSP 分析仍可用。"
            )
        error_evidence = [item for item in evidence if item.id.endswith(".error")]
        if error_evidence:
            warnings.append(
                "部分模块调用失败，已返回其余可用结果："
                + "；".join(item.text for item in error_evidence)
            )
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
            warnings.append(
                "质量提示："
                + "；".join(item.text for item in unavailable_evidence)
            )

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
            warnings=warnings,
        )
