from __future__ import annotations

import numpy as np
import librosa

from music_insight.adapters.base import DspAdapter
from music_insight.audio import decode_mono
from music_insight.schemas import AudioAsset, DspResult, Evidence, EvidenceType, TimeSpan


class BasicDspAdapter(DspAdapter):
    """Deterministic BPM, key, and energy analysis backed by librosa."""

    async def analyze(self, asset: AudioAsset) -> DspResult:
        audio, sample_rate = decode_mono(asset.path)
        if not audio.size:
            raise ValueError("音频为空，无法计算 DSP 指标。")

        duration = len(audio) / sample_rate
        rms = librosa.feature.rms(y=audio, frame_length=2048, hop_length=512)[0]
        times = librosa.frames_to_time(np.arange(len(rms)), sr=sample_rate, hop_length=512)
        peak_rms = float(np.max(rms)) if rms.size else 0.0

        energy_curve: list[Evidence] = []
        bucket_count = min(24, max(1, int(np.ceil(duration / 2.0))))
        for index, bucket in enumerate(np.array_split(np.arange(len(rms)), bucket_count), start=1):
            if not bucket.size:
                continue
            value = float(np.mean(rms[bucket]))
            normalized = value / peak_rms if peak_rms > 1e-9 else 0.0
            start_s = float(times[bucket[0]])
            end_s = min(duration, float(times[bucket[-1]] + 512 / sample_rate))
            energy_curve.append(
                Evidence(
                    id=f"dsp.energy.{index}",
                    source="librosa DSP",
                    kind=EvidenceType.COMPUTED,
                    text=f"能量 {normalized:.2f}",
                    confidence=1.0,
                    span=TimeSpan(start_s=start_s, end_s=max(start_s, end_s)),
                    metadata={"normalized_rms": normalized, "rms": value},
                )
            )

        bpm: float | None = None
        bpm_confidence: float | None = None
        if peak_rms > 1e-7 and duration >= 2.0:
            onset_envelope = librosa.onset.onset_strength(y=audio, sr=sample_rate)
            tempo = librosa.feature.tempo(
                onset_envelope=onset_envelope,
                sr=sample_rate,
                aggregate=np.median,
            )
            tempo_value = float(np.asarray(tempo).reshape(-1)[0])
            if np.isfinite(tempo_value) and tempo_value > 0:
                bpm = round(tempo_value, 1)
                bpm_confidence = self._tempo_confidence(onset_envelope, sample_rate)

        key, key_confidence = (
            self._estimate_key(audio, sample_rate)
            if peak_rms > 1e-7
            else (None, None)
        )
        bpm_text = (
            f"{bpm} (confidence {bpm_confidence:.2f})"
            if bpm is not None and bpm_confidence is not None
            else "无法确认"
        )
        key_text = (
            f"{key} (confidence {key_confidence:.2f})"
            if key is not None and key_confidence is not None
            else "无法确认"
        )
        metric_confidences = [
            value
            for value in (bpm_confidence, key_confidence)
            if value is not None
        ]
        evidence = [
            Evidence(
                id="dsp.metrics",
                source="librosa DSP",
                kind=EvidenceType.COMPUTED,
                text=f"时长 {duration:.2f}s；BPM {bpm_text}；调性 {key_text}",
                confidence=min(metric_confidences) if metric_confidences else None,
                metadata={
                    "duration_s": duration,
                    "sample_rate": sample_rate,
                    "bpm_confidence": bpm_confidence,
                    "key_confidence": key_confidence,
                },
            )
        ]
        return DspResult(
            bpm=bpm,
            bpm_confidence=bpm_confidence,
            key=key,
            key_confidence=key_confidence,
            energy_curve=energy_curve,
            evidence=evidence,
        )

    @staticmethod
    def _tempo_confidence(onset_envelope: np.ndarray, sample_rate: int) -> float:
        if onset_envelope.size < 4 or float(np.max(onset_envelope)) <= 1e-9:
            return 0.0
        tempogram = librosa.feature.tempogram(
            onset_envelope=onset_envelope,
            sr=sample_rate,
        )
        pulse = np.mean(tempogram, axis=1)
        tempo_bins = librosa.tempo_frequencies(len(pulse), sr=sample_rate)
        valid = np.isfinite(tempo_bins) & (tempo_bins >= 30) & (tempo_bins <= 300)
        plausible_pulse = pulse[valid]
        if plausible_pulse.size < 2 or float(np.max(plausible_pulse)) <= 1e-9:
            return 0.0
        ranked = np.sort(plausible_pulse)
        prominence = (ranked[-1] - ranked[-2]) / max(ranked[-1], 1e-9)
        onset_density = float(np.count_nonzero(onset_envelope > np.mean(onset_envelope)))
        density_factor = min(1.0, onset_density / 8.0)
        return round(min(1.0, max(0.0, prominence * 4.0 * density_factor)), 3)

    @staticmethod
    def _estimate_key(
        audio: np.ndarray, sample_rate: int
    ) -> tuple[str | None, float | None]:
        chroma = librosa.feature.chroma_cqt(y=audio, sr=sample_rate)
        profile = np.mean(chroma, axis=1)
        if not np.any(np.isfinite(profile)) or float(np.sum(profile)) <= 1e-9:
            return None, None
        profile = profile / np.linalg.norm(profile)
        major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
        minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
        major /= np.linalg.norm(major)
        minor /= np.linalg.norm(minor)
        scores = []
        for root in range(12):
            scores.append((float(np.dot(profile, np.roll(major, root))), root, "major"))
            scores.append((float(np.dot(profile, np.roll(minor, root))), root, "minor"))
        ranked = sorted(scores, reverse=True)
        best_score, root, mode = ranked[0]
        margin = best_score - ranked[1][0]
        confidence = round(min(1.0, max(0.0, margin / 0.08)), 3)
        names = ["C", "C♯", "D", "E♭", "E", "F", "F♯", "G", "A♭", "A", "B♭", "B"]
        return f"{names[root]} {mode}", confidence
