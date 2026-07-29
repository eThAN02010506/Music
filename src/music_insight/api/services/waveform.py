from __future__ import annotations

from functools import lru_cache
from math import ceil
from pathlib import Path

import numpy as np

from music_insight.api.contracts.history import HistoryWaveform
from music_insight.audio import iter_mono_chunks, probe_audio_duration


def load_waveform(
    path: Path,
    *,
    points: int = 1_200,
    max_duration_s: float | None = None,
) -> HistoryWaveform:
    """Reuse small pre-decoded envelopes while the source file is unchanged."""

    resolved = path.resolve()
    stat = resolved.stat()
    return _cached_waveform(
        str(resolved),
        stat.st_mtime_ns,
        stat.st_size,
        max(100, min(int(points), 4_000)),
        max_duration_s,
    )


@lru_cache(maxsize=64)
def _cached_waveform(
    path: str,
    _mtime_ns: int,
    _size: int,
    points: int,
    max_duration_s: float | None,
) -> HistoryWaveform:
    return build_waveform(
        Path(path),
        points=points,
        max_duration_s=max_duration_s,
    )


def build_waveform(
    path: Path,
    *,
    points: int = 1_200,
    max_duration_s: float | None = None,
) -> HistoryWaveform:
    """Create a bounded mono min/max envelope for the browser waveform."""

    requested_points = max(100, min(int(points), 4_000))
    sample_rate = 4_000
    duration_hint = probe_audio_duration(
        path,
        max_duration_s=max_duration_s,
    )
    if duration_hint is None or duration_hint <= 0:
        raise ValueError("音频过短，无法生成波形。")

    estimated_samples = max(2, ceil(duration_hint * sample_rate))
    bucket_size = max(1, ceil(estimated_samples / requested_points))
    minimums: list[float] = []
    maximums: list[float] = []
    decoded_samples = 0
    samples_in_bucket = 0
    current_min = float("inf")
    current_max = float("-inf")
    for chunk in iter_mono_chunks(
        path,
        sample_rate=sample_rate,
        max_duration_s=max_duration_s,
    ):
        decoded_samples += int(chunk.size)
        offset = 0
        while offset < chunk.size:
            take = min(bucket_size - samples_in_bucket, chunk.size - offset)
            part = chunk[offset : offset + take]
            current_min = min(current_min, float(np.min(part)))
            current_max = max(current_max, float(np.max(part)))
            samples_in_bucket += int(take)
            offset += int(take)
            if samples_in_bucket == bucket_size:
                minimums.append(current_min)
                maximums.append(current_max)
                samples_in_bucket = 0
                current_min = float("inf")
                current_max = float("-inf")
    if samples_in_bucket:
        minimums.append(current_min)
        maximums.append(current_max)
    if decoded_samples < 2 or not minimums:
        raise ValueError("音频过短，无法生成波形。")

    minimum_array, maximum_array = _compress_extrema(
        np.asarray(minimums, dtype=np.float32),
        np.asarray(maximums, dtype=np.float32),
        requested_points,
    )
    envelope = np.empty(minimum_array.size * 2, dtype=np.float32)
    envelope[0::2] = minimum_array
    envelope[1::2] = maximum_array
    peak = float(np.max(np.abs(envelope)))
    if np.isfinite(peak) and peak > 0:
        envelope /= peak
    safe = np.nan_to_num(
        envelope,
        copy=False,
        nan=0.0,
        posinf=1.0,
        neginf=-1.0,
    )
    np.clip(safe, -1.0, 1.0, out=safe)
    return HistoryWaveform(
        duration_s=float(decoded_samples / sample_rate),
        peaks=[[round(float(value), 4) for value in safe]],
        points_per_channel=int(safe.size),
    )


def _compress_extrema(
    minimums: np.ndarray,
    maximums: np.ndarray,
    target_points: int,
) -> tuple[np.ndarray, np.ndarray]:
    if minimums.size <= target_points:
        return minimums, maximums
    boundaries = np.linspace(
        0,
        minimums.size,
        target_points + 1,
        dtype=np.int64,
    )
    compressed_minimums = np.empty(target_points, dtype=np.float32)
    compressed_maximums = np.empty(target_points, dtype=np.float32)
    for index in range(target_points):
        start = int(boundaries[index])
        end = max(start + 1, int(boundaries[index + 1]))
        compressed_minimums[index] = np.min(minimums[start:end])
        compressed_maximums[index] = np.max(maximums[start:end])
    return compressed_minimums, compressed_maximums
