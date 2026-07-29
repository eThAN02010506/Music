from __future__ import annotations

import copy
from itertools import islice
import math
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match


class StructuredOutputError(ValueError):
    """Raised when a model returns JSON that violates the requested contract."""


def validate_structured_output(
    payload: dict[str, Any],
    request: dict[str, Any],
) -> None:
    """Validate model JSON against the request's strict response schema."""

    _reject_nonfinite_numbers(payload)
    response_format = request.get("response_format")
    if not isinstance(response_format, dict):
        return
    if response_format.get("type") != "json_schema":
        return
    schema_container = response_format.get("json_schema")
    if not isinstance(schema_container, dict):
        raise StructuredOutputError("response_format 缺少 json_schema。")
    schema = schema_container.get("schema")
    if not isinstance(schema, dict):
        raise StructuredOutputError("response_format 缺少 schema 对象。")

    validator = Draft202012Validator(schema)
    # A model response is untrusted input. Bound validation work even when a
    # large array violates the schema at every element.
    errors = list(islice(validator.iter_errors(payload), 16))
    error = best_match(errors)
    if error is None:
        return
    location = ".".join(str(part) for part in error.absolute_path)
    prefix = f"{location}: " if location else ""
    detail = f"{prefix}{error.message}".replace("\n", " ")[:500]
    raise StructuredOutputError(f"模型 JSON 不符合结构契约：{detail}")


def schema_retry_request(
    request: dict[str, Any],
    error: ValueError,
    *,
    json_object_fallback: bool,
) -> dict[str, Any]:
    """Build one bounded retry while preserving the original strict schema."""

    retry = copy.deepcopy(request)
    retry["temperature"] = 0
    token_cap = 1200 if json_object_fallback else 1800
    retry["max_tokens"] = min(
        max(1, int(retry.get("max_tokens", 1200))),
        token_cap,
    )
    schema_name, required = _schema_summary(retry)
    if json_object_fallback:
        retry["response_format"] = {"type": "json_object"}
    instruction = (
        "上一次输出不是可接受的结构化 JSON。"
        f"错误：{str(error)[:300]}。"
        f"本次必须返回 schema={schema_name} 的单个 JSON 对象"
        f"，且顶层字段必须严格为：{required}；不得增加其他字段。"
    )
    messages = retry.get("messages")
    if not isinstance(messages, list):
        messages = []
        retry["messages"] = messages
    if messages and isinstance(messages[0], dict):
        content = messages[0].get("content")
        if isinstance(content, str):
            messages[0]["content"] = f"{content} {instruction}"
            return retry
    messages.insert(0, {"role": "system", "content": instruction})
    return retry


def _schema_summary(request: dict[str, Any]) -> tuple[str, str]:
    response_format = request.get("response_format")
    if not isinstance(response_format, dict):
        return "unknown", "以原请求为准"
    container = response_format.get("json_schema")
    if not isinstance(container, dict):
        return "unknown", "以原请求为准"
    name = str(container.get("name") or "unknown")
    schema = container.get("schema")
    required = schema.get("required") if isinstance(schema, dict) else None
    if not isinstance(required, list):
        return name, "以原请求为准"
    fields = "、".join(str(field) for field in required[:12])
    return name, fields or "无必填字段"


def _reject_nonfinite_numbers(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise StructuredOutputError(f"模型 JSON 包含非有限数字：{path}")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_nonfinite_numbers(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite_numbers(item, f"{path}.{str(key)[:80]}")
