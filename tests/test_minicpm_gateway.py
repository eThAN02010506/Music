from __future__ import annotations

import asyncio
import base64
import io
import json
from typing import Any
import wave

import numpy as np
import pytest

from music_insight.adapters.minicpm_gateway import (
    MiniCpmGatewayClient,
    MiniCpmGatewayError,
    MiniCpmGatewayProtocolError,
    openai_request_to_comni,
    wav_to_float32_base64,
)


class FakeWebSocket:
    def __init__(self, frames: list[str | bytes | BaseException]) -> None:
        self.frames = list(frames)
        self.sent: list[str | bytes] = []

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def recv(self, decode: bool | None = None) -> str | bytes:
        del decode
        if not self.frames:
            raise AssertionError("Test WebSocket ran out of frames.")
        frame = self.frames.pop(0)
        if isinstance(frame, BaseException):
            raise frame
        return frame


class FakeConnection:
    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket
        self.exited = False

    async def __aenter__(self) -> FakeWebSocket:
        return self.websocket

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool:
        del exc_type, exc, traceback
        self.exited = True
        return False


class FakeConnectFactory:
    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.connections: list[FakeConnection] = []

    def __call__(self, uri: str, **kwargs: Any) -> FakeConnection:
        self.calls.append((uri, kwargs))
        connection = FakeConnection(self.websocket)
        self.connections.append(connection)
        return connection


def _pcm16_wav(
    samples: np.ndarray,
    *,
    sample_rate: int = 16_000,
    channels: int = 1,
) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(samples.astype("<i2", copy=False).tobytes())
    return output.getvalue()


def _event(event_type: str, **values: Any) -> str:
    return json.dumps({"type": event_type, **values})


def test_wav_to_float32_base64_emits_raw_little_endian_samples():
    samples = np.asarray(
        [-32768, -16384, 0, 16384, 32767],
        dtype="<i2",
    )

    encoded = wav_to_float32_base64(_pcm16_wav(samples))
    decoded = np.frombuffer(base64.b64decode(encoded), dtype="<f4")

    np.testing.assert_allclose(
        decoded,
        samples.astype(np.float32) / 32768.0,
        rtol=0,
        atol=0,
    )
    assert len(base64.b64decode(encoded)) == samples.size * 4


@pytest.mark.parametrize(
    ("sample_rate", "channels"),
    [(8_000, 1), (16_000, 2)],
)
def test_wav_to_float32_base64_rejects_non_gateway_audio(
    sample_rate: int,
    channels: int,
):
    samples = np.asarray([0, 1, -1, 2] * channels, dtype="<i2")

    with pytest.raises(
        MiniCpmGatewayProtocolError,
        match="16 kHz 单声道 PCM16 WAV",
    ):
        wav_to_float32_base64(
            _pcm16_wav(
                samples,
                sample_rate=sample_rate,
                channels=channels,
            )
        )


def test_request_conversion_uses_comni_audio_and_explicitly_disables_tts():
    samples = np.asarray([-32768, 0, 32767], dtype="<i2")
    wav_data = base64.b64encode(_pcm16_wav(samples)).decode("ascii")
    request = {
        "messages": [
            {"role": "system", "content": "Only return JSON."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Analyze this audio."},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "format": "wav",
                            "data": wav_data,
                        },
                    },
                ],
            },
        ],
        "max_tokens": 8_000,
        "temperature": 3.0,
        "response_format": {"type": "json_schema"},
    }

    payload = openai_request_to_comni(request)

    assert payload["tts"] == {"enabled": False}
    assert payload["streaming"] is False
    assert payload["omni_mode"] is False
    assert payload["enable_thinking"] is False
    assert payload["generation"] == {
        "max_new_tokens": 4096,
        "temperature": 2.0,
        "do_sample": True,
        "length_penalty": 1.0,
    }
    audio = payload["messages"][1]["content"][1]
    assert audio["type"] == "audio"
    assert audio["sample_rate"] == 16_000
    np.testing.assert_allclose(
        np.frombuffer(base64.b64decode(audio["data"]), dtype="<f4"),
        samples.astype(np.float32) / 32768.0,
        rtol=0,
        atol=0,
    )
    assert "input_audio" not in json.dumps(payload)
    assert "response_format" not in payload


def test_client_consumes_progress_chunks_and_done_without_returning_audio():
    websocket = FakeWebSocket(
        [
            _event("prefill_done", input_tokens=12),
            _event("chunk", text_delta='{"answer":'),
            _event("status", message="working"),
            _event("chunk", text_delta='"ok"}'),
            _event("done", text="", audio_data="must-not-be-returned"),
        ]
    )
    factory = FakeConnectFactory(websocket)
    client = MiniCpmGatewayClient(
        "http://model.local:8005",
        connect_factory=factory,
        open_timeout=3,
        close_timeout=4,
        max_message_bytes=4096,
    )
    payload = {"messages": [{"role": "user", "content": "hello"}]}

    result = asyncio.run(client.request_text(payload, timeout=5))

    assert result == '{"answer":"ok"}'
    assert [json.loads(message) for message in websocket.sent] == [payload]
    assert factory.connections[0].exited is True

    uri, kwargs = factory.calls[0]
    assert uri == "ws://model.local:8005/ws/chat"
    assert kwargs["proxy"] is None
    assert kwargs["max_size"] == 4096
    assert isinstance(kwargs["max_size"], int)
    assert kwargs["compression"] is None
    assert kwargs["host"] == "model.local"
    assert kwargs["port"] == 8005
    assert kwargs["open_timeout"] == 3
    assert kwargs["close_timeout"] == 4


def test_client_prefers_nonempty_done_text_over_streamed_chunks():
    websocket = FakeWebSocket(
        [
            _event("chunk", text_delta="partial"),
            _event("done", text="complete"),
        ]
    )
    client = MiniCpmGatewayClient(
        "https://gateway.example/base/",
        connect_factory=FakeConnectFactory(websocket),
    )

    result = asyncio.run(client.request_text({"messages": []}, timeout=5))

    assert result == "complete"


def test_client_turns_gateway_error_event_into_bounded_adapter_error():
    websocket = FakeWebSocket(
        [_event("error", error="GPU busy", audio_data="ignored")]
    )
    client = MiniCpmGatewayClient(
        "http://model.local:8005",
        connect_factory=FakeConnectFactory(websocket),
    )

    with pytest.raises(MiniCpmGatewayError, match="GPU busy"):
        asyncio.run(client.request_text({"messages": []}, timeout=5))


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (b'{"type":"done","text":"binary"}', "非文本"),
        ("not-json", "无效 JSON"),
        ("[]", "JSON 对象"),
        (_event("done", text="", audio_data="ignored"), "缺少文本"),
    ],
)
def test_client_rejects_malformed_or_incomplete_frames(
    frame: str | bytes,
    message: str,
):
    client = MiniCpmGatewayClient(
        "http://model.local:8005",
        connect_factory=FakeConnectFactory(FakeWebSocket([frame])),
    )

    with pytest.raises(MiniCpmGatewayProtocolError, match=message):
        asyncio.run(client.request_text({"messages": []}, timeout=5))


def test_client_converts_event_timeout_without_network_access():
    client = MiniCpmGatewayClient(
        "http://model.local:8005",
        connect_factory=FakeConnectFactory(
            FakeWebSocket([TimeoutError("simulated event timeout")])
        ),
    )

    with pytest.raises(MiniCpmGatewayError, match="未返回下一事件"):
        asyncio.run(client.request_text({"messages": []}, timeout=5))
