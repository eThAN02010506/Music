from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from music_insight.api.accounts import UserPublic
from music_insight.api.debug import DEBUG_PAGE
from music_insight.api.dependencies import (
    get_current_user,
    validate_private_model_endpoint,
)
from music_insight.api.model_probe import (
    ModelProbeRequest,
    ModelProbeResult,
    probe_model_endpoint,
)
from music_insight.config import Settings, get_settings


router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "mode": "local-api",
    }


class RuntimeConfig(BaseModel):
    model_endpoint: str
    local_model_root: str
    local_runner_available: bool
    job_backend: str


@router.get("/runtime-config", response_model=RuntimeConfig)
async def runtime_config(
    _: UserPublic = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> RuntimeConfig:
    return RuntimeConfig(
        model_endpoint=settings.omni_endpoint,
        local_model_root=str(settings.local_model_root.resolve()),
        local_runner_available=shutil.which(settings.local_llama_server) is not None,
        job_backend=settings.job_backend,
    )


@router.post("/models/probe", response_model=ModelProbeResult)
async def probe_model(
    payload: ModelProbeRequest,
    _: UserPublic = Depends(get_current_user),
) -> ModelProbeResult:
    try:
        endpoint = validate_private_model_endpoint(payload.endpoint)
        return await probe_model_endpoint(endpoint)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(DEBUG_PAGE)


@router.get("/api/info")
async def api_info(
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return {
        "name": "Music Insight",
        "status": "ok",
        "endpoints": {
            "health": "/health",
            "register": "POST /auth/register",
            "login": "POST /auth/login",
            "runtime_config": "GET /runtime-config",
            "analyze": "POST /analyze",
            "markdown": "POST /analyze/markdown",
            "jobs": "POST /jobs",
            "remote_audio_job": "POST /jobs/from-url",
            "history": "GET /history",
            "singing_score": "POST /history/{id}/singing/score",
            "singing_compare": "POST /singing/compare",
            "singing_attempts": "GET /singing/attempts",
            "delete_singing_attempt": "DELETE /singing/attempts/{id}",
            "leaderboard": "GET /leaderboard",
            "leaderboard_preferences": "PATCH /auth/me",
            "docs": "/docs",
        },
        "pipeline": {
            "unified_model": (
                "能力探测 + Provider 适配器"
                "（登录后以 /runtime-config 配置为准）"
            ),
            "acoustic_metrics": "librosa (local)",
            "strategy": (
                f"OpenAI {settings.omni_chunk_seconds:g}s / "
                f"Comni {settings.comni_chunk_seconds:g}s audio chunks + "
                f"{settings.omni_chunk_overlap_seconds:g}-second overlap + "
                "same-model text fusion"
            ),
        },
    }
