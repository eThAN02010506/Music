import asyncio

import httpx
import pytest

from music_insight.api.model_probe import (
    probe_model_endpoint,
    validate_model_endpoint,
)
from music_insight.adapters.model_capabilities import (
    clear_probe_cache,
    probe_model_service,
)


def test_probe_reports_online_model_without_audio_modality():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "MiniCPM-o-4_5-Q4_K_M.gguf",
                        }
                    ]
                },
            )
        if request.url.path == "/props":
            return httpx.Response(200, json={"modalities": {"audio": False}})
        return httpx.Response(404)

    result = asyncio.run(
        probe_model_endpoint(
            "http://127.0.0.1:8005/",
            transport=httpx.MockTransport(handler),
        )
    )

    assert result.online is True
    assert result.service == "MiniCPM-o"
    assert result.protocol == "openai-chat"
    assert result.analysis_supported is False
    assert result.audio_supported is False
    assert result.openai_audio_supported is False
    assert "未启用音频模态" in result.detail


def test_probe_accepts_audio_enabled_qwen_service():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"models": [{"name": "Qwen3-Omni"}]})
        return httpx.Response(200, json={"modalities": {"audio": True}})

    result = asyncio.run(
        probe_model_endpoint(
            "http://model.local:8004",
            transport=httpx.MockTransport(handler),
        )
    )

    assert result.online is True
    assert result.service == "Qwen Omni"
    assert result.protocol == "openai-chat"
    assert result.analysis_supported is True
    assert result.audio_supported is True
    assert result.openai_audio_supported is True


def test_probe_prefers_comni_turnbased_audio_over_disabled_openai_route():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.path == "/api/apps":
            return httpx.Response(
                200,
                json={
                    "apps": [
                        {
                            "app_id": "turnbased",
                            "name": "Turn-based Chat",
                            "route": "/turnbased",
                        }
                    ]
                },
            )
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "1.0.21"})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": "MiniCPM-o-4_5-Q4_K_M.gguf"}]},
            )
        if request.url.path == "/props":
            return httpx.Response(200, json={"modalities": {"audio": False}})
        return httpx.Response(404)

    result = asyncio.run(
        probe_model_endpoint(
            "http://model.local:9017",
            transport=httpx.MockTransport(handler),
        )
    )

    assert result.online is True
    assert result.service == "MiniCPM-o Comni Gateway"
    assert result.protocol == "comni-ws-chat"
    assert result.analysis_supported is True
    assert result.audio_supported is True
    assert result.openai_audio_supported is False
    assert "Gateway 音频分析可用" in result.detail


def test_probe_recognizes_gateway_without_openai_models_route():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.path == "/api/apps":
            return httpx.Response(
                200,
                json={
                    "apps": [
                        {
                            "app_id": "turnbased",
                            "route": "/turnbased",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    result = asyncio.run(
        probe_model_endpoint(
            "http://model.local:12345",
            transport=httpx.MockTransport(handler),
        )
    )

    assert result.online is True
    assert result.model is None
    assert result.protocol == "comni-ws-chat"
    assert result.analysis_supported is True


def test_probe_does_not_treat_duplex_only_gateway_as_batch_analysis():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.path == "/api/apps":
            return httpx.Response(
                200,
                json={
                    "apps": [
                        {
                            "app_id": "audio_duplex",
                            "route": "/audio_duplex",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    result = asyncio.run(
        probe_model_endpoint(
            "http://model.local:8005",
            transport=httpx.MockTransport(handler),
        )
    )

    assert result.online is True
    assert result.protocol is None
    assert result.analysis_supported is None


def test_probe_rejects_non_http_endpoint():
    with pytest.raises(ValueError):
        validate_model_endpoint("file:///tmp/model")


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://user@model.local:8004",
        "http://user:password@model.local:8004",
        "http://model.local:8004?token=secret",
        "http://model.local:8004#models",
        "http://model.local:99999",
        "http://model.local:not-a-port",
    ],
)
def test_model_endpoint_rejects_ambiguous_or_credentialed_urls(endpoint):
    with pytest.raises(ValueError):
        validate_model_endpoint(endpoint)


def test_probe_cache_serves_repeat_calls_without_reprobing():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if request.url.path == "/v1/models":
            return httpx.Response(
                200, json={"data": [{"id": "qwen"}]}
            )
        if request.url.path == "/props":
            return httpx.Response(200, json={"modalities": {"audio": True}})
        return httpx.Response(200, json={})

    async def exercise():
        clear_probe_cache("http://cache-probe.test:8004")
        transport = httpx.MockTransport(handler)
        first = await probe_model_service(
            "http://cache-probe.test:8004/",
            transport=transport,
        )
        second = await probe_model_service(
            "http://cache-probe.test:8004/",
            transport=transport,
        )
        return first, second

    first, second = asyncio.run(exercise())

    assert first.protocol == second.protocol
    # The first call probed all five endpoints; the second hit the cache and
    # added no further network round trips.
    assert calls["count"] == 5
    clear_probe_cache()
