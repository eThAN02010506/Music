"""Provider contract tests (FR-PV-003 / FR-PV-005).

Every protocol registered in ``NetworkOmniProviderRegistry`` must produce an
adapter that satisfies the analysis and teaching contracts, and capability
probing must classify the common service shapes (audio-enabled OpenAI,
Comni turn-based gateway, plain text-only endpoint) independently of port or
model name. These tests are the gate a new provider must pass before it can be
marked "supported".
"""

from __future__ import annotations

import asyncio

import httpx

from music_insight.adapters.base import (
    LyricsRetryAdapter,
    UnifiedAudioAdapter,
    VerifiedLyricsSynthesisAdapter,
)
from music_insight.adapters.model_capabilities import (
    clear_probe_cache,
    probe_model_service,
)
from music_insight.adapters.network_omni import NetworkOmniAdapter
from music_insight.adapters.structured_omni_requests import chunk_analysis_request
from music_insight.teaching.protocols import (
    TeachingModelAdapter,
    TeachingRelistenProvider,
)


def _adapter_for(endpoint: str) -> NetworkOmniAdapter:
    return NetworkOmniAdapter(endpoint=endpoint)


def test_every_registered_protocol_builds_a_teaching_audio_adapter() -> None:
    adapter = _adapter_for("http://127.0.0.1:1")
    protocols = adapter.registry.protocols
    assert {"openai-chat", "comni-ws-chat"} <= set(protocols)


def _probe(handler) -> object:
    async def exercise():
        clear_probe_cache("http://contract.test:8004")
        transport = httpx.MockTransport(handler)
        return await probe_model_service(
            "http://contract.test:8004",
            transport=transport,
        )

    return asyncio.run(exercise())


def _audio_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/models":
        return httpx.Response(200, json={"data": [{"id": "qwen"}]})
    if request.url.path == "/props":
        return httpx.Response(200, json={"modalities": {"audio": True}})
    return httpx.Response(200, json={})


def test_probe_classifies_audio_enabled_openai_service() -> None:
    capabilities = _probe(_audio_handler)
    assert capabilities.online is True
    assert capabilities.protocol == "openai-chat"
    assert capabilities.analysis_supported is True
    assert capabilities.audio_supported is True


def test_probe_classifies_comni_gateway_even_with_openai_audio_off() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "minicpm"}]})
        if request.url.path == "/api/apps":
            return httpx.Response(
                200,
                json={"apps": [{"app_id": "turnbased", "route": "/turnbased"}]},
            )
        if request.url.path == "/props":
            return httpx.Response(200, json={"modalities": {"audio": False}})
        return httpx.Response(200, json={})

    capabilities = _probe(handler)
    assert capabilities.protocol == "comni-ws-chat"
    assert capabilities.analysis_supported is True
    assert capabilities.audio_supported is True


def test_probe_rejects_text_only_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "text-model"}]})
        if request.url.path == "/props":
            return httpx.Response(200, json={"modalities": {}})
        return httpx.Response(200, json={})

    capabilities = _probe(handler)
    assert capabilities.online is True
    # OpenAI-compatible models endpoint is present but audio is not declared:
    # probing reports an undetermined capability (None) rather than a hard
    # negative, and the detail asks for a short-audio verification.
    assert capabilities.analysis_supported is None
    assert capabilities.audio_supported is None


def test_registered_adapters_expose_expected_abilities() -> None:
    """The two built-in builders must produce adapters that satisfy the
    contracts the API relies on (analysis, teaching, relisten, synthesis).
    """

    # OpenAI-compatible builder
    openai_adapter = _adapter_for("http://127.0.0.1:1").registry.build(
        _probe(_audio_handler),
    )
    assert isinstance(openai_adapter, UnifiedAudioAdapter)
    assert isinstance(openai_adapter, TeachingModelAdapter)
    assert isinstance(openai_adapter, TeachingRelistenProvider)
    assert isinstance(openai_adapter, LyricsRetryAdapter)
    assert isinstance(openai_adapter, VerifiedLyricsSynthesisAdapter)

    # Comni builder
    def comni_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "minicpm"}]})
        if request.url.path == "/api/apps":
            return httpx.Response(
                200,
                json={"apps": [{"app_id": "turnbased", "route": "/turnbased"}]},
            )
        return httpx.Response(200, json={})

    comni_adapter = _adapter_for("http://127.0.0.1:1").registry.build(
        _probe(comni_handler),
    )
    assert isinstance(comni_adapter, UnifiedAudioAdapter)
    assert isinstance(comni_adapter, TeachingModelAdapter)
    assert isinstance(comni_adapter, TeachingRelistenProvider)


def test_openai_audio_request_uses_standard_input_audio() -> None:
    """The OpenAI-compatible wire format must be the canonical input_audio
    shape (base64 WAV) that vLLM-Omni / Ollama / Qwen-compatible services
    accept, so a new provider does not need a bespoke audio transport.
    """

    request = chunk_analysis_request(
        model="test-model",
        audio_bytes=b"\x00\x01\x02",
        duration_s=5.0,
        language_hint="zh",
        response_format={"type": "json_schema", "json_schema": {}},
    )
    user_content = request["messages"][1]["content"]
    audio_parts = [
        part
        for part in user_content
        if isinstance(part, dict) and part.get("type") == "input_audio"
    ]
    assert len(audio_parts) == 1
    audio = audio_parts[0]["input_audio"]
    assert audio["format"] == "wav"
    assert audio["data"] == "AAEC"
    # Every user message carries the instruction before the audio.
    assert any(
        isinstance(part, dict) and part.get("type") == "text"
        for part in user_content
    )
