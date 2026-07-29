from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Never

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from music_insight.api.history import HistoryStore
from music_insight.api.orchestrator_factory import build_orchestrator
from music_insight.api.services.history import require_history
from music_insight.config import Settings
from music_insight.teaching.models import (
    MapGenerationContext,
    MusicUnderstandingMap,
    RelistenRequest,
    RelistenResult,
    TeachingChatContext,
    TeachingChatResponse,
)
from music_insight.teaching.protocols import (
    TeachingModelAdapter,
    TeachingRelistenProvider,
)


@dataclass(frozen=True, slots=True)
class TeachingRuntime:
    model: TeachingModelAdapter | None
    relisten: TeachingRelistenProvider | None


class _GatedTeachingAdapter:
    """Share the exact endpoint gate used by whole-song analysis."""

    def __init__(
        self,
        adapter: TeachingModelAdapter,
        gate: AbstractAsyncContextManager[None],
    ) -> None:
        self._adapter = adapter
        self._gate = gate

    async def build_understanding_map(
        self,
        context: MapGenerationContext,
    ) -> MusicUnderstandingMap:
        async with self._gate:
            return await self._adapter.build_understanding_map(context)

    async def answer_music_question(
        self,
        context: TeachingChatContext,
    ) -> TeachingChatResponse:
        async with self._gate:
            return await self._adapter.answer_music_question(context)

    async def listen_to_excerpts(
        self,
        request: RelistenRequest,
    ) -> RelistenResult:
        if not isinstance(self._adapter, TeachingRelistenProvider):
            raise RuntimeError("当前模型协议不支持局部音频重听。")
        async with self._gate:
            return await self._adapter.listen_to_excerpts(request)


class _UnavailableTeachingAdapter:
    """Make provider restoration failures visible to the service fallback."""

    def __init__(self, error: Exception) -> None:
        detail = str(error).strip() or error.__class__.__name__
        self._detail = detail[:500]

    def _raise(self) -> Never:
        raise RuntimeError(f"无法恢复该歌曲使用的统一模型：{self._detail}")

    async def build_understanding_map(
        self,
        context: MapGenerationContext,
    ) -> MusicUnderstandingMap:
        del context
        self._raise()

    async def answer_music_question(
        self,
        context: TeachingChatContext,
    ) -> TeachingChatResponse:
        del context
        self._raise()


async def resolve_teaching_runtime(
    *,
    request: Request,
    history: HistoryStore,
    history_id: str,
    user_id: str,
    settings: Settings,
    model_override: TeachingModelAdapter | None,
    relisten_override: TeachingRelistenProvider | None,
) -> TeachingRuntime:
    """Resolve the provider saved with this song, never a client-supplied URL."""

    if model_override is not None or relisten_override is not None:
        relisten = relisten_override
        if relisten is None and isinstance(model_override, TeachingRelistenProvider):
            relisten = model_override
        return TeachingRuntime(model=model_override, relisten=relisten)

    entry = await run_in_threadpool(
        require_history,
        history,
        history_id,
        user_id,
    )
    try:
        orchestrator = build_orchestrator(
            settings,
            model_source=entry.model_source,
            model_endpoint=(
                entry.model_location
                if entry.model_source == "network"
                else None
            ),
            local_model_path=(
                entry.model_location
                if entry.model_source == "local"
                else None
            ),
            local_server=request.app.state.local_server,
        )
    except (HTTPException, OSError, RuntimeError, ValueError) as exc:
        unavailable = _UnavailableTeachingAdapter(exc)
        return TeachingRuntime(model=unavailable, relisten=None)

    candidate = orchestrator.unified
    if not isinstance(candidate, TeachingModelAdapter):
        unavailable = _UnavailableTeachingAdapter(
            RuntimeError("当前 Provider 没有音乐导赏能力。")
        )
        return TeachingRuntime(model=unavailable, relisten=None)
    if orchestrator.model_gate is None:
        return TeachingRuntime(
            model=candidate,
            relisten=(
                candidate
                if isinstance(candidate, TeachingRelistenProvider)
                else None
            ),
        )
    gated = _GatedTeachingAdapter(candidate, orchestrator.model_gate)
    return TeachingRuntime(
        model=gated,
        relisten=(
            gated
            if isinstance(candidate, TeachingRelistenProvider)
            else None
        ),
    )
