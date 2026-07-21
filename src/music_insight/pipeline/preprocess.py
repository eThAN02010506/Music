from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
from pathlib import Path

from music_insight.schemas import AudioAsset, Evidence, EvidenceType


@dataclass(slots=True)
class PreparedAudio:
    original: AudioAsset
    scene: AudioAsset
    evidence: list[Evidence] = field(default_factory=list)


class Preprocessor:
    def __init__(self, workspace_dir: Path = Path(".music_insight")) -> None:
        self.workspace_dir = workspace_dir

    async def prepare(self, asset: AudioAsset) -> PreparedAudio:
        try:
            scene_path = await asyncio.to_thread(self._normalize_for_omni, asset.path)
            scene_asset = AudioAsset(
                path=scene_path,
                media_type="audio/wav",
                size_bytes=scene_path.stat().st_size,
                language_hint=asset.language_hint,
            )
            evidence = [
                Evidence(
                    id="preprocess.omni.wav",
                    source="本地音频预处理",
                    kind=EvidenceType.COMPUTED,
                    text="已生成 16 kHz 单声道 WAV，供统一模型分块分析。",
                    confidence=1.0,
                    metadata={"cached_path": str(scene_path)},
                )
            ]
        except Exception as exc:
            scene_asset = asset
            evidence = [
                Evidence(
                    id="preprocess.omni.error",
                    source="本地音频预处理",
                    kind=EvidenceType.COMPUTED,
                    text=f"音频标准化失败，统一模型将尝试原文件：{str(exc)[:500]}",
                    confidence=0.0,
                    metadata={"error_type": exc.__class__.__name__},
                )
            ]
        return PreparedAudio(original=asset, scene=scene_asset, evidence=evidence)

    def _normalize_for_omni(self, path: Path) -> Path:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:20]
        output_dir = self.workspace_dir / "normalized" / digest
        output_path = output_dir / "omni_input.wav"
        if output_path.exists() and output_path.stat().st_size > 44:
            return output_path

        import soundfile as sf
        from music_insight.audio import decode_mono

        mono, sample_rate = decode_mono(path, sample_rate=16_000)
        if not mono.size:
            raise ValueError("原音频为空，无法标准化。")
        output_dir.mkdir(parents=True, exist_ok=True)
        sf.write(output_path, mono, sample_rate, subtype="PCM_16")
        return output_path
