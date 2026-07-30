from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeAlias

from music_insight.adapters.base import (
    LyricsRetryAdapter,
    UnifiedAudioAdapter,
    VerifiedLyricsSynthesisAdapter,
)
from music_insight.adapters.minicpm_gateway import (
    MiniCpmGatewayAdapter,
    MiniCpmGatewayClient,
)
from music_insight.adapters.model_capabilities import (
    COMNI_CHAT_PROTOCOL,
    OPENAI_CHAT_PROTOCOL,
    ModelServiceCapabilities,
    probe_model_service,
)
from music_insight.adapters.openai_chat_audio import OpenAIChatAudioAdapter
from music_insight.schemas import (
    AudioAsset,
    AudioSceneResult,
    DspResult,
    LyricsSegment,
    UnifiedAudioResult,
    VerifiedLyricsSynthesisResult,
)
from music_insight.teaching.models import (
    MapGenerationContext,
    MusicUnderstandingMap,
    RelistenRequest,
    RelistenResult,
    TeachingChatContext,
    TeachingChatResponse,
)
from music_insight.teaching.protocols import (
    TeachingModelAdapter,
    TeachingRelistenProvider,
)


ProviderBuilder: TypeAlias = Callable[
    [ModelServiceCapabilities],
    UnifiedAudioAdapter,
]
ProbeFunction: TypeAlias = Callable[
    [str],
    Awaitable[ModelServiceCapabilities],
]


class NetworkOmniProviderRegistry:
    """Extensible protocol-to-adapter registry for network model services."""

    def __init__(self) -> None:
        self._builders: dict[str, ProviderBuilder] = {}

    def register(self, protocol: str, builder: ProviderBuilder) -> None:
        key = protocol.strip()
        if not key:
            raise ValueError("Provider protocol cannot be empty.")
        if key in self._builders:
            raise ValueError(f"Provider protocol is already registered: {key}")
        self._builders[key] = builder

    def build(
        self,
        capabilities: ModelServiceCapabilities,
    ) -> UnifiedAudioAdapter:
        protocol = capabilities.protocol
        if protocol is None or protocol not in self._builders:
            label = protocol or "unknown"
            raise RuntimeError(f"尚未注册模型协议适配器：{label}")
        return self._builders[protocol](capabilities)

    @property
    def protocols(self) -> tuple[str, ...]:
        return tuple(self._builders)


class NetworkOmniAdapter(UnifiedAudioAdapter):
    """Resolve endpoint capabilities once, then delegate to a registered provider."""

    def __init__(
        self,
        *,
        endpoint: str,
        completions_path: str = "/v1/chat/completions",
        models_path: str = "/v1/models",
        model: str | None = None,
        chunk_seconds: float = 30.0,
        chunk_overlap_seconds: float = 1.5,
        comni_chunk_seconds: float = 15.0,
        comni_open_timeout: float = 10.0,
        comni_first_event_timeout: float = 600.0,
        comni_idle_timeout: float = 600.0,
        comni_request_timeout: float = 600.0,
        comni_max_message_bytes: int = 8 * 1024 * 1024,
        registry: NetworkOmniProviderRegistry | None = None,
        probe: ProbeFunction = probe_model_service,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.source = f"自动模型适配 · {self.endpoint}"
        self._probe = probe
        self._delegate: UnifiedAudioAdapter | None = None
        self._delegate_lock = asyncio.Lock()
        if registry is None:
            registry = NetworkOmniProviderRegistry()
            registry.register(
                OPENAI_CHAT_PROTOCOL,
                lambda capabilities: OpenAIChatAudioAdapter(
                    endpoint=self.endpoint,
                    completions_path=completions_path,
                    models_path=models_path,
                    model=model or capabilities.model,
                    chunk_seconds=chunk_seconds,
                    chunk_overlap_seconds=chunk_overlap_seconds,
                    display_name=capabilities.service,
                ),
            )
            registry.register(
                COMNI_CHAT_PROTOCOL,
                lambda capabilities: MiniCpmGatewayAdapter(
                    endpoint=self.endpoint,
                    models_path=models_path,
                    model=model or capabilities.model,
                    chunk_seconds=comni_chunk_seconds,
                    chunk_overlap_seconds=chunk_overlap_seconds,
                    client=MiniCpmGatewayClient(
                        self.endpoint,
                        open_timeout=comni_open_timeout,
                        first_event_timeout=comni_first_event_timeout,
                        idle_timeout=comni_idle_timeout,
                        request_timeout=comni_request_timeout,
                        max_message_bytes=comni_max_message_bytes,
                    ),
                ),
            )
        self.registry = registry

    async def analyze(
        self,
        asset: AudioAsset,
        dsp: DspResult,
        progress: Callable[[str, float, str], Awaitable[None] | None] | None = None,
    ) -> UnifiedAudioResult:
        adapter = await self._resolve()
        return await adapter.analyze(asset, dsp, progress)

    async def retry_lyrics(
        self,
        audio_bytes: bytes,
        duration_s: float,
        language_hint: str | None,
    ) -> tuple[list[LyricsSegment], list[str]]:
        adapter = await self._resolve()
        if not isinstance(adapter, LyricsRetryAdapter):
            raise RuntimeError("当前模型协议不支持歌词重听。")
        return await adapter.retry_lyrics(
            audio_bytes,
            duration_s,
            language_hint,
        )

    async def resynthesize_verified_lyrics(
        self,
        lyrics: list[LyricsSegment],
        scene: AudioSceneResult,
        dsp: DspResult,
    ) -> VerifiedLyricsSynthesisResult:
        adapter = await self._resolve()
        if not isinstance(adapter, VerifiedLyricsSynthesisAdapter):
            raise RuntimeError("当前模型协议不支持已验证歌词重新综合。")
        return await adapter.resynthesize_verified_lyrics(
            lyrics,
            scene,
            dsp,
        )

    async def build_understanding_map(
        self,
        context: MapGenerationContext,
    ) -> MusicUnderstandingMap:
        adapter = await self._resolve()
        if not isinstance(adapter, TeachingModelAdapter):
            raise RuntimeError("当前模型协议不支持结构化音乐导赏。")
        return await adapter.build_understanding_map(context)

    async def answer_music_question(
        self,
        context: TeachingChatContext,
    ) -> TeachingChatResponse:
        adapter = await self._resolve()
        if not isinstance(adapter, TeachingModelAdapter):
            raise RuntimeError("当前模型协议不支持交互式音乐导赏问答。")
        return await adapter.answer_music_question(context)

    async def listen_to_excerpts(
        self,
        request: RelistenRequest,
    ) -> RelistenResult:
        adapter = await self._resolve()
        if not isinstance(adapter, TeachingRelistenProvider):
            raise RuntimeError("当前模型协议不支持局部音频重听。")
        return await adapter.listen_to_excerpts(request)

    async def _resolve(self) -> UnifiedAudioAdapter:
        if self._delegate is not None:
            return self._delegate
        async with self._delegate_lock:
            if self._delegate is not None:
                return self._delegate
            capabilities = await self._probe(self.endpoint)
            if not capabilities.online:
                raise RuntimeError(capabilities.detail)
            if capabilities.analysis_supported is False:
                raise RuntimeError(capabilities.detail)
            delegate = self.registry.build(capabilities)
            self._delegate = delegate
            self.source = getattr(delegate, "source", self.source)
            return delegate

    @property
    def resolved_adapter(self) -> UnifiedAudioAdapter | None:
        return self._delegate
