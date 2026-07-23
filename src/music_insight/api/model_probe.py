from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel


class ModelProbeRequest(BaseModel):
    endpoint: str


class ModelProbeResult(BaseModel):
    endpoint: str
    online: bool
    model: str | None = None
    audio_supported: bool | None = None
    service: str = "OpenAI-compatible"
    detail: str


def validate_model_endpoint(value: str) -> str:
    endpoint = value.strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Model endpoint must use http or https.")
    return endpoint


async def probe_model_endpoint(
    endpoint: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ModelProbeResult:
    endpoint = validate_model_endpoint(endpoint)
    try:
        async with httpx.AsyncClient(
            timeout=8,
            trust_env=False,
            transport=transport,
        ) as client:
            models_response = await client.get(f"{endpoint}/v1/models")
            models_response.raise_for_status()
            model = _model_name(models_response.json())
            audio_supported = None
            service = "OpenAI-compatible"
            try:
                props_response = await client.get(f"{endpoint}/props")
                if props_response.is_success:
                    props = props_response.json()
                    modalities = props.get("modalities")
                    if isinstance(modalities, dict):
                        raw_audio = modalities.get("audio")
                        if isinstance(raw_audio, bool):
                            audio_supported = raw_audio
            except (httpx.HTTPError, ValueError):
                pass

            if model and "minicpm" in model.lower():
                service = "MiniCPM-o"
            elif model and "qwen" in model.lower():
                service = "Qwen Omni"

            if audio_supported is True:
                detail = "服务在线，当前 OpenAI 路由已启用音频模态。"
            elif audio_supported is False:
                detail = (
                    "服务在线，但当前 OpenAI 路由未启用音频模态；"
                    "请加载音频 projector/mmproj 或使用专用 Gateway 接口。"
                )
            else:
                detail = "服务在线，但未声明音频模态；建议先用短音频验证。"
            return ModelProbeResult(
                endpoint=endpoint,
                online=True,
                model=model,
                audio_supported=audio_supported,
                service=service,
                detail=detail,
            )
    except (httpx.HTTPError, ValueError) as exc:
        return ModelProbeResult(
            endpoint=endpoint,
            online=False,
            detail=f"模型服务连接失败：{str(exc)[:500]}",
        )


def _model_name(payload: dict[str, Any]) -> str | None:
    candidates = payload.get("data") or payload.get("models") or []
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0]
    if not isinstance(first, dict):
        return None
    value = first.get("id") or first.get("model") or first.get("name")
    return value if isinstance(value, str) and value else None
