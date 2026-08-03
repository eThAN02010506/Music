from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import random
from typing import Any

import httpx

from music_insight.adapters.openai_compat_utils import (
    api_path,
    discover_model,
    extract_chat_content,
)
from music_insight.adapters.structured_omni import StructuredOmniAdapter


class OpenAIChatAudioAdapter(StructuredOmniAdapter):
    """Music analysis through an OpenAI-compatible audio Chat API."""

    source = "OpenAI-compatible audio"

    def __init__(
        self,
        endpoint: str,
        completions_path: str = "/v1/chat/completions",
        models_path: str = "/v1/models",
        model: str | None = None,
        chunk_seconds: float = 30.0,
        chunk_overlap_seconds: float = 1.5,
        display_name: str = "OpenAI-compatible audio",
    ) -> None:
        super().__init__(
            endpoint,
            model=model,
            chunk_seconds=chunk_seconds,
            chunk_overlap_seconds=chunk_overlap_seconds,
        )
        self.source = f"{display_name} · {self.endpoint}"
        self.completions_path = api_path(completions_path)
        self.models_path = api_path(models_path)
        self._http_client: httpx.AsyncClient | None = None
        self._json_schema_supported: bool | None = None

    @asynccontextmanager
    async def _request_scope(self) -> AsyncIterator[None]:
        async with self._client_scope():
            yield

    async def _chat(self, request: dict[str, Any], timeout: float) -> str:
        outbound_request = request
        uses_json_schema = (
            request.get("response_format", {}).get("type") == "json_schema"
        )
        if uses_json_schema and self._json_schema_supported is False:
            outbound_request = self._json_object_request(request)
        async with self._client_scope() as client:
            response = await self._post_with_retry(
                client,
                outbound_request,
                timeout=timeout,
            )
            if (
                response.status_code in {400, 422}
                and uses_json_schema
                and outbound_request is request
                and self._schema_format_unsupported(response)
            ):
                self._json_schema_supported = False
                response = await self._post_with_retry(
                    client,
                    self._json_object_request(request),
                    timeout=timeout,
                )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip().replace("\n", " ")[:500]
            raise RuntimeError(
                f"{self.source} HTTP {response.status_code}: {detail}"
            ) from exc
        return extract_chat_content(response.json())

    @staticmethod
    def _json_object_request(request: dict[str, Any]) -> dict[str, Any]:
        fallback_request = dict(request)
        fallback_request["response_format"] = {"type": "json_object"}
        return fallback_request

    @asynccontextmanager
    async def _client_scope(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._http_client is not None:
            yield self._http_client
            return
        async with httpx.AsyncClient(
            trust_env=False,
            limits=httpx.Limits(
                max_connections=4,
                max_keepalive_connections=2,
            ),
        ) as client:
            self._http_client = client
            try:
                yield client
            finally:
                self._http_client = None

    async def _post_with_retry(
        self,
        client: httpx.AsyncClient,
        request: dict[str, Any],
        *,
        timeout: float,
    ) -> httpx.Response:
        url = f"{self.endpoint}{self.completions_path}"
        request_timeout = httpx.Timeout(
            timeout,
            connect=10.0,
            write=90.0,
            pool=10.0,
        )
        retryable_statuses = {429, 502, 503, 504}
        for attempt in range(2):
            try:
                response = await client.post(
                    url,
                    json=request,
                    timeout=request_timeout,
                )
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadError,
                httpx.RemoteProtocolError,
            ):
                if attempt:
                    raise
            else:
                if response.status_code not in retryable_statuses or attempt:
                    return response
                retry_after = response.headers.get("retry-after", "")
                try:
                    delay = min(5.0, max(0.0, float(retry_after)))
                except ValueError:
                    delay = 0.4 + random.uniform(0.0, 0.2)
                await asyncio.sleep(delay)
                continue
            await asyncio.sleep(0.4 + random.uniform(0.0, 0.2))
        raise RuntimeError("模型请求重试状态异常。")

    @staticmethod
    def _schema_format_unsupported(response: httpx.Response) -> bool:
        detail = response.text.casefold()
        return any(
            marker in detail
            for marker in (
                "response_format",
                "json_schema",
                "structured output",
                "schema is not supported",
                "unsupported schema",
                "failed to initialize samplers",
                "failed to parse grammar",
            )
        )

    async def _model(self) -> str:
        if not self._resolved_model:
            self._resolved_model = await discover_model(
                self.endpoint,
                self.models_path,
                client=self._http_client,
            )
        return self._resolved_model
