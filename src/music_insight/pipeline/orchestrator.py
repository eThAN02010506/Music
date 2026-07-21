from __future__ import annotations

from collections.abc import Awaitable, Callable
import inspect
import re

from music_insight.adapters.base import DspAdapter, UnifiedAudioAdapter
from music_insight.adapters.local_analysis import LocalEvidenceAnalysisAdapter
from music_insight.pipeline.fusion import FusionEngine
from music_insight.pipeline.preprocess import Preprocessor
from music_insight.schemas import (
    AnalysisResult,
    AsrResult,
    AudioAsset,
    DspResult,
    Evidence,
    EvidenceType,
    LiteraryResult,
)


class AnalysisOrchestrator:
    def __init__(
        self,
        unified: UnifiedAudioAdapter,
        dsp: DspAdapter,
        preprocessor: Preprocessor | None = None,
        fusion: FusionEngine | None = None,
    ) -> None:
        self.unified = unified
        self.dsp = dsp
        self.preprocessor = preprocessor or Preprocessor()
        self.fusion = fusion or FusionEngine()

    async def analyze(
        self,
        asset: AudioAsset,
        progress: Callable[[str, float, str], Awaitable[None] | None] | None = None,
    ) -> AnalysisResult:
        await self._notify(progress, "preprocessing", 0.08, "正在标准化音频")
        prepared = await self.preprocessor.prepare(asset)
        await self._notify(progress, "dsp", 0.18, "正在计算节拍、调性与能量")
        try:
            dsp_result = await self.dsp.analyze(prepared.original)
        except Exception as exc:
            dsp_result = DspResult(
                evidence=[self._error_evidence("dsp", "librosa DSP", exc)]
            )
        await self._notify(progress, "audio_analysis", 0.36, "模型正在分块聆听音频")
        try:
            unified_result = await self.unified.analyze(prepared.scene, dsp_result)
            asr_result = unified_result.asr
            scene_result = unified_result.scene
            literary_result = unified_result.literary
        except Exception as exc:
            model_source = getattr(self.unified, "source", "统一音频模型")
            error = self._error_evidence("omni", model_source, exc)
            asr_result = AsrResult(
                model=model_source,
                lyrics=[],
                evidence=[error],
            )
            scene_result = await LocalEvidenceAnalysisAdapter().analyze_scene(
                asset, asr_result, dsp_result
            )
            scene_result = scene_result.model_copy(
                update={"evidence": [*scene_result.evidence, error]}
            )
            literary_result = LiteraryResult(
                model="本地报告降级",
                themes=scene_result.themes,
                narrative=scene_result.narrative or "统一模型分析失败；本地 DSP 仍可用。",
                evidence=[error],
            )

        if prepared.evidence:
            asr_result = asr_result.model_copy(
                update={"evidence": [*asr_result.evidence, *prepared.evidence]}
            )
        asr_result = self._apply_language_gate(
            asr_result, asset.language_hint, "omni"
        )

        await self._notify(progress, "fusion", 0.94, "正在整理证据与生成报告")
        result = self.fusion.merge(
            asr=asr_result,
            scene=scene_result,
            literary=literary_result,
            dsp=dsp_result,
        )
        await self._notify(progress, "finalizing", 0.99, "正在完成报告")
        return result

    @staticmethod
    async def _notify(
        callback: Callable[[str, float, str], Awaitable[None] | None] | None,
        stage: str,
        progress: float,
        message: str,
    ) -> None:
        if callback is None:
            return
        result = callback(stage, progress, message)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _error_evidence(prefix: str, source: str, error: BaseException) -> Evidence:
        detail = str(error).strip() or error.__class__.__name__
        return Evidence(
            id=f"{prefix}.error",
            source=source,
            kind=EvidenceType.OBSERVED,
            text=f"{source} 调用失败：{detail[:600]}",
            confidence=0.0,
            metadata={"error_type": error.__class__.__name__},
        )

    @staticmethod
    def _apply_language_gate(
        result: AsrResult,
        language: str | None,
        prefix: str,
    ) -> AsrResult:
        if not language or not result.lyrics:
            return result
        text = " ".join(segment.text for segment in result.lyrics)
        latin_count = len(re.findall(r"[A-Za-z]", text))
        cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
        script_total = latin_count + cjk_count
        if language == "zh":
            matches_language = cjk_count >= 2 and cjk_count / max(1, script_total) >= 0.6
        elif language == "en":
            matches_language = latin_count >= 3 and latin_count / max(1, script_total) >= 0.7
        else:
            matches_language = True
        only_unclear = not text.replace("[unclear]", "").strip()
        if matches_language and not only_unclear:
            return result
        rejected = Evidence(
            id=f"{prefix}.transcript.rejected",
            source=result.model,
            kind=EvidenceType.OBSERVED,
            text=f"已拒绝与指定语言 {language} 不匹配或无法确认的歌词候选：{text[:300]}",
            confidence=0.0,
            metadata={"language_hint": language},
        )
        return result.model_copy(
            update={"lyrics": [], "evidence": [*result.evidence, rejected]}
        )
