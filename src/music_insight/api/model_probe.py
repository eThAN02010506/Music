from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from music_insight.adapters.model_capabilities import (
    clear_probe_cache,
    probe_model_service,
)


class ModelProbeRequest(BaseModel):
    endpoint: str


class ModelProbeResult(BaseModel):
    endpoint: str
    online: bool
    model: str | None = None
    protocol: str | None = None
    analysis_supported: bool | None = None
    audio_supported: bool | None = None
    openai_audio_supported: bool | None = None
    service: str = "OpenAI-compatible"
    detail: str


def validate_model_endpoint(value: str) -> str:
    endpoint = value.strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Model endpoint must use http or https.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Model endpoint must not contain user credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError("Model endpoint must not contain a query or fragment.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Model endpoint contains an invalid port.") from exc
    return endpoint


async def probe_model_endpoint(
    endpoint: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ModelProbeResult:
    endpoint = validate_model_endpoint(endpoint)
    # The "test connection" action must always reflect the live service, not a
    # cached teaching-runtime probe.
    clear_probe_cache(endpoint)
    result = await probe_model_service(
        endpoint,
        transport=transport,
        timeout=8,
    )
    return ModelProbeResult(
        endpoint=result.endpoint,
        online=result.online,
        model=result.model,
        protocol=result.protocol,
        analysis_supported=result.analysis_supported,
        audio_supported=result.audio_supported,
        openai_audio_supported=result.openai_audio_supported,
        service=result.service,
        detail=result.detail,
    )
