from __future__ import annotations

import numpy as np
import librosa

from music_insight.async_utils import run_sync_settled
from music_insight.adapters.base import DspAdapter
from music_insight.audio import decode_mono
from music_insight.schemas import AudioAsset, DspResult, Evidence, EvidenceType, TimeSpan


class BasicDspAdapter(DspAdapter):
    """Deterministic BPM, key, and energy analysis backed by librosa."""

    async def analyze(self, asset: AudioAsset) -> DspResult:
        return await run_sync_settled(self._analyze_sync, asset)

    def _analyze_sync(self, asset: AudioAsset) -> DspResult:
        audio, sample_rate = decode_mono(
            asset.path,
            max_duration_s=asset.max_duration_s,
        )
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
        bpm_candidates: list[float] = []
        bpm_ambiguous = False
        octave_support_ratio: float | None = None
        analysis_windows = self._analysis_windows(audio, sample_rate)
        if peak_rms > 1e-7 and duration >= 2.0:
            onset_parts = [
                librosa.onset.onset_strength(y=part, sr=sample_rate)
                for part in analysis_windows
            ]
            onset_envelope = np.concatenate(onset_parts)
            tempogram_parts = [
                librosa.feature.tempogram(
                    onset_envelope=part,
                    sr=sample_rate,
                )
                for part in onset_parts
                if part.size >= 4
            ]
            tempogram = (
                np.concatenate(tempogram_parts, axis=1)
                if tempogram_parts
                else np.empty((0, 0))
            )
            tempo = librosa.feature.tempo(
                tg=tempogram,
                sr=sample_rate,
                aggregate=np.median,
            ) if tempogram.size else np.asarray([0.0])
            tempo_value = float(np.asarray(tempo).reshape(-1)[0])
            if np.isfinite(tempo_value) and tempo_value > 0:
                pulse = np.mean(tempogram, axis=1)
                tempo_bins = librosa.tempo_frequencies(
                    len(pulse),
                    sr=sample_rate,
                )
                (
                    tempo_value,
                    bpm_candidates,
                    bpm_ambiguous,
                    octave_support_ratio,
                ) = self._resolve_tempo_octave(
                    tempo_value,
                    pulse,
                    tempo_bins,
                )
                bpm = round(tempo_value, 1)
                bpm_confidence = self._tempo_confidence(
                    pulse,
                    tempo_bins,
                    onset_envelope,
                )

        key, key_confidence = (
            self._estimate_key(analysis_windows, sample_rate)
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
                    "bpm_candidates": bpm_candidates,
                    "bpm_ambiguous": bpm_ambiguous,
                    "octave_support_ratio": octave_support_ratio,
                    "key_confidence": key_confidence,
                },
            )
        ]
        return DspResult(
            bpm=bpm,
            bpm_confidence=bpm_confidence,
            bpm_candidates=bpm_candidates,
            bpm_ambiguous=bpm_ambiguous,
            key=key,
            key_confidence=key_confidence,
            energy_curve=energy_curve,
            evidence=evidence,
        )

    @classmethod
    def _resolve_tempo_octave(
        cls,
        tempo: float,
        pulse: np.ndarray,
        tempo_bins: np.ndarray,
    ) -> tuple[float, list[float], bool, float | None]:
        """Prefer the perceptual half-time pulse when octave candidates are tied.

        Beat trackers frequently report the subdivision rate of a slow song.  We
        only fold a fast estimate in half when the half-time pulse is in the
        common tapping range and has nearly the same tempogram support.
        """
        rounded = round(float(tempo), 1)
        if tempo < 135.0 or tempo > 220.0:
            return tempo, [rounded], False, None

        half_tempo = tempo / 2.0
        if half_tempo < 55.0 or half_tempo > 110.0:
            return tempo, [rounded], False, None

        primary_support = cls._pulse_support(
            pulse,
            tempo_bins,
            tempo,
        )
        half_support = cls._pulse_support(
            pulse,
            tempo_bins,
            half_tempo,
        )
        if primary_support <= 1e-9:
            return tempo, [rounded], False, None

        support_ratio = half_support / primary_support
        if support_ratio < 0.92:
            return tempo, [rounded], False, round(support_ratio, 3)

        return (
            half_tempo,
            [round(half_tempo, 1), rounded],
            True,
            round(support_ratio, 3),
        )

    @staticmethod
    def _pulse_support(
        pulse: np.ndarray,
        tempo_bins: np.ndarray,
        tempo: float,
    ) -> float:
        if pulse.size < 4:
            return 0.0
        valid = np.flatnonzero(np.isfinite(tempo_bins) & (tempo_bins > 0))
        if not valid.size:
            return 0.0
        distances = np.abs(np.log2(tempo_bins[valid] / tempo))
        return float(pulse[valid[int(np.argmin(distances))]])

    @staticmethod
    def _tempo_confidence(
        pulse: np.ndarray,
        tempo_bins: np.ndarray,
        onset_envelope: np.ndarray,
    ) -> float:
        if onset_envelope.size < 4 or float(np.max(onset_envelope)) <= 1e-9:
            return 0.0
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
        windows: list[np.ndarray],
        sample_rate: int,
    ) -> tuple[str | None, float | None]:
        profiles = []
        for audio in windows:
            harmonic = librosa.effects.harmonic(audio)
            chroma = librosa.feature.chroma_cqt(
                y=harmonic,
                sr=sample_rate,
            )
            profiles.append(np.mean(chroma, axis=1))
        profile = np.mean(profiles, axis=0)
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

    @staticmethod
    def _analysis_windows(
        audio: np.ndarray,
        sample_rate: int,
        *,
        window_seconds: float = 20.0,
        max_windows: int = 6,
    ) -> list[np.ndarray]:
        """Sample bounded, continuous windows across the full recording.

        Tempo and key should represent the whole song, but full-length HPSS and
        tempograms have memory proportional to duration. Evenly spaced windows
        retain beginning/middle/end evidence with a fixed memory ceiling.
        """

        window_samples = max(1, int(window_seconds * sample_rate))
        if audio.size <= window_samples:
            return [audio]
        available = audio.size - window_samples
        count = min(max_windows, max(1, int(np.ceil(audio.size / window_samples))))
        starts = np.linspace(0, available, num=count, dtype=int)
        return [
            audio[start : start + window_samples]
            for start in starts
        ]
