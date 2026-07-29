from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from music_insight.api.services import teaching_runtime
from music_insight.config import get_settings
from music_insight.teaching.protocols import (
    TeachingModelAdapter,
    TeachingRelistenProvider,
)


class _History:
    def get(self, history_id: str, *, user_id: str):
        assert history_id == "song-1"
        assert user_id == "user-1"
        return SimpleNamespace(
            model_source="network",
            model_location="http://192.168.1.97:8005",
        )


class _Gate:
    def __init__(self) -> None:
        self.entries = 0
        self.exits = 0

    async def __aenter__(self):
        self.entries += 1

    async def __aexit__(self, *_):
        self.exits += 1


class _Adapter:
    async def build_understanding_map(self, context: Any):
        return context

    async def answer_music_question(self, context: Any):
        return context

    async def listen_to_excerpts(self, request: Any):
        return request


def test_runtime_restores_saved_provider_and_reuses_orchestrator_gate(
    monkeypatch,
) -> None:
    adapter = _Adapter()
    gate = _Gate()
    captured: dict[str, Any] = {}

    def build(settings, **options):
        captured.update(options)
        return SimpleNamespace(unified=adapter, model_gate=gate)

    monkeypatch.setattr(teaching_runtime, "build_orchestrator", build)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(local_server=object()))
    )

    runtime = asyncio.run(
        teaching_runtime.resolve_teaching_runtime(
            request=request,
            history=_History(),
            history_id="song-1",
            user_id="user-1",
            settings=get_settings(),
            model_override=None,
            relisten_override=None,
        )
    )

    assert captured["model_source"] == "network"
    assert captured["model_endpoint"] == "http://192.168.1.97:8005"
    assert isinstance(runtime.model, TeachingModelAdapter)
    assert isinstance(runtime.relisten, TeachingRelistenProvider)
    marker = object()
    assert asyncio.run(runtime.model.build_understanding_map(marker)) is marker
    assert asyncio.run(runtime.relisten.listen_to_excerpts(marker)) is marker
    assert gate.entries == gate.exits == 2


def test_explicit_runtime_override_does_not_read_history() -> None:
    adapter = _Adapter()

    class _UnreadableHistory:
        def get(self, *_args, **_kwargs):
            raise AssertionError("override must bypass history resolution")

    runtime = asyncio.run(
        teaching_runtime.resolve_teaching_runtime(
            request=SimpleNamespace(),
            history=_UnreadableHistory(),
            history_id="song-1",
            user_id="user-1",
            settings=get_settings(),
            model_override=adapter,
            relisten_override=None,
        )
    )

    assert runtime.model is adapter
    assert runtime.relisten is adapter
