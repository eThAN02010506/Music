from __future__ import annotations

import asyncio
import copy
import json
import math
from typing import Any

import pytest

from music_insight.adapters.structured_omni import StructuredOmniAdapter
from music_insight.adapters.structured_output import StructuredOutputError


def _chunk_payload() -> dict[str, Any]:
    return {
        "lyrics": [],
        "instruments": [],
        "sound_events": [],
        "emotion_timeline": [],
        "themes": [],
        "narrative": "",
    }


class _SequenceAdapter(StructuredOmniAdapter):
    def __init__(self, responses: list[dict[str, Any] | str]) -> None:
        super().__init__("http://127.0.0.1:9999", model="test-model")
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def _model(self) -> str:
        return "test-model"

    async def _chat(
        self,
        request: dict[str, Any],
        timeout: float,
    ) -> str:
        self.requests.append(request)
        response = self.responses.pop(0)
        return response if isinstance(response, str) else json.dumps(response)


class _FailingAdapter(_SequenceAdapter):
    def __init__(self, error: BaseException) -> None:
        super().__init__([])
        self.error = error
        self.calls = 0

    async def _chat(
        self,
        request: dict[str, Any],
        timeout: float,
    ) -> str:
        del request, timeout
        self.calls += 1
        raise self.error


class _SlowAdapter(_SequenceAdapter):
    def __init__(self) -> None:
        super().__init__([])
        self.calls = 0

    async def _chat(
        self,
        request: dict[str, Any],
        timeout: float,
    ) -> str:
        del request, timeout
        self.calls += 1
        await asyncio.sleep(0.04)
        return json.dumps({"event": self.calls})


def test_validated_chat_accepts_contract_without_retry() -> None:
    adapter = _SequenceAdapter([_chunk_payload()])
    request = {
        "messages": [{"role": "system", "content": "Return JSON."}],
        "response_format": adapter._chunk_response_format(),
    }

    result = asyncio.run(adapter._chat_json(request, 1))

    assert result == _chunk_payload()
    assert len(adapter.requests) == 1


def test_validated_chat_retries_syntactic_json_with_wrong_schema() -> None:
    adapter = _SequenceAdapter([{"event": "I'm back"}, _chunk_payload()])
    request = {
        "messages": [{"role": "system", "content": "Return JSON."}],
        "response_format": adapter._chunk_response_format(),
        "temperature": 0.7,
        "max_tokens": 5000,
    }

    result = asyncio.run(adapter._chat_json(request, 1))

    assert result == _chunk_payload()
    assert len(adapter.requests) == 2
    retry = adapter.requests[1]
    assert retry["response_format"] == request["response_format"]
    assert retry["temperature"] == 0
    assert retry["max_tokens"] == 1800
    assert "music_chunk_analysis" in retry["messages"][0]["content"]


def test_malformed_json_retry_uses_json_object_without_mutating_request() -> None:
    adapter = _SequenceAdapter(
        [
            '{"lyrics": [] "themes": []}',
            _chunk_payload(),
        ]
    )
    request = {
        "messages": [{"role": "system", "content": "Return JSON."}],
        "response_format": adapter._chunk_response_format(),
        "max_tokens": 1800,
    }
    original = copy.deepcopy(request)

    result = asyncio.run(adapter._chat_json(request, 1))

    assert result == _chunk_payload()
    assert request == original
    assert adapter.requests[1]["response_format"] == {"type": "json_object"}
    assert adapter.requests[1]["max_tokens"] == 1200


def test_validated_chat_rejects_two_wrong_schema_responses() -> None:
    adapter = _SequenceAdapter(
        [{"event": "first"}, {"event": "second"}],
    )
    request = {
        "messages": [{"role": "system", "content": "Return JSON."}],
        "response_format": adapter._chunk_response_format(),
    }

    with pytest.raises(StructuredOutputError, match="连续两次"):
        asyncio.run(adapter._chat_json(request, 1))

    assert len(adapter.requests) == 2


def test_recovery_contract_rejects_unrequested_extra_fields() -> None:
    adapter = _SequenceAdapter(
        [
            {"lyrics": [], "emotion_timeline": []},
            {"lyrics": [], "emotion_timeline": []},
        ]
    )
    request = {
        "messages": [{"role": "system", "content": "Return lyrics."}],
        "response_format": adapter._recovery_response_format(["lyrics"]),
    }

    with pytest.raises(StructuredOutputError, match="连续两次"):
        asyncio.run(adapter._chat_json(request, 1))


def test_final_contract_requires_atmosphere_basis() -> None:
    invalid = {
        "themes": [],
        "narrative": "summary",
        "inferred_atmosphere": [{"text": "calm", "confidence": 0.8}],
    }
    adapter = _SequenceAdapter([invalid, invalid])
    request = {
        "messages": [{"role": "system", "content": "Return report."}],
        "response_format": adapter._final_response_format(),
    }

    with pytest.raises(StructuredOutputError, match="连续两次"):
        asyncio.run(adapter._chat_json(request, 1))


@pytest.mark.parametrize(
    "invalid_value",
    [True, math.nan, math.inf, -math.inf],
)
def test_numeric_contract_rejects_boolean_and_nonfinite_values(
    invalid_value: float | bool,
) -> None:
    invalid = _chunk_payload()
    invalid["sound_events"] = [
        {
            "text": "tone",
            "start_s": invalid_value,
            "end_s": 1,
            "confidence": 0.5,
        }
    ]
    adapter = _SequenceAdapter([invalid, invalid])
    request = {
        "messages": [{"role": "system", "content": "Return JSON."}],
        "response_format": adapter._chunk_response_format(),
    }

    with pytest.raises(StructuredOutputError, match="连续两次"):
        asyncio.run(adapter._chat_json(request, 1))


def test_transport_error_and_cancellation_are_not_retried() -> None:
    request = {"messages": [], "response_format": {"type": "json_object"}}
    failed = _FailingAdapter(RuntimeError("transport failed"))

    with pytest.raises(RuntimeError, match="transport failed"):
        asyncio.run(failed._chat_json(request, 1))

    assert failed.calls == 1

    cancelled = _FailingAdapter(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled._chat_json(request, 1))
    assert cancelled.calls == 1


def test_schema_retry_shares_one_total_deadline() -> None:
    adapter = _SlowAdapter()
    request = {
        "messages": [{"role": "system", "content": "Return JSON."}],
        "response_format": adapter._chunk_response_format(),
    }

    with pytest.raises(TimeoutError):
        asyncio.run(adapter._chat_json(request, 0.06))

    assert adapter.calls == 2
