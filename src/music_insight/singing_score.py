from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import librosa
import numpy as np
from pydantic import BaseModel, Field

from music_insight.audio import decode_mono


class PitchPoint(BaseModel):
    progress: float = Field(ge=0, le=1)
    reference_midi: float | None = None
    performance_midi: float | None = None
    error_semitones: float | None = None


class SingingScore(BaseModel):
    total: int = Field(ge=0, le=100)
    pitch: int = Field(ge=0, le=100)
    rhythm: int = Field(ge=0, le=100)
    completeness: int = Field(ge=0, le=100)
    stability: int = Field(ge=0, le=100)
    median_pitch_error: float | None = None
    in_tune_ratio: float | None = None
    reference_duration_s: float
    performance_duration_s: float
    pitch_curve: list[PitchPoint]
    notes: list[str]


def score_singing(reference_path: Path, performance_path: Path) -> SingingScore:
    reference = _features(str(reference_path.resolve()), reference_path.stat().st_mtime_ns)
    performance = _extract_features(performance_path)
    reference_pitch, reference_onset, reference_duration, _ = reference
    performance_pitch, performance_onset, performance_duration, voiced = performance

    pitch_points, pitch_score, median_error, in_tune_ratio = _compare_pitch(
        reference_pitch, performance_pitch
    )
    duration_ratio = min(reference_duration, performance_duration) / max(
        reference_duration, performance_duration, 1e-6
    )
    completeness = int(round(100 * min(1.0, performance_duration / max(reference_duration, 1e-6))))
    onset_similarity = _shape_similarity(reference_onset, performance_onset)
    rhythm = int(round(100 * (0.7 * onset_similarity + 0.3 * duration_ratio)))
    stability = int(round(100 * float(np.clip(voiced, 0.0, 1.0))))
    total = int(round(
        0.5 * pitch_score
        + 0.25 * rhythm
        + 0.15 * completeness
        + 0.1 * stability
    ))
    notes = [
        "评分由本地声学特征计算，大模型不参与总分。",
        "参考音频含伴奏时，旋律提取可能受乐器影响；清唱或伴奏版参考更稳定。",
    ]
    if median_error is None:
        notes.append("双方可比较的稳定音高不足，音准分按保守值计算。")
    return SingingScore(
        total=max(0, min(100, total)),
        pitch=pitch_score,
        rhythm=max(0, min(100, rhythm)),
        completeness=max(0, min(100, completeness)),
        stability=max(0, min(100, stability)),
        median_pitch_error=median_error,
        in_tune_ratio=in_tune_ratio,
        reference_duration_s=round(reference_duration, 2),
        performance_duration_s=round(performance_duration, 2),
        pitch_curve=pitch_points,
        notes=notes,
    )


@lru_cache(maxsize=16)
def _features(path: str, _mtime_ns: int):
    return _extract_features(Path(path))


def _extract_features(path: Path):
    audio, sample_rate = decode_mono(path, sample_rate=16_000)
    if audio.size < sample_rate:
        raise ValueError("演唱音频至少需要 1 秒。")
    trimmed, _ = librosa.effects.trim(audio, top_db=35)
    if trimmed.size < sample_rate:
        trimmed = audio
    duration = len(trimmed) / sample_rate
    hop = 512
    f0, voiced_flag, voiced_probability = librosa.pyin(
        trimmed,
        fmin=librosa.note_to_hz("C2"),
        fmax=librosa.note_to_hz("C7"),
        sr=sample_rate,
        frame_length=2048,
        hop_length=hop,
    )
    midi = librosa.hz_to_midi(f0)
    midi[~np.isfinite(midi)] = np.nan
    onset = librosa.onset.onset_strength(y=trimmed, sr=sample_rate, hop_length=hop)
    voiced = float(np.nanmean(voiced_probability[voiced_flag])) if np.any(voiced_flag) else 0.0
    return midi, onset, duration, voiced


def _resample(values: np.ndarray, size: int) -> np.ndarray:
    if values.size == 0:
        return np.full(size, np.nan)
    source = np.linspace(0.0, 1.0, values.size)
    target = np.linspace(0.0, 1.0, size)
    finite = np.isfinite(values)
    if np.count_nonzero(finite) < 2:
        return np.full(size, np.nan)
    return np.interp(target, source[finite], values[finite])


def _compare_pitch(reference: np.ndarray, performance: np.ndarray):
    size = 240
    ref = _resample(reference, size)
    sung = _resample(performance, size)
    valid = np.isfinite(ref) & np.isfinite(sung)
    errors = np.abs(ref[valid] - sung[valid])
    if errors.size < 8:
        score = 20
        median_error = None
        in_tune = None
    else:
        median_error = round(float(np.median(errors)), 2)
        in_tune = round(float(np.mean(errors <= 0.5)), 3)
        score = int(round(100 * np.exp(-median_error / 2.0)))
    indices = np.linspace(0, size - 1, 80).astype(int)
    points = []
    for index in indices:
        ref_value = float(ref[index]) if np.isfinite(ref[index]) else None
        sung_value = float(sung[index]) if np.isfinite(sung[index]) else None
        error = (
            round(abs(ref_value - sung_value), 2)
            if ref_value is not None and sung_value is not None
            else None
        )
        points.append(
            PitchPoint(
                progress=round(index / (size - 1), 4),
                reference_midi=round(ref_value, 2) if ref_value is not None else None,
                performance_midi=round(sung_value, 2) if sung_value is not None else None,
                error_semitones=error,
            )
        )
    return points, max(0, min(100, score)), median_error, in_tune


def _shape_similarity(first: np.ndarray, second: np.ndarray) -> float:
    first_resampled = np.nan_to_num(_resample(first, 300))
    second_resampled = np.nan_to_num(_resample(second, 300))
    if np.std(first_resampled) < 1e-7 or np.std(second_resampled) < 1e-7:
        return 0.0
    correlation = float(np.corrcoef(first_resampled, second_resampled)[0, 1])
    return float(np.clip((correlation + 1.0) / 2.0, 0.0, 1.0))
