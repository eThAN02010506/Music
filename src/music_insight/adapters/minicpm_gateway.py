from __future__ import annotations

import asyncio
import base64
import binascii
import copy
from contextlib import AbstractAsyncContextManager
import io
import json
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit
import wave

import numpy as np
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    SecurityError,
)

from music_insight.adapters.model_capabilities import (
    COMNI_CHAT_PROTOCOL,
    probe_model_service,
)
from music_insight.adapters.openai_compat_utils import parse_json_object
from music_insight.adapters.structured_omni import StructuredOmniAdapter


class MiniCpmGatewayError(RuntimeError):
    """Base error for bounded Comni Gateway requests."""


class MiniCpmGatewayProtocolError(MiniCpmGatewayError):
    """Raised when the Gateway violates the documented event protocol."""


class _WebSocket(Protocol):
    async def send(self, message: str | bytes) -> None: ...

    async def recv(self, decode: bool | None = None) -> str | bytes: ...


class _ConnectFactory(Protocol):
    def __call__(
        self,
        uri: str,
        **kwargs: Any,
    ) -> AbstractAsyncContextManager[_WebSocket]: ...


class MiniCpmGatewayClient:
    """One-request Comni `/ws/chat` client with explicit resource bounds."""

    def __init__(
        self,
        endpoint: str,
        *,
        connect_factory: _ConnectFactory = websocket_connect,
        open_timeout: float = 10.0,
        first_event_timeout: float = 180.0,
        idle_timeout: float = 180.0,
        request_timeout: float = 600.0,
        close_timeout: float = 5.0,
        max_message_bytes: int = 8 * 1024 * 1024,
        max_text_chars: int = 2_000_000,
        max_events: int = 10_000,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.ws_url, self.host, self.port = _websocket_target(self.endpoint)
        self._connect_factory = connect_factory
        self.open_timeout = max(1.0, float(open_timeout))
        self.first_event_timeout = max(1.0, float(first_event_timeout))
        self.idle_timeout = max(1.0, float(idle_timeout))
        self.request_timeout = max(1.0, float(request_timeout))
        self.close_timeout = max(1.0, float(close_timeout))
        self.max_message_bytes = max(1024, int(max_message_bytes))
        self.max_text_chars = max(1024, int(max_text_chars))
        self.max_events = max(8, int(max_events))

    async def request_text(
        self,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        total_timeout = min(
            self.request_timeout,
            max(1.0, float(timeout or self.request_timeout)),
        )
        try:
            async with asyncio.timeout(total_timeout):
                return await self._request_text(encoded)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise MiniCpmGatewayError(
                f"MiniCPM Gateway 请求超过 {total_timeout:g} 秒。"
            ) from exc
        except MiniCpmGatewayError:
            raise
        except (
            ConnectionClosed,
            InvalidHandshake,
            SecurityError,
            OSError,
        ) as exc:
            raise MiniCpmGatewayError(
                f"MiniCPM Gateway WebSocket 连接失败：{str(exc)[:400]}"
            ) from exc

    async def _request_text(self, encoded: str) -> str:
        connector = self._connect_factory(
            self.ws_url,
            open_timeout=self.open_timeout,
            ping_interval=20.0,
            ping_timeout=30.0,
            close_timeout=self.close_timeout,
            max_size=self.max_message_bytes,
            max_queue=4,
            compression=None,
            proxy=None,
            # Supplying the original destination explicitly makes websockets
            # reject cross-origin redirects while preserving same-origin ones.
            host=self.host,
            port=self.port,
        )
        async with connector as websocket:
            await websocket.send(encoded)
            chunks: list[str] = []
            total_chars = 0
            for event_index in range(self.max_events):
                event_timeout = (
                    self.first_event_timeout
                    if event_index == 0
                    else self.idle_timeout
                )
                try:
                    async with asyncio.timeout(event_timeout):
                        raw = await websocket.recv()
                except TimeoutError as exc:
                    raise MiniCpmGatewayError(
                        "MiniCPM Gateway 长时间未返回下一事件。"
                    ) from exc
                event = _decode_event(raw)
                event.pop("audio_data", None)
                event_type = event.get("type")

                if event_type == "chunk":
                    delta = event.get("text_delta")
                    if isinstance(delta, str) and delta:
                        total_chars += len(delta)
                        if total_chars > self.max_text_chars:
                            raise MiniCpmGatewayProtocolError(
                                "MiniCPM Gateway 文本结果超过安全上限。"
                            )
                        chunks.append(delta)
                    continue
                if event_type == "done":
                    text = event.get("text")
                    if not isinstance(text, str) or not text.strip():
                        text = "".join(chunks)
                    if not text.strip():
                        raise MiniCpmGatewayProtocolError(
                            "MiniCPM Gateway 完成事件缺少文本结果。"
                        )
                    if len(text) > self.max_text_chars:
                        raise MiniCpmGatewayProtocolError(
                            "MiniCPM Gateway 文本结果超过安全上限。"
                        )
                    return text
                if event_type == "error":
                    detail = str(event.get("error") or "未知 Gateway 错误")
                    raise MiniCpmGatewayError(
                        f"MiniCPM Gateway 推理失败：{detail[:500]}"
                    )
                if event_type in {
                    "queued",
                    "queue_done",
                    "prefill_done",
                    "status",
                }:
                    continue
                # Future Gateway versions may add non-terminal progress events.
                # They are tolerated but remain bounded by max_events/timeout.
            raise MiniCpmGatewayProtocolError(
                "MiniCPM Gateway 事件数量超过安全上限。"
            )


class MiniCpmGatewayAdapter(StructuredOmniAdapter):
    """Structured music analysis through MiniCPM-o Comni turn-based chat."""

    source = "MiniCPM-o Comni Gateway"

    def __init__(
        self,
        endpoint: str,
        *,
        model: str | None = None,
        models_path: str = "/v1/models",
        chunk_seconds: float = 15.0,
        chunk_overlap_seconds: float = 1.5,
        client: MiniCpmGatewayClient | None = None,
    ) -> None:
        super().__init__(
            endpoint,
            model=model,
            chunk_seconds=chunk_seconds,
            chunk_overlap_seconds=chunk_overlap_seconds,
        )
        self.models_path = models_path
        self.source = f"MiniCPM-o Comni · {self.endpoint}"
        self.client = client or MiniCpmGatewayClient(self.endpoint)

    async def _model(self) -> str:
        if self._resolved_model:
            return self._resolved_model
        capabilities = await probe_model_service(self.endpoint)
        if (
            not capabilities.online
            or capabilities.protocol != COMNI_CHAT_PROTOCOL
        ):
            raise MiniCpmGatewayError(
                "端点未提供可用的 Comni Turn-based Gateway。"
            )
        self._resolved_model = capabilities.model or "MiniCPM-o-4.5"
        return self._resolved_model

    async def _chat_json(
        self,
        request: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        content = await self._chat(request, timeout)
        try:
            return parse_json_object(content)
        except ValueError:
            retry_request = copy.deepcopy(request)
            retry_request["max_tokens"] = min(
                int(retry_request.get("max_tokens", 1200)),
                1200,
            )
            messages = retry_request.get("messages") or []
            if messages and isinstance(messages[0].get("content"), str):
                messages[0]["content"] += (
                    " 上一次输出不是可解析 JSON。请缩短结果，"
                    "严格检查逗号、引号和括号，只输出一个 JSON 对象。"
                )
            repaired = await self._chat(retry_request, timeout)
            return parse_json_object(repaired)

    async def _chat(self, request: dict[str, Any], timeout: float) -> str:
        payload = openai_request_to_comni(request)
        return await self.client.request_text(payload, timeout=timeout)


def openai_request_to_comni(request: dict[str, Any]) -> dict[str, Any]:
    """Map the workflow's canonical request into the Comni Chat schema."""

    raw_messages = request.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise MiniCpmGatewayProtocolError("分析请求缺少 messages。")
    messages: list[dict[str, Any]] = []
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            raise MiniCpmGatewayProtocolError("分析消息格式无效。")
        role = raw_message.get("role")
        if role not in {"system", "user", "assistant"}:
            raise MiniCpmGatewayProtocolError("分析消息角色无效。")
        messages.append(
            {
                "role": role,
                "content": _convert_content(raw_message.get("content")),
            }
        )

    max_tokens = min(4096, max(1, int(request.get("max_tokens", 1200))))
    temperature = max(0.0, min(2.0, float(request.get("temperature", 0.0))))
    return {
        "messages": messages,
        "streaming": False,
        "generation": {
            "max_new_tokens": max_tokens,
            "temperature": temperature,
            "do_sample": temperature > 0,
            "length_penalty": 1.0,
        },
        # TTS defaults to true in the official Gateway. It must be explicit.
        "tts": {"enabled": False},
        "omni_mode": False,
        "enable_thinking": False,
    }


def wav_to_float32_base64(audio_bytes: bytes) -> str:
    """Convert normalized PCM16 WAV bytes to raw little-endian Float32 PCM."""

    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getframerate() != 16_000
                or source.getsampwidth() != 2
                or source.getcomptype() != "NONE"
            ):
                raise MiniCpmGatewayProtocolError(
                    "MiniCPM Gateway 仅接收 16 kHz 单声道 PCM16 WAV。"
                )
            frame_count = source.getnframes()
            frames = source.readframes(frame_count)
    except MiniCpmGatewayProtocolError:
        raise
    except (EOFError, wave.Error) as exc:
        raise MiniCpmGatewayProtocolError("输入不是有效的 PCM WAV。") from exc

    if frame_count <= 0 or len(frames) != frame_count * 2:
        raise MiniCpmGatewayProtocolError("输入 WAV 为空或数据不完整。")
    pcm16 = np.frombuffer(frames, dtype="<i2")
    pcm32 = (pcm16.astype(np.float32) / 32768.0).astype("<f4", copy=False)
    return base64.b64encode(pcm32.tobytes()).decode("ascii")


def _convert_content(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise MiniCpmGatewayProtocolError("分析消息 content 格式无效。")

    converted: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            raise MiniCpmGatewayProtocolError("多模态消息项格式无效。")
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            converted.append({"type": "text", "text": item["text"]})
            continue
        if item.get("type") == "input_audio":
            audio = item.get("input_audio")
            if (
                not isinstance(audio, dict)
                or audio.get("format") != "wav"
                or not isinstance(audio.get("data"), str)
            ):
                raise MiniCpmGatewayProtocolError("OpenAI 音频消息格式无效。")
            try:
                wav_bytes = base64.b64decode(audio["data"], validate=True)
            except (binascii.Error, ValueError) as exc:
                raise MiniCpmGatewayProtocolError(
                    "OpenAI 音频 Base64 无效。"
                ) from exc
            converted.append(
                {
                    "type": "audio",
                    "data": wav_to_float32_base64(wav_bytes),
                    "sample_rate": 16_000,
                }
            )
            continue
        raise MiniCpmGatewayProtocolError(
            f"Comni 暂不支持消息类型：{str(item.get('type'))[:80]}"
        )
    return converted


def _decode_event(raw: str | bytes) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise MiniCpmGatewayProtocolError(
            "MiniCPM Gateway 返回了非文本 WebSocket 帧。"
        )
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MiniCpmGatewayProtocolError(
            "MiniCPM Gateway 返回了无效 JSON 事件。"
        ) from exc
    if not isinstance(event, dict):
        raise MiniCpmGatewayProtocolError(
            "MiniCPM Gateway 事件必须是 JSON 对象。"
        )
    return event


def _websocket_target(endpoint: str) -> tuple[str, str, int]:
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("MiniCPM Gateway endpoint must use http or https.")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = f"{parsed.path.rstrip('/')}/ws/chat"
    return (
        urlunsplit((scheme, parsed.netloc, path, "", "")),
        parsed.hostname,
        port,
    )
