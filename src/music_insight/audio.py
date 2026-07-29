from __future__ import annotations

import io
from pathlib import Path
from collections.abc import Iterator
import wave

import av
import numpy as np


class AudioDurationExceededError(ValueError):
    """Raised before decoded PCM can grow beyond the configured duration."""


_APE_TAG_PREAMBLE = b"APETAGEX"
_APE_TAG_FOOTER_BYTES = 32
_APE_TAG_MAX_BYTES = 16 * 1024 * 1024 + _APE_TAG_FOOTER_BYTES
_APE_TAG_MAX_FIELDS = 65_536
_APE_TAG_MAX_KEY_BYTES = 1_023


def probe_audio_duration(
    path: Path,
    *,
    max_duration_s: float | None = None,
) -> float | None:
    """Validate audio and return duration, scanning frames if metadata is absent."""

    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError(f"音频文件中没有可用音轨：{path.name}")
        stream = container.streams.audio[0]
        if stream.duration is not None and stream.time_base is not None:
            duration = float(stream.duration * stream.time_base)
            if np.isfinite(duration) and duration >= 0:
                _check_duration_limit(duration, max_duration_s)
                return duration
        if container.duration is not None:
            duration = float(container.duration / av.time_base)
            if np.isfinite(duration) and duration >= 0:
                _check_duration_limit(duration, max_duration_s)
                return duration
        # Some valid containers omit duration metadata. Scan decoded frame
        # lengths without retaining PCM so an unknown-duration upload cannot
        # bypass the limit and reach the model chunk loop.
        duration = 0.0
        for frame in _decode_audio_frames(
            container,
            stream,
            trailing_metadata_start=_trailing_apev2_start(path),
            file_size=path.stat().st_size,
        ):
            if frame.sample_rate and frame.samples:
                duration += frame.samples / frame.sample_rate
                _check_duration_limit(duration, max_duration_s)
        return duration
    return None


def decode_mono(
    path: Path,
    sample_rate: int = 22_050,
    *,
    max_duration_s: float | None = None,
) -> tuple[np.ndarray, int]:
    """Decode any PyAV-supported audio file to mono float PCM."""
    pcm = bytearray()
    decoded_samples = 0
    max_samples = (
        max(1, int(max_duration_s * sample_rate))
        if max_duration_s is not None
        else None
    )
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError(f"音频文件中没有可用音轨：{path.name}")
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=sample_rate)
        for frame in _decode_audio_frames(
            container,
            stream,
            trailing_metadata_start=_trailing_apev2_start(path),
            file_size=path.stat().st_size,
        ):
            for converted in resampler.resample(frame):
                chunk = np.asarray(
                    converted.to_ndarray(),
                    dtype=np.float32,
                ).reshape(-1)
                decoded_samples += chunk.size
                if max_samples is not None and decoded_samples > max_samples:
                    raise AudioDurationExceededError(
                        f"音频超过允许的 {max_duration_s / 60:.1f} 分钟。"
                    )
                pcm.extend(chunk.tobytes())
        for converted in resampler.resample(None):
            chunk = np.asarray(
                converted.to_ndarray(),
                dtype=np.float32,
            ).reshape(-1)
            decoded_samples += chunk.size
            if max_samples is not None and decoded_samples > max_samples:
                raise AudioDurationExceededError(
                    f"音频超过允许的 {max_duration_s / 60:.1f} 分钟。"
                )
            pcm.extend(chunk.tobytes())

    if not pcm:
        return np.zeros(0, dtype=np.float32), sample_rate
    # ``frombuffer`` keeps one backing bytearray instead of temporarily holding
    # both a list of all decoded chunks and a second concatenated allocation.
    audio = np.frombuffer(pcm, dtype=np.float32)
    return np.nan_to_num(audio, copy=False), sample_rate


