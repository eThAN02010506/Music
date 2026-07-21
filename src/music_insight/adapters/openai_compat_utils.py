from __future__ import annotations

import ast
import json
import re
from typing import Any

import httpx


def api_path(value: str) -> str:
    return value if value.startswith("/") else f"/{value}"


async def discover_model(
    endpoint: str,
    models_path: str,
    timeout: float = 30.0,
) -> str:
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.get(f"{endpoint.rstrip('/')}{api_path(models_path)}")
    response.raise_for_status()
    payload = response.json()
    candidates = payload.get("data") or payload.get("models") or []
    if not candidates:
        raise RuntimeError("模型服务未返回可用模型。")
    first = candidates[0]
    model = first.get("id") or first.get("model") or first.get("name")
    if not isinstance(model, str) or not model:
        raise RuntimeError("模型服务返回的模型 ID 无效。")
    return model


def extract_chat_content(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Chat 响应缺少 choices[0].message.content。") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Chat 返回了空的最终文本。")
    return content


def parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Chat 未返回 JSON 对象。")
    candidate = text[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as original_error:
        parsed = _repair_json_object(candidate, original_error)
    if not isinstance(parsed, dict):
        raise ValueError("Chat JSON 顶层必须是对象。")
    return parsed


def _repair_json_object(candidate: str, original_error: Exception) -> dict[str, Any]:
    try:
        parsed = ast.literal_eval(candidate)
        if isinstance(parsed, dict):
            return parsed
    except (SyntaxError, ValueError):
        pass

    quoted_keys = re.sub(
        r'([\{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:',
        r'\1"\2":',
        candidate,
    )
    try:
        parsed = json.loads(quoted_keys)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    try:
        parsed = ast.literal_eval(quoted_keys)
        if isinstance(parsed, dict):
            return parsed
    except (SyntaxError, ValueError):
        pass
    raise original_error
