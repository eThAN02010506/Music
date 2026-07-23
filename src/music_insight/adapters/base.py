from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from music_insight.schemas import (
    AsrResult,
    AudioAsset,
    AudioSceneResult,
    DspResult,
    LiteraryResult,
)


class AsrAdapter(ABC):
    @abstractmethod
    async def transcribe(self, asset: AudioAsset) -> AsrResult:
        raise NotImplementedError


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
    ):
        raise NotImplementedError
