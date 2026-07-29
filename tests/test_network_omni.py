from __future__ import annotations

import asyncio
from typing import Any

import pytest

from music_insight.adapters.base import UnifiedAudioAdapter
from music_insight.adapters.minicpm_gateway import MiniCpmGatewayAdapter
from music_insight.adapters.model_capabilities import (
    COMNI_CHAT_PROTOCOL,
    OPENAI_CHAT_PROTOCOL,
    ModelServiceCapabilities,
)
from music_insight.adapters.network_omni import (
    NetworkOmniAdapter,
    NetworkOmniProviderRegistry,
)
from music_insight.adapters.openai_chat_audio import OpenAIChatAudioAdapter


class _FakeAdapter(UnifiedAudioAdapter):
    source = "Fake provider"

    async def analyze(
        self,
        asset: Any,
        dsp: Any,
        progress: Any = None,
    ) -> Any:
        raise AssertionError("The fake provider should not run inference.")


class _RetryCapableFakeAdapter(_FakeAdapter):
    def __init__(self) -> None:
        self.retry_calls: list[tuple[bytes, float, str | None]] = []

    async def retry_lyrics(
        self,
        audio_bytes: bytes,
        duration_s: float,
        language_hint: str | None,
    ) -> tuple[list[Any], list[str]]:
        self.retry_calls.append((audio_bytes, duration_s, language_hint))
        return [], ["fake-quality-issue"]


def _capabilities(
    endpoint: str,
    protocol: str | None,
    *,
    analysis_supported: bool | None = True,
    online: bool = True,
    detail: str = "ready",
) -> ModelServiceCapabilities:
    audio_supported = analysis_supported
    return ModelServiceCapabilities(
        endpoint=endpoint,
        online=online,
        model="test-model",
        service="Test service",
        protocol=protocol,
        analysis_supported=analysis_supported,
        audio_supported=audio_supported,
        openai_audio_supported=(
            audio_supported if protocol == OPENAI_CHAT_PROTOCOL else False
        ),
        detail=detail,
    )


def test_provider_registry_registers_and_builds_custom_protocol() -> None:
    registry = NetworkOmniProviderRegistry()
    provider = _FakeAdapter()
    capabilities = _capabilities("http://model.local:9911", "custom-chat")

    registry.register("custom-chat", lambda discovered: provider)

    assert registry.protocols == ("custom-chat",)
    assert registry.build(capabilities) is provider


def test_provider_registry_rejects_duplicate_and_unknown_protocols() -> None:
    registry = NetworkOmniProviderRegistry()
    registry.register("custom-chat", lambda discovered: _FakeAdapter())

    with pytest.raises(ValueError, match="already registered"):
        registry.register("custom-chat", lambda discovered: _FakeAdapter())

    with pytest.raises(RuntimeError, match="not-registered"):
        registry.build(
            _capabilities(
                "http://model.local:9912",
                "not-registered",
            )
        )


@pytest.mark.parametrize(
    ("endpoint", "protocol", "expected_type"),
    [
        (
            "http://127.0.0.1:39117",
            OPENAI_CHAT_PROTOCOL,
            OpenAIChatAudioAdapter,
        ),
        (
            "http://127.0.0.1:39118",
            COMNI_CHAT_PROTOCOL,
            MiniCpmGatewayAdapter,
        ),
        # Port 8005 is not a provider identity. If its discovered protocol is
        # OpenAI chat, it must not be routed to the MiniCPM adapter.
        (
            "http://127.0.0.1:8005",
            OPENAI_CHAT_PROTOCOL,
            OpenAIChatAudioAdapter,
        ),
        # Conversely, a Comni gateway remains Comni on a traditionally
        # Qwen-associated port.
        (
            "http://127.0.0.1:8004",
            COMNI_CHAT_PROTOCOL,
            MiniCpmGatewayAdapter,
        ),
    ],
)
def test_network_adapter_selects_provider_by_capability_not_port(
    endpoint: str,
    protocol: str,
    expected_type: type[UnifiedAudioAdapter],
) -> None:
    probe_calls: list[str] = []

    async def probe(candidate: str) -> ModelServiceCapabilities:
        probe_calls.append(candidate)
        return _capabilities(candidate, protocol)

    adapter = NetworkOmniAdapter(endpoint=endpoint, probe=probe)
    resolved = asyncio.run(adapter._resolve())

    assert isinstance(resolved, expected_type)
    assert probe_calls == [endpoint]


