from __future__ import annotations

from collections.abc import Iterator
import io
import math
from pathlib import Path
import wave


def iter_wav_chunks(
    path: Path,
    chunk_seconds: float,
    overlap_seconds: float = 0.0,
) -> Iterator[tuple[bytes, float, float]]:
    """Yield self-contained WAV chunks with their absolute time bounds."""

    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        total_frames = source.getnframes()
        frames_per_chunk = max(1, int(chunk_seconds * sample_rate))
        overlap_frames = min(
            frames_per_chunk - 1,
            max(0, int(overlap_seconds * sample_rate)),
        )
        step_frames = max(1, frames_per_chunk - overlap_frames)
        start_frame = 0
        while start_frame < total_frames:
            source.setpos(start_frame)
            frame_count = min(frames_per_chunk, total_frames - start_frame)
            frames = source.readframes(frame_count)
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as output:
                output.setnchannels(channels)
                output.setsampwidth(sample_width)
                output.setframerate(sample_rate)
                output.writeframes(frames)
            start_s = start_frame / sample_rate
            end_s = (start_frame + frame_count) / sample_rate
            yield buffer.getvalue(), start_s, end_s
            if start_frame + frame_count >= total_frames:
                break
            start_frame += step_frames


def count_wav_chunks(
    path: Path,
    chunk_seconds: float,
    overlap_seconds: float = 0.0,
) -> int:
    """Return the number of chunks without decoding the complete audio file."""

    with wave.open(str(path), "rb") as source:
        sample_rate = source.getframerate()
        frames_per_chunk = max(1, int(chunk_seconds * sample_rate))
        overlap_frames = min(
            frames_per_chunk - 1,
            max(0, int(overlap_seconds * sample_rate)),
        )
        step_frames = max(1, frames_per_chunk - overlap_frames)
        total_frames = source.getnframes()
        if total_frames <= frames_per_chunk:
            return 1
        return 1 + math.ceil((total_frames - frames_per_chunk) / step_frames)
