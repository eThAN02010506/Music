import wave

import numpy as np

from music_insight.singing_score import score_singing


def _tone(path, frequency: float, seconds: float = 3.0) -> None:
    sample_rate = 16_000
    time = np.arange(int(seconds * sample_rate)) / sample_rate
    envelope = np.minimum(1.0, np.minimum(time * 8, (seconds - time) * 8))
    signal = 0.3 * np.sin(2 * np.pi * frequency * time) * envelope
    pcm = (np.clip(signal, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())


def _melody(path, seconds_per_note: float) -> None:
    sample_rate = 16_000
    frequencies = (220.0, 246.94, 261.63, 293.66, 329.63, 293.66, 261.63, 220.0)
    notes = []
    sample_count = int(seconds_per_note * sample_rate)
    time = np.arange(sample_count) / sample_rate
    envelope = np.minimum(1.0, np.minimum(time * 15, (seconds_per_note - time) * 15))
    for frequency in frequencies:
        notes.append(0.3 * np.sin(2 * np.pi * frequency * time) * envelope)
    pcm = (np.clip(np.concatenate(notes), -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())


def test_singing_score_rewards_matching_pitch(tmp_path):
    reference = tmp_path / "reference.wav"
    matching = tmp_path / "matching.wav"
    detuned = tmp_path / "detuned.wav"
    _tone(reference, 220.0)
    _tone(matching, 220.0)
    _tone(detuned, 277.18)

    good = score_singing(reference, matching)
    bad = score_singing(reference, detuned)

    assert good.pitch >= 95
    assert good.total > bad.total
    assert bad.median_pitch_error is not None
    assert bad.median_pitch_error >= 3.5
    assert len(good.pitch_curve) == 80


def test_singing_score_dtw_tolerates_consistent_time_stretch(tmp_path):
    reference = tmp_path / "reference-melody.wav"
    stretched = tmp_path / "stretched-melody.wav"
    _melody(reference, seconds_per_note=0.35)
    _melody(stretched, seconds_per_note=0.5)

    score = score_singing(reference, stretched)

    assert score.pitch >= 90
    assert score.median_pitch_error is not None
    assert score.median_pitch_error <= 0.5
    assert score.in_tune_ratio is not None
    assert score.in_tune_ratio >= 0.8
