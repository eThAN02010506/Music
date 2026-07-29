from __future__ import annotations

from pathlib import Path
import wave

import numpy as np

from music_insight.api.services.waveform import build_waveform, load_waveform


def _write_wave(path: Path, duration_s: float = 1.0) -> None:
    sample_rate = 16_000
    time = np.arange(int(sample_rate * duration_s)) / sample_rate
    samples = (np.sin(2 * np.pi * 220 * time) * 20_000).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples.tobytes())


def test_waveform_is_bounded_normalized_and_reports_duration(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tone.wav"
    _write_wave(source)

    waveform = build_waveform(source, points=200, max_duration_s=2)

    assert waveform.duration_s == 1.0
    assert len(waveform.peaks) == 1
    assert waveform.points_per_channel == len(waveform.peaks[0])
    assert waveform.points_per_channel <= 400
    assert max(abs(value) for value in waveform.peaks[0]) <= 1


def test_cached_waveform_invalidates_when_source_changes(tmp_path: Path) -> None:
    source = tmp_path / "tone.wav"
    _write_wave(source, 1.0)
    first = load_waveform(source, points=200, max_duration_s=2)

    _write_wave(source, 1.5)
    second = load_waveform(source, points=200, max_duration_s=2)

    assert first.duration_s == 1.0
    assert second.duration_s == 1.5


def test_waveform_streaming_keeps_output_bounded_for_longer_audio(
    tmp_path: Path,
) -> None:
    source = tmp_path / "longer-tone.wav"
    _write_wave(source, 4.0)

    waveform = build_waveform(source, points=100, max_duration_s=5)

    assert waveform.duration_s == 4.0
    assert waveform.points_per_channel <= 200
