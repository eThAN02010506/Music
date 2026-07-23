import asyncio

import httpx
import pytest

from music_insight.api.model_probe import (
    probe_model_endpoint,
    validate_model_endpoint,
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
    assert result.audio_supported is False
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
    assert result.audio_supported is True


def test_probe_rejects_non_http_endpoint():
    with pytest.raises(ValueError):
        validate_model_endpoint("file:///tmp/model")
