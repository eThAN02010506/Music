from __future__ import annotations

from pathlib import Path

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