def test_network_adapter_caches_concurrent_capability_probe() -> None:
    endpoint = "http://model.local:42001"
    provider = _FakeAdapter()
    registry = NetworkOmniProviderRegistry()
    registry.register("custom-chat", lambda discovered: provider)
    probe_calls = 0

    async def probe(candidate: str) -> ModelServiceCapabilities:
        nonlocal probe_calls
        probe_calls += 1
        await asyncio.sleep(0)
        return _capabilities(candidate, "custom-chat")

    async def resolve_concurrently() -> list[UnifiedAudioAdapter]:
        adapter = NetworkOmniAdapter(
            endpoint=endpoint,
            registry=registry,
            probe=probe,
        )
        delegates = await asyncio.gather(
            *(adapter._resolve() for _ in range(8))
        )
        assert adapter.resolved_adapter is provider
        return delegates

    delegates = asyncio.run(resolve_concurrently())

    assert probe_calls == 1
    assert all(delegate is provider for delegate in delegates)


def test_network_adapter_rejects_explicitly_unsupported_analysis() -> None:
    endpoint = "http://model.local:42002"
    builder_calls = 0
    registry = NetworkOmniProviderRegistry()

    def build_provider(
        discovered: ModelServiceCapabilities,
    ) -> UnifiedAudioAdapter:
        nonlocal builder_calls
        builder_calls += 1
        return _FakeAdapter()

    registry.register(OPENAI_CHAT_PROTOCOL, build_provider)

    async def probe(candidate: str) -> ModelServiceCapabilities:
        return _capabilities(
            candidate,
            OPENAI_CHAT_PROTOCOL,
            analysis_supported=False,
            detail="Audio analysis route is disabled.",
        )

    adapter = NetworkOmniAdapter(
        endpoint=endpoint,
        registry=registry,
        probe=probe,
    )

    with pytest.raises(RuntimeError, match="Audio analysis route is disabled"):
        asyncio.run(adapter._resolve())

    assert builder_calls == 0
    assert adapter.resolved_adapter is None


def test_network_adapter_delegates_retry_by_runtime_capability() -> None:
    endpoint = "http://model.local:42003"
    provider = _RetryCapableFakeAdapter()
    registry = NetworkOmniProviderRegistry()
    registry.register("custom-chat", lambda discovered: provider)

    async def probe(candidate: str) -> ModelServiceCapabilities:
        return _capabilities(candidate, "custom-chat")

    adapter = NetworkOmniAdapter(
        endpoint=endpoint,
        registry=registry,
        probe=probe,
    )

    result = asyncio.run(adapter.retry_lyrics(b"wav-data", 12.5, "zh"))

    assert result == ([], ["fake-quality-issue"])
    assert provider.retry_calls == [(b"wav-data", 12.5, "zh")]
    assert adapter.source == provider.source


def test_network_adapter_rejects_retry_without_capability() -> None:
    endpoint = "http://model.local:42004"
    registry = NetworkOmniProviderRegistry()
    registry.register("custom-chat", lambda discovered: _FakeAdapter())

    async def probe(candidate: str) -> ModelServiceCapabilities:
        return _capabilities(candidate, "custom-chat")

    adapter = NetworkOmniAdapter(
        endpoint=endpoint,
        registry=registry,
        probe=probe,
    )

    with pytest.raises(RuntimeError, match="不支持歌词重听"):
        asyncio.run(adapter.retry_lyrics(b"wav-data", 3.0, None))
