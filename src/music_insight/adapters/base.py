from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from music_insight.schemas import (
    AsrResult,
    AsrVerificationResult,
    AudioAsset,
    AudioSceneResult,
    DspResult,
    LiteraryResult,
    LyricsSegment,
    UnifiedAudioResult,
    VerifiedLyricsSynthesisResult,
)


class AsrAdapter(ABC):
    @abstractmethod
    async def transcribe(self, asset: AudioAsset) -> AsrResult:
        raise NotImplementedError


@runtime_checkable
class AsrVerifier(Protocol):
    """Optional secondary ASR contract used to verify primary-model lyrics."""

    source: str
    endpoint: str

    async def verify(self, asset: AudioAsset) -> AsrVerificationResult: ...


class AudioSceneAdapter(ABC):
    @abstractmethod
    async def analyze_scene(
        self,
        asset: AudioAsset,
        lyrics: AsrResult | None,
        dsp: DspResult | None,
    ) -> AudioSceneResult:
        raise NotImplementedError


class LiteraryAdapter(ABC):
    @abstractmethod
    async def interpret(
        self,
        asset: AudioAsset,
        lyrics: AsrResult,
        scene: AudioSceneResult,
        dsp: DspResult,
    ) -> LiteraryResult:
        raise NotImplementedError


class DspAdapter(ABC):
    @abstractmethod
    async def analyze(self, asset: AudioAsset) -> DspResult:
        raise NotImplementedError


class UnifiedAudioAdapter(ABC):
    @abstractmethod
    async def analyze(
        self,
        asset: AudioAsset,
        dsp: DspResult,
        progress: Callable[[str, float, str], Awaitable[None] | None] | None = None,
    ) -> UnifiedAudioResult:
        raise NotImplementedError


@runtime_checkable
class LyricsRetryAdapter(Protocol):
    """Capability contract for adapters that can re-listen to a WAV excerpt."""

    source: str

    async def retry_lyrics(
        self,
        audio_bytes: bytes,
        duration_s: float,
        language_hint: str | None,
    ) -> tuple[list[LyricsSegment], list[str]]: ...


@runtime_checkable
class VerifiedLyricsSynthesisAdapter(Protocol):
    """Capability to rebuild interpretation from externally verified lyrics."""

    source: str

    async def resynthesize_verified_lyrics(
        self,
        lyrics: list[LyricsSegment],
        scene: AudioSceneResult,
        dsp: DspResult,
    ) -> VerifiedLyricsSynthesisResult: ...
