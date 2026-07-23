from __future__ import annotations

import io
from pathlib import Path
import wave

import av
import numpy as np


def decode_mono(path: Path, sample_rate: int = 22_050) -> tuple[np.ndarray, int]:
    """Decode any PyAV-supported audio file to mono float PCM."""
    chunks: list[np.ndarray] = []
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError(f"音频文件中没有可用音轨：{path.name}")
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=sample_rate)
        for frame in container.decode(stream):
            for converted in resampler.resample(frame):
                chunks.append(converted.to_ndarray().reshape(-1).astype(np.float32))
        for converted in resampler.resample(None):
            chunks.append(converted.to_ndarray().reshape(-1).astype(np.float32))

    if not chunks:
        return np.zeros(0, dtype=np.float32), sample_rate
    audio = np.concatenate(chunks)
    return np.nan_to_num(audio, copy=False), sample_rate


def slice_wav(path: Path, start_s: float, end_s: float) -> tuple[bytes, float]:
    """Return a bounded WAV excerpt and its exact duration."""
    with wave.open(str(path), "rb") as source:
        sample_rate = source.getframerate()
        total_frames = source.getnframes()
        start_frame = max(0, min(total_frames, int(start_s * sample_rate)))
        end_frame = max(
            start_frame,
            min(total_frames, int(end_s * sample_rate)),
        )
        if end_frame <= start_frame:
            raise ValueError("重听范围内没有音频。")
        source.setpos(start_frame)
        frames = source.readframes(end_frame - start_frame)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(source.getnchannels())
            output.setsampwidth(source.getsampwidth())
            output.setframerate(sample_rate)
            output.writeframes(frames)
    return buffer.getvalue(), (end_frame - start_frame) / sample_rate
