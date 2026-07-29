from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx


ModelProtocol = str
OPENAI_CHAT_PROTOCOL = "openai-chat"
COMNI_CHAT_PROTOCOL = "comni-ws-chat"


@dataclass(frozen=True, slots=True)
class ModelServiceCapabilities:
    endpoint: str
    online: bool
    model: str | None
    service: str
    protocol: ModelProtocol | None
    analysis_supported: bool | None
    audio_supported: bool | None
    openai_audio_supported: bool | None
    detail: str


@dataclass(frozen=True, slots=True)
class _ProbeResponse:
    success: bool
    payload: dict[str, Any] | None = None
    error: str | None = None


async def probe_model_service(
    endpoint: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 8.0,
) -> ModelServiceCapabilities:
    """Discover the usable analysis protocol without invoking inference."""

    endpoint = endpoint.rstrip("/")
    async with httpx.AsyncClient(
        timeout=timeout,
        trust_env=False,
        transport=transport,
        follow_redirects=False,
    ) as client:
        health, apps, version, models, props = await asyncio.gather(
            _get_json(client, f"{endpoint}/health"),
            _get_json(client, f"{endpoint}/api/apps"),
            _get_json(client, f"{endpoint}/version"),
            _get_json(client, f"{endpoint}/v1/models"),
            _get_json(client, f"{endpoint}/props"),
        )

    model = _model_name(models.payload or {})
    openai_audio_supported = _audio_capability(props.payload or {})
    has_turnbased = _has_turnbased_app(apps.payload or {})
    is_comni = health.success and has_turnbased
    online = any(
        item.success for item in (health, apps, version, models, props)
    )

    if is_comni:
        protocol: ModelProtocol | None = COMNI_CHAT_PROTOCOL
        analysis_supported = True
        audio_supported = True
    elif models.success:
        protocol = OPENAI_CHAT_PROTOCOL
        analysis_supported = openai_audio_supported
        audio_supported = openai_audio_supported
    else:
        protocol = None
        analysis_supported = None
        audio_supported = None

    service = _service_name(model, is_comni)
    if not online:
        errors = [
            item.error
            for item in (health, apps, version, models, props)
            if item.error
        ]
        detail = (
            "模型服务连接失败："
            + (errors[0] if errors else "所有能力探测端点均不可用。")
        )
    elif is_comni:
        openai_note = (
            "；OpenAI 音频路由关闭，但不影响 Gateway"
            if openai_audio_supported is False
            else ""
        )
        detail = f"服务在线，Comni Gateway 音频分析可用{openai_note}。"
    elif openai_audio_supported is True:
        detail = "服务在线，当前 OpenAI 路由已启用音频模态。"
    elif openai_audio_supported is False:
        detail = (
            "服务在线，但当前 OpenAI 路由未启用音频模态，"
            "且未检测到 Comni Turn-based Gateway。"
        )
    else:
        detail = "服务在线，但未声明音频模态；建议先用短音频验证。"

    return ModelServiceCapabilities(
        endpoint=endpoint,
        online=online,
        model=model,
        service=service,
        protocol=protocol,
        analysis_supported=analysis_supported,
        audio_supported=audio_supported,
        openai_audio_supported=openai_audio_supported,
        detail=detail,
    )


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
) -> _ProbeResponse:
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return _ProbeResponse(False, error=f"{response.url.path} 返回非对象 JSON。")
        return _ProbeResponse(True, payload=payload)
    except (httpx.HTTPError, ValueError) as exc:
        return _ProbeResponse(False, error=str(exc)[:300])


def _model_name(payload: dict[str, Any]) -> str | None:
    candidates = payload.get("data") or payload.get("models") or []
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0]
    if not isinstance(first, dict):
        return None
    value = first.get("id") or first.get("model") or first.get("name")
    return value if isinstance(value, str) and value else None


def _audio_capability(payload: dict[str, Any]) -> bool | None:
    modalities = payload.get("modalities")
    if not isinstance(modalities, dict):
        return None
    value = modalities.get("audio")
    return value if isinstance(value, bool) else None


def _has_turnbased_app(payload: dict[str, Any]) -> bool:
    apps = payload.get("apps")
    if not isinstance(apps, list):
        return False
    return any(
        isinstance(app, dict)
        and app.get("app_id") == "turnbased"
        and app.get("route") == "/turnbased"
        for app in apps
    )


def _service_name(model: str | None, is_comni: bool) -> str:
    lowered = (model or "").casefold()
    if is_comni:
        return "MiniCPM-o Comni Gateway" if "minicpm" in lowered else "Comni Gateway"
    if "minicpm" in lowered:
        return "MiniCPM-o"
    if "qwen" in lowered:
        return "Qwen Omni"
    return "OpenAI-compatible"
