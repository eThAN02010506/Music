from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
import inspect
import re

from music_insight.adapters.base import (
    AsrVerifier,
    DspAdapter,
    UnifiedAudioAdapter,
    VerifiedLyricsSynthesisAdapter,
)
from music_insight.adapters.local_analysis import LocalEvidenceAnalysisAdapter
from music_insight.pipeline.asr_verification import AsrVerificationFusion
from music_insight.pipeline.fusion import FusionEngine
from music_insight.pipeline.preprocess import PreparedAudio, Preprocessor
from music_insight.schemas import (
    AnalysisResult,
    AsrResult,
    AudioAsset,
    AudioSceneResult,
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
        dsp_gate: AbstractAsyncContextManager[None] | None = None,
        model_gate: AbstractAsyncContextManager[None] | None = None,
        asr_verifier: AsrVerifier | None = None,
        asr_verification_fusion: AsrVerificationFusion | None = None,
        asr_gate: AbstractAsyncContextManager[None] | None = None,
    ) -> None:
        self.unified = unified
        self.dsp = dsp
        self.preprocessor = preprocessor or Preprocessor()
        self.fusion = fusion or FusionEngine()
        self.dsp_gate = dsp_gate
        self.model_gate = model_gate
        self.asr_verifier = asr_verifier
        self.asr_verification_fusion = (
            asr_verification_fusion or AsrVerificationFusion()
        )
        self.asr_gate = asr_gate

    async def analyze(
        self,
        asset: AudioAsset,
        progress: Callable[[str, float, str], Awaitable[None] | None] | None = None,
    ) -> AnalysisResult:
        prepared = await self._prepare_audio(asset, progress)
        dsp_result = await self._analyze_dsp(prepared, progress)
        asr_result, scene_result, literary_result = (
            await self._analyze_unified(
                asset,
                prepared,
                dsp_result,
                progress,
            )
        )
        if prepared.evidence:
            asr_result = asr_result.model_copy(
                update={"evidence": [*asr_result.evidence, *prepared.evidence]}
            )
        asr_result = self._apply_language_gate(
            asr_result,
            asset.language_hint,
            "omni",
        )
        asr_result, scene_result, literary_result = (
            await self._verify_lyrics(
                asset=asset,
                prepared=prepared,
                asr=asr_result,
                scene=scene_result,
                literary=literary_result,
                dsp=dsp_result,
                progress=progress,
            )
        )
        await self._notify(progress, "fusion", 0.97, "正在整理模型与 DSP 证据")
        result = self.fusion.merge(
            asr=asr_result,
            scene=scene_result,
            literary=literary_result,
            dsp=dsp_result,
        )
        await self._notify(progress, "finalizing", 0.99, "正在完成报告")
        return result

    async def _prepare_audio(
        self,
        asset: AudioAsset,
        progress: Callable[[str, float, str], Awaitable[None] | None] | None,
    ) -> PreparedAudio:
        await self._notify(progress, "preprocessing", 0.08, "正在标准化音频")
        if self.dsp_gate is None:
            return await self.preprocessor.prepare(asset)
        async with self.dsp_gate:
            return await self.preprocessor.prepare(asset)

    async def _analyze_dsp(
        self,
        prepared: PreparedAudio,
        progress: Callable[[str, float, str], Awaitable[None] | None] | None,
    ) -> DspResult:
        await self._notify(progress, "dsp", 0.18, "正在计算节拍、调性与能量")
        try:
            if self.dsp_gate is None:
                return await self.dsp.analyze(prepared.original)
            async with self.dsp_gate:
                return await self.dsp.analyze(prepared.original)
        except Exception as exc:
            return DspResult(
                evidence=[self._error_evidence("dsp", "librosa DSP", exc)]
            )

    async def _analyze_unified(
        self,
        asset: AudioAsset,
        prepared: PreparedAudio,
        dsp_result: DspResult,
        progress: Callable[[str, float, str], Awaitable[None] | None] | None,
    ) -> tuple[AsrResult, AudioSceneResult, LiteraryResult]:
        await self._notify(progress, "model_queue", 0.32, "正在等待模型资源")
        try:
            if prepared.scene is None:
                raise RuntimeError(
                    "音频标准化失败；统一模型仅接收标准 WAV，未发送原始文件。"
                )

            async def model_progress(
                stage: str, model_progress: float, message: str
            ) -> None:
                overall = 0.36 + max(0.0, min(model_progress, 1.0)) * 0.56
                await self._notify(progress, stage, overall, message)

            if self.model_gate is None:
                unified_result = await self.unified.analyze(
                    prepared.scene, dsp_result, progress=model_progress
                )
            else:
                async with self.model_gate:
                    await self._notify(
                        progress, "audio_analysis", 0.36, "已获得模型资源，开始聆听"
                    )
                    unified_result = await self.unified.analyze(
                        prepared.scene, dsp_result, progress=model_progress
                    )
            return (
                unified_result.asr,
                unified_result.scene,
                unified_result.literary,
            )
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
            return asr_result, scene_result, literary_result

    async def _verify_lyrics(
        self,
        *,
        asset: AudioAsset,
        prepared: PreparedAudio,
        asr: AsrResult,
        scene: AudioSceneResult,
        literary: LiteraryResult,
        dsp: DspResult,
        progress: Callable[[str, float, str], Awaitable[None] | None] | None,
    ) -> tuple[AsrResult, AudioSceneResult, LiteraryResult]:
        if self.asr_verifier is None:
            return asr, scene, literary
        await self._notify(
            progress,
            "asr_verification",
            0.93,
            "正在使用专用 ASR 二次校验歌词时间轴",
        )
        try:
            if prepared.scene is None:
                raise RuntimeError("标准化音频不可用，无法执行歌词二次验证。")
            if self.asr_gate is None:
                verification = await self.asr_verifier.verify(prepared.scene)
            else:
                async with self.asr_gate:
                    verification = await self.asr_verifier.verify(
                        prepared.scene
                    )
            decision = self.asr_verification_fusion.evaluate(asr, verification)
        except Exception as exc:
            unavailable = self.asr_verification_fusion.mark_unavailable(
                asr,
                source=self.asr_verifier.source,
                error=exc,
            )
            return unavailable, scene, literary
        asr, refresh_report = self._apply_verification_decision(
            decision,
            language_hint=asset.language_hint,
        )
        if not refresh_report:
            return asr, scene, literary
        await self._notify(
            progress,
            "verified_lyrics_synthesis",
            0.945,
            "正在依据已验证歌词更新主题与综合解释",
        )
        scene, literary = await self._refresh_lyrics_dependent_report(
            lyrics=asr,
            scene=scene,
            literary=literary,
            dsp=dsp,
        )
        return asr, scene, literary

    def _apply_verification_decision(
        self,
        decision,
        *,
        language_hint: str | None,
    ) -> tuple[AsrResult, bool]:
        refresh_report = (
            decision.status == "verified_silence"
            and self._lyrics_differ(decision.primary, decision.result)
        )
        if not decision.candidate_applied:
            return decision.result, refresh_report
        language_checked = self._apply_language_gate(
            decision.result,
            language_hint,
            "asr.verifier",
        )
        if decision.result.lyrics and not language_checked.lyrics:
            rejected = self.asr_verification_fusion.reject_candidate_language(
                decision,
                language_checked,
            )
            return rejected, False
        finalized = self.asr_verification_fusion.finalize_candidate(decision)
        refresh_report = (
            decision.replaces_primary
            and self._lyrics_differ(decision.primary, finalized)
        )
        return finalized, refresh_report

    async def _refresh_lyrics_dependent_report(
        self,
        *,
        lyrics: AsrResult,
        scene: AudioSceneResult,
        literary: LiteraryResult,
        dsp: DspResult,
    ) -> tuple[AudioSceneResult, LiteraryResult]:
        try:
            if not isinstance(self.unified, VerifiedLyricsSynthesisAdapter):
                raise RuntimeError(
                    "当前统一模型不支持已验证歌词重新综合。"
                )
            if self.model_gate is None:
                refreshed = await self.unified.resynthesize_verified_lyrics(
                    lyrics.lyrics,
                    scene,
                    dsp,
                )
            else:
                async with self.model_gate:
                    refreshed = (
                        await self.unified.resynthesize_verified_lyrics(
                            lyrics.lyrics,
                            scene,
                            dsp,
                        )
                    )
        except Exception as exc:
            return self._sanitize_lyrics_dependent_report(
                scene=scene,
                source=getattr(self.unified, "source", "统一音频模型"),
                error=exc,
            )

        retained_scene_evidence = self._consistency_safe_scene_evidence(
            scene
        )
        refreshed_scene = scene.model_copy(
            update={
                "inferred_atmosphere": refreshed.inferred_atmosphere,
                "themes": [],
                "narrative": None,
                "evidence": [
                    *retained_scene_evidence,
                    *refreshed.evidence,
                ],
            }
        )
        return refreshed_scene, refreshed.literary

    @staticmethod
    def _sanitize_lyrics_dependent_report(
        *,
        scene: AudioSceneResult,
        source: str,
        error: BaseException,
    ) -> tuple[AudioSceneResult, LiteraryResult]:
        consistency = Evidence(
            id="asr.verifier.consistency.unavailable",
            source=source,
            kind=EvidenceType.OBSERVED,
            text=(
                "歌词已由二次 ASR 更新，但统一模型未能基于新歌词重新综合；"
                "已移除旧歌词支撑的主题、意境和综合解释。"
            ),
            confidence=0.0,
            metadata={"error_type": error.__class__.__name__},
        )
        retained_scene_evidence = (
            AnalysisOrchestrator._consistency_safe_scene_evidence(scene)
        )
        safe_scene = scene.model_copy(
            update={
                "inferred_atmosphere": [],
                "themes": [],
                "narrative": None,
                "evidence": retained_scene_evidence,
            }
        )
        safe_literary = LiteraryResult(
            model="歌词一致性安全降级",
            themes=[],
            narrative=(
                "歌词已完成二次校验。由于无法安全地基于新歌词重新综合，"
                "原歌词相关主题结论已移除；节拍、调性、配器和声音事件仍可参考。"
            ),
            evidence=[consistency],
        )
        return safe_scene, safe_literary

    @staticmethod
    def _lyrics_differ(first: AsrResult, second: AsrResult) -> bool:
        return [
            item.model_dump(mode="json") for item in first.lyrics
        ] != [
            item.model_dump(mode="json") for item in second.lyrics
        ]

    @staticmethod
    def _consistency_safe_scene_evidence(
        scene: AudioSceneResult,
    ) -> list[Evidence]:
        lyric_sensitive_markers = (
            ".analysis",
            ".quality_retry",
            ".recovery",
        )
        return [
            item
            for item in scene.evidence
            if not item.id.startswith("omni.final.")
            and not (
                item.id.startswith("omni.chunk.")
                and any(
                    marker in item.id
                    for marker in lyric_sensitive_markers
                )
            )
        ]

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
