from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4
import wave

from music_insight.async_utils import run_sync_settled
from music_insight.audio import AudioDurationExceededError
from music_insight.schemas import AudioAsset, Evidence, EvidenceType
from music_insight.storage.assets import content_cache_key


@dataclass(slots=True)
class PreparedAudio:
    original: AudioAsset
    scene: AudioAsset | None
    evidence: list[Evidence] = field(default_factory=list)


class Preprocessor:
    CACHE_FORMAT_VERSION = "v2"

    def __init__(self, workspace_dir: Path = Path(".music_insight")) -> None:
        self.workspace_dir = workspace_dir

    async def prepare(self, asset: AudioAsset) -> PreparedAudio:
        try:
            scene_path = await run_sync_settled(
                self._normalize_for_omni,
                asset.path,
                asset.max_duration_s,
            )
            scene_asset = AudioAsset(
                path=scene_path,
                media_type="audio/wav",
                size_bytes=scene_path.stat().st_size,
                language_hint=asset.language_hint,
                max_duration_s=asset.max_duration_s,
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
        except AudioDurationExceededError:
            raise
        except Exception as exc:
            scene_asset = None
            evidence = [
                Evidence(
                    id="preprocess.omni.error",
                    source="本地音频预处理",
                    kind=EvidenceType.COMPUTED,
                    text=f"音频标准化失败，已跳过统一模型：{str(exc)[:500]}",
                    confidence=0.0,
                    metadata={"error_type": exc.__class__.__name__},
                )
            ]
        return PreparedAudio(original=asset, scene=scene_asset, evidence=evidence)

    def _normalize_for_omni(
        self,
        path: Path,
        max_duration_s: float | None = None,
    ) -> Path:
        digest = content_cache_key(path)
        output_dir = (
            self.workspace_dir
            / "normalized"
            / f"{self.CACHE_FORMAT_VERSION}-{digest}"
        )
        output_path = output_dir / "omni_input.wav"
        try:
            if self._valid_cached_wav(output_path, max_duration_s):
                return output_path
        except OSError:
            pass

        import soundfile as sf
        from music_insight.audio import decode_mono

        mono, sample_rate = decode_mono(
            path,
            sample_rate=16_000,
            max_duration_s=max_duration_s,
        )
        if not mono.size:
            raise ValueError("原音频为空，无法标准化。")
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        output_dir.chmod(0o700)
        temporary_path = output_dir / (
            f".{output_path.name}.{uuid4().hex}.tmp.wav"
        )
        try:
            sf.write(
                temporary_path,
                mono,
                sample_rate,
                subtype="PCM_16",
                format="WAV",
            )
            if temporary_path.stat().st_size <= 44:
                raise ValueError("标准化音频写入不完整。")
            temporary_path.chmod(0o600)
            # Every producer writes a private complete file. ``replace`` is
            # atomic, so a concurrent cache reader can observe only an old
            # complete WAV or the new complete WAV, never an in-progress file.
            temporary_path.replace(output_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return output_path

    @staticmethod
    def _valid_cached_wav(
        path: Path,
        max_duration_s: float | None = None,
    ) -> bool:
        if not path.is_file() or path.stat().st_size <= 44:
            return False
        try:
            with wave.open(str(path), "rb") as source:
                valid = (
                    source.getnchannels() == 1
                    and source.getsampwidth() == 2
                    and source.getframerate() == 16_000
                    and source.getnframes() > 0
                )
                if not valid:
                    return False
                return (
                    max_duration_s is None
                    or source.getnframes() / source.getframerate()
                    <= max_duration_s
                )
        except (OSError, EOFError, wave.Error):
            return False
