from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import librosa
import numpy as np
from pydantic import BaseModel, Field

from music_insight.audio import decode_mono


class PitchPoint(BaseModel):
    progress: float = Field(ge=0, le=1)
    reference_time_s: float | None = Field(default=None, ge=0)
    performance_time_s: float | None = Field(default=None, ge=0)
    reference_midi: float | None = None
    performance_midi: float | None = None
    error_semitones: float | None = None
    signed_error_semitones: float | None = None


class SingingPracticeMoment(BaseModel):
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    observation: str = Field(min_length=1, max_length=300)
    listening_task: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)


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
    practice_moments: list[SingingPracticeMoment] = Field(
        default_factory=list,
        max_length=3,
    )
    notes: list[str]


def score_singing(
    reference_path: Path,
    performance_path: Path,
    max_duration_s: float | None = None,
) -> SingingScore:
    reference = _features(
        str(reference_path.resolve()),
        reference_path.stat().st_mtime_ns,
        max_duration_s,
    )
    performance = _extract_features(
        performance_path,
        max_duration_s=max_duration_s,
    )
    (
        reference_pitch,
        reference_onset,
        reference_analysis_duration,
        reference_voiced_coverage,
        _,
        reference_offset,
        reference_duration,
    ) = reference
    (
        performance_pitch,
        performance_onset,
        performance_analysis_duration,
        voiced_coverage,
        pitch_stability,
        performance_offset,
        performance_duration,
    ) = performance
    duration_ratio = min(
        reference_analysis_duration,
        performance_analysis_duration,
    ) / max(
        reference_analysis_duration,
        performance_analysis_duration,
        1e-6,
    )
    voiced_duration_ratio = min(
        1.0,
        (voiced_coverage * performance_analysis_duration)
        / max(
            reference_voiced_coverage * reference_analysis_duration,
            1e-6,
        ),
    )
    (
        pitch_points,
        pitch_score,
        median_error,
        in_tune_ratio,
        aligned_coverage,
    ) = _compare_pitch(
        reference_pitch,
        performance_pitch,
        reference_duration=reference_analysis_duration,
        performance_duration=performance_analysis_duration,
        reference_offset=reference_offset,
        performance_offset=performance_offset,
        coverage_cap=voiced_duration_ratio,
    )
    completeness = int(round(100 * (
        0.6 * aligned_coverage * duration_ratio
        + 0.4 * voiced_duration_ratio
    )))
    onset_similarity = _dtw_shape_similarity(reference_onset, performance_onset)
    rhythm = int(round(100 * (0.7 * onset_similarity + 0.3 * duration_ratio)))
    stability = int(round(100 * pitch_stability))
    total = int(round(
        0.5 * pitch_score
        + 0.25 * rhythm
        + 0.15 * completeness
        + 0.1 * stability
    ))
    notes = [
        "评分由本地声学特征计算，大模型不参与总分。",
        "音高和节奏已使用动态时间规整对齐，允许合理的局部快慢差异。",
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
        practice_moments=_practice_moments(
            pitch_points,
            reference_duration=reference_duration,
        ),
        notes=notes,
    )


@lru_cache(maxsize=16)
def _features(path: str, _mtime_ns: int, max_duration_s: float | None):
    return _extract_features(Path(path), max_duration_s=max_duration_s)