def iter_mono_chunks(
    path: Path,
    sample_rate: int = 22_050,
    *,
    max_duration_s: float | None = None,
) -> Iterator[np.ndarray]:
    """Decode mono float PCM incrementally without retaining the whole track."""

    decoded_samples = 0
    max_samples = (
        max(1, int(max_duration_s * sample_rate))
        if max_duration_s is not None
        else None
    )
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError(f"音频文件中没有可用音轨：{path.name}")
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=sample_rate)
        frames = _decode_audio_frames(
            container,
            stream,
            trailing_metadata_start=_trailing_apev2_start(path),
            file_size=path.stat().st_size,
        )
        for frame in frames:
            for converted in resampler.resample(frame):
                chunk = np.asarray(
                    converted.to_ndarray(),
                    dtype=np.float32,
                ).reshape(-1)
                decoded_samples += chunk.size
                if max_samples is not None and decoded_samples > max_samples:
                    raise AudioDurationExceededError(
                        f"音频超过允许的 {max_duration_s / 60:.1f} 分钟。"
                    )
                if chunk.size:
                    yield np.nan_to_num(chunk, copy=False)
        for converted in resampler.resample(None):
            chunk = np.asarray(
                converted.to_ndarray(),
                dtype=np.float32,
            ).reshape(-1)
            decoded_samples += chunk.size
            if max_samples is not None and decoded_samples > max_samples:
                raise AudioDurationExceededError(
                    f"音频超过允许的 {max_duration_s / 60:.1f} 分钟。"
                )
            if chunk.size:
                yield np.nan_to_num(chunk, copy=False)


def _decode_audio_frames(
    container: av.container.InputContainer,
    stream: av.audio.stream.AudioStream,
    *,
    trailing_metadata_start: int | None,
    file_size: int,
):
    """Decode packets while tolerating only a validated trailing APEv2 tag."""

    decoded_audio = False
    for packet in container.demux(stream):
        try:
            frames = packet.decode()
        except av.InvalidDataError:
            packet_position = packet.pos
            is_validated_trailing_metadata = (
                decoded_audio
                and trailing_metadata_start is not None
                and packet_position is not None
                and packet_position >= trailing_metadata_start
                and packet_position + packet.size <= file_size
            )
            if is_validated_trailing_metadata:
                continue
            raise
        for frame in frames:
            decoded_audio = True
            yield frame


def _trailing_apev2_start(path: Path) -> int | None:
    """Return the start of a structurally valid trailing APEv2 tag."""

    try:
        file_size = path.stat().st_size
        if file_size < _APE_TAG_FOOTER_BYTES:
            return None
        with path.open("rb") as source:
            source.seek(file_size - _APE_TAG_FOOTER_BYTES)
            footer = source.read(_APE_TAG_FOOTER_BYTES)
            if (
                len(footer) != _APE_TAG_FOOTER_BYTES
                or footer[:8] != _APE_TAG_PREAMBLE
            ):
                return None
            version = int.from_bytes(footer[8:12], "little")
            tag_size = int.from_bytes(footer[12:16], "little")
            field_count = int.from_bytes(footer[16:20], "little")
            if (
                version not in {1_000, 2_000}
                or not _APE_TAG_FOOTER_BYTES <= tag_size <= _APE_TAG_MAX_BYTES
                or tag_size > file_size
                or field_count > _APE_TAG_MAX_FIELDS
            ):
                return None

            tag_start = file_size - tag_size
            footer_start = file_size - _APE_TAG_FOOTER_BYTES
            source.seek(tag_start)
            for _ in range(field_count):
                field_header = source.read(8)
                if len(field_header) != 8:
                    return None
                value_size = int.from_bytes(field_header[:4], "little")
                key_size = 0
                while key_size <= _APE_TAG_MAX_KEY_BYTES:
                    key_byte = source.read(1)
                    if not key_byte:
                        return None
                    if key_byte == b"\0":
                        break
                    if not 0x20 <= key_byte[0] <= 0x7E:
                        return None
                    key_size += 1
                else:
                    return None
                value_end = source.tell() + value_size
                if value_end > footer_start:
                    return None
                source.seek(value_size, 1)
            if source.tell() != footer_start:
                return None
            return tag_start
    except OSError:
        return None


def _check_duration_limit(
    duration_s: float,
    max_duration_s: float | None,
) -> None:
    if max_duration_s is not None and duration_s > max_duration_s:
        raise AudioDurationExceededError(
            f"音频超过允许的 {max_duration_s / 60:.1f} 分钟。"
        )


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
