from __future__ import annotations

from music_insight.adapters.base import AudioSceneAdapter
from music_insight.schemas import (
    AsrResult,
    AudioAsset,
    AudioSceneResult,
    DspResult,
    Evidence,
    EvidenceType,
)


class LocalEvidenceAnalysisAdapter(AudioSceneAdapter):
    """Conservative local synthesis used when the chat service is unavailable."""

    async def analyze_scene(
        self,
        asset: AudioAsset,
        lyrics: AsrResult | None,
        dsp: DspResult | None,
    ) -> AudioSceneResult:
        lyrics = lyrics or AsrResult(model="统一模型不可用", lyrics=[], evidence=[])
        dsp = dsp or DspResult()
        energy_values = [
            float(item.metadata.get("normalized_rms", 0.0))
            for item in dsp.energy_curve
        ]
        average_energy = sum(energy_values) / len(energy_values) if energy_values else None
        peak_energy = max(energy_values, default=None)

        themes = []
        if dsp.bpm and (dsp.bpm_confidence or 0.0) >= 0.35:
            themes.append("快速推进" if dsp.bpm >= 120 else "舒缓展开")
        if dsp.key and (dsp.key_confidence or 0.0) >= 0.25:
            themes.append("明亮倾向" if "major" in dsp.key else "内省倾向")

        tension_evidence = []
        for index, item in enumerate(dsp.energy_curve, start=1):
            energy = float(item.metadata.get("normalized_rms", 0.0))
            if energy < 0.3 and index not in {1, len(dsp.energy_curve)}:
                continue
            label = "张力增强" if energy >= 0.65 else "能量舒缓"
            tension_evidence.append(
                Evidence(
                    id=f"local.tension.{index}",
                    source="本地证据融合",
                    kind=EvidenceType.INTERPRETIVE,
                    text=f"{label}（依据归一化能量 {energy:.2f}）",
                    confidence=0.7,
                    span=item.span,
                    metadata={"normalized_rms": energy, "basis": "DSP energy"},
                )
            )

        lyric_status = (
            f"统一模型转写得到 {len(lyrics.lyrics)} 个歌词片段"
            if lyrics.lyrics
            else "统一模型未确认可靠歌词"
        )
        metric_parts = []
        if dsp.bpm:
            confidence = (
                f"（可信度 {dsp.bpm_confidence:.0%}）"
                if dsp.bpm_confidence is not None
                else ""
            )
            metric_parts.append(f"速度约 {dsp.bpm:.1f} BPM{confidence}")
        if dsp.key:
            confidence = (
                f"（可信度 {dsp.key_confidence:.0%}）"
                if dsp.key_confidence is not None
                else ""
            )
            metric_parts.append(f"整体调性估计为 {dsp.key}{confidence}")
        if average_energy is not None and peak_energy is not None:
            metric_parts.append(f"平均相对能量 {average_energy:.2f}，峰值 {peak_energy:.2f}")
        metrics = "；".join(metric_parts) or "确定性声学指标不足"
        narrative = (
            f"{lyric_status}。{metrics}。"
            "本报告仅根据统一模型歌词转写与本地 DSP 证据描述节奏、调性和能量变化；"
            "不对具体乐器、环境声或文学主题作无证据猜测。"
        )
        evidence = [
            Evidence(
                id="local.evidence.synthesis",
                source="本地证据融合",
                kind=EvidenceType.INTERPRETIVE,
                text=narrative,
                confidence=1.0,
                metadata={"external_model": lyrics.model},
            )
        ]
        return AudioSceneResult(
            model=f"{lyrics.model} + 本地证据融合",
            instruments=[],
            sound_events=[],
            emotion_timeline=tension_evidence,
            themes=themes,
            narrative=narrative,
            evidence=evidence,
        )