def _extract_features(path: Path, *, max_duration_s: float | None = None):
    audio, sample_rate = decode_mono(
        path,
        sample_rate=16_000,
        max_duration_s=max_duration_s,
    )
    if audio.size < sample_rate:
        raise ValueError("演唱音频至少需要 1 秒。")
    audio_duration = len(audio) / sample_rate
    trimmed, trim_indices = librosa.effects.trim(audio, top_db=35)
    trim_offset = float(trim_indices[0]) / sample_rate
    if trimmed.size < sample_rate:
        trimmed = audio
        trim_offset = 0.0
    duration = len(trimmed) / sample_rate
    hop = 512
    f0, voiced_flag, _ = librosa.pyin(
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
    voiced_coverage = float(np.mean(voiced_flag)) if voiced_flag.size else 0.0
    stability = _pitch_stability(midi)
    return (
        midi,
        onset,
        duration,
        voiced_coverage,
        stability,
        trim_offset,
        audio_duration,
    )


def _compress_pitch(values: np.ndarray, size: int) -> np.ndarray:
    """Downsample pitch while retaining the original voiced/unvoiced mask."""

    values = np.asarray(values, dtype=float).reshape(-1)
    if values.size <= size:
        return values
    output = np.full(size, np.nan)
    for index, bucket in enumerate(np.array_split(values, size)):
        finite = bucket[np.isfinite(bucket)]
        if finite.size / max(1, bucket.size) >= 0.25:
            output[index] = float(np.median(finite))
    return output


def _compress_onset(values: np.ndarray, size: int) -> np.ndarray:
    """Downsample sparse onset strength without erasing narrow transients."""

    values = np.asarray(values, dtype=float).reshape(-1)
    if values.size <= size:
        return values
    return np.asarray(
        [
            float(np.max(bucket)) if bucket.size else 0.0
            for bucket in np.array_split(values, size)
        ],
        dtype=float,
    )


def _compare_pitch(
    reference: np.ndarray,
    performance: np.ndarray,
    *,
    reference_duration: float | None = None,
    performance_duration: float | None = None,
    reference_offset: float = 0.0,
    performance_offset: float = 0.0,
    coverage_cap: float = 1.0,
):
    ref = _compress_pitch(reference, 600)
    sung = _compress_pitch(performance, 600)
    if not ref.size or not sung.size:
        return [], 20, None, None, 0.0

    ref_grid = ref[:, None]
    sung_grid = sung[None, :]
    both_voiced = np.isfinite(ref_grid) & np.isfinite(sung_grid)
    one_voiced = np.isfinite(ref_grid) ^ np.isfinite(sung_grid)
    cost = np.full((ref.size, sung.size), 0.35, dtype=np.float32)
    pitch_distance = np.abs(
        np.nan_to_num(ref_grid) - np.nan_to_num(sung_grid)
    )
    cost[both_voiced] = np.minimum(pitch_distance[both_voiced], 12.0)
    cost[one_voiced] = 4.0
    _, raw_path = librosa.sequence.dtw(
        C=cost,
        global_constraints=True,
        band_rad=0.25,
    )
    path = raw_path[::-1]
    ref_indices = path[:, 0]
    sung_indices = path[:, 1]
    valid = np.isfinite(ref[ref_indices]) & np.isfinite(sung[sung_indices])
    errors = np.abs(
        ref[ref_indices[valid]] - sung[sung_indices[valid]]
    )
    voiced_reference = np.flatnonzero(np.isfinite(ref))
    matched_reference = np.unique(ref_indices[valid])
    aligned_coverage = (
        float(np.intersect1d(voiced_reference, matched_reference).size)
        / max(1, voiced_reference.size)
    )
    aligned_coverage = float(
        np.clip(min(aligned_coverage, coverage_cap), 0.0, 1.0)
    )
    if errors.size < 8:
        score = 20
        median_error = None
        in_tune = None
    else:
        median_error = round(float(np.median(errors)), 2)
        in_tune = round(float(np.mean(errors <= 0.5)), 3)
        raw_accuracy = 100 * np.exp(-median_error / 2.0)
        # Missing most target notes must not produce a perfect pitch score
        # merely because the few comparable frames happened to be accurate.
        score = int(round(
            20 * (1.0 - aligned_coverage)
            + raw_accuracy * aligned_coverage
        ))
    path_indices = np.linspace(0, len(path) - 1, 80).astype(int)
    points = []
    for output_index, path_index in enumerate(path_indices):
        ref_index, sung_index = path[path_index]
        ref_value = (
            float(ref[ref_index]) if np.isfinite(ref[ref_index]) else None
        )
        sung_value = (
            float(sung[sung_index]) if np.isfinite(sung[sung_index]) else None
        )
        error = (
            round(abs(ref_value - sung_value), 2)
            if ref_value is not None and sung_value is not None
            else None
        )
        signed_error = (
            round(sung_value - ref_value, 2)
            if ref_value is not None and sung_value is not None
            else None
        )
        points.append(
            PitchPoint(
                progress=round(output_index / max(1, len(path_indices) - 1), 4),
                reference_time_s=_index_time(
                    int(ref_index),
                    len(ref),
                    reference_duration,
                    reference_offset,
                ),
                performance_time_s=_index_time(
                    int(sung_index),
                    len(sung),
                    performance_duration,
                    performance_offset,
                ),
                reference_midi=round(ref_value, 2) if ref_value is not None else None,
                performance_midi=round(sung_value, 2) if sung_value is not None else None,
                error_semitones=error,
                signed_error_semitones=signed_error,
            )
        )
    return (
        points,
        max(0, min(100, score)),
        median_error,
        in_tune,
        aligned_coverage,
    )


def _index_time(
    index: int,
    size: int,
    duration: float | None,
    offset: float = 0.0,
) -> float | None:
    if duration is None or size <= 0:
        return None
    return round(
        float(
            np.clip(index / max(1, size - 1) * duration, 0, duration)
            + max(0.0, offset)
        ),
        2,
    )


def _practice_moments(
    points: list[PitchPoint],
    *,
    reference_duration: float,
) -> list[SingingPracticeMoment]:
    """Select up to three evidence-backed windows worth practicing."""

    if len(points) < 8:
        return []
    candidates: list[tuple[float, int, list[PitchPoint]]] = []
    for index, bucket in enumerate(np.array_split(np.asarray(points, dtype=object), 8)):
        values = [
            point
            for point in bucket.tolist()
            if isinstance(point, PitchPoint)
            and point.error_semitones is not None
            and point.reference_time_s is not None
        ]
        errors = [point.error_semitones for point in values]
        if len(errors) < 3:
            continue
        median_error = float(np.median(errors))
        if median_error < 0.5:
            continue
        candidates.append((median_error, index, values))
    candidates.sort(key=lambda item: (-item[0], item[1]))

    moments: list[SingingPracticeMoment] = []
    for median_error, _, values in candidates[:3]:
        times = [
            point.reference_time_s
            for point in values
            if point.reference_time_s is not None
        ]
        signed = [
            point.signed_error_semitones
            for point in values
            if point.signed_error_semitones is not None
        ]
        if not times:
            continue
        start_s = max(0.0, min(times))
        end_s = min(
            reference_duration,
            max(max(times), start_s + max(1.0, reference_duration / 80)),
        )
        if end_s <= start_s:
            continue
        median_signed = float(np.median(signed)) if signed else 0.0
        direction = (
            "整体偏高"
            if median_signed > 0.35
            else "整体偏低"
            if median_signed < -0.35
            else "高低方向不固定"
        )
        confidence = min(1.0, len(values) / 10)
        moments.append(
            SingingPracticeMoment(
                start_s=round(start_s, 2),
                end_s=round(end_s, 2),
                observation=(
                    f"这一段音高中位偏差约 {median_error:.2f} 半音，"
                    f"{direction}。"
                ),
                listening_task=(
                    "先循环参考片段并只哼主旋律，再关闭参考独唱一次；"
                    "第二遍重点保持起音后的音高中心。"
                ),
                confidence=round(confidence, 2),
            )
        )
    return sorted(moments, key=lambda item: item.start_s)


def _dtw_shape_similarity(first: np.ndarray, second: np.ndarray) -> float:
    first_values = np.nan_to_num(_compress_onset(first, 500))
    second_values = np.nan_to_num(_compress_onset(second, 500))
    if np.std(first_values) < 1e-7 or np.std(second_values) < 1e-7:
        return 0.0
    first_values = (first_values - np.mean(first_values)) / np.std(first_values)
    second_values = (
        second_values - np.mean(second_values)
    ) / np.std(second_values)
    cost = np.abs(first_values[:, None] - second_values[None, :])
    _, path = librosa.sequence.dtw(
        C=cost,
        global_constraints=True,
        band_rad=0.25,
    )
    mean_cost = float(np.mean(cost[path[:, 0], path[:, 1]]))
    return float(np.clip(np.exp(-mean_cost), 0.0, 1.0))


def _pitch_stability(midi: np.ndarray) -> float:
    """Estimate local pitch steadiness without treating voicing as stability."""

    values = np.asarray(midi, dtype=float).reshape(-1)
    if values.size < 3:
        return 0.0
    adjacent = (
        np.isfinite(values[:-1])
        & np.isfinite(values[1:])
    )
    steps = np.abs(np.diff(values)[adjacent])
    # Exclude likely note transitions while retaining ordinary vibrato.
    within_notes = steps[steps <= 1.5]
    if within_notes.size < 4:
        return 0.0
    median_step = float(np.median(within_notes))
    return float(np.clip(np.exp(-median_step / 0.45), 0.0, 1.0))
