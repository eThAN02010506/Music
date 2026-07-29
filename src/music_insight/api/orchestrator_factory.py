from __future__ import annotations

import ipaddress
from pathlib import Path
import shutil
from urllib.parse import urlparse

from fastapi import HTTPException

from music_insight.adapters.dsp import BasicDspAdapter
from music_insight.adapters.local_omni import (
    LocalModelConfigurationError,
    LocalOmniServer,
    ManagedLocalOmniAdapter,
)
from music_insight.adapters.network_omni import NetworkOmniAdapter
from music_insight.adapters.openai_asr_verifier import OpenAIAsrVerifier
from music_insight.api.model_probe import validate_model_endpoint
from music_insight.config import Settings
from music_insight.pipeline.orchestrator import AnalysisOrchestrator
from music_insight.pipeline.preprocess import Preprocessor
from music_insight.pipeline.resources import model_resources


def validate_private_model_endpoint(endpoint: str) -> str:
    """Validate that a model endpoint stays on this machine or private LAN."""

    validated = validate_model_endpoint(endpoint)
    host = urlparse(validated).hostname
    if host == "localhost":
        return validated
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="模型地址须使用 localhost 或局域网 IP。",
        ) from exc
    if not (address.is_private or address.is_loopback or address.is_link_local):
        raise HTTPException(
            status_code=422,
            detail="仅允许连接本机或局域网模型地址。",
        )
    return validated


def create_local_server(settings: Settings) -> LocalOmniServer:
    """Create one child-process owner for one FastAPI application instance."""

    return LocalOmniServer(
        root=Path(settings.local_model_root),
        endpoint=settings.local_omni_endpoint,
        executable=settings.local_llama_server,
    )


def build_orchestrator(
    settings: Settings,
    *,
    model_source: str = "network",
    model_endpoint: str | None = None,
    local_model_path: str | None = None,
    local_server: LocalOmniServer | None = None,
) -> AnalysisOrchestrator:
    """Compose one analysis use case without depending on FastAPI injection."""

    if model_source == "local":
        if not local_model_path:
            raise HTTPException(status_code=422, detail="Local model path is required.")
        server = local_server or create_local_server(settings)
        try:
            server.resolve(local_model_path)
        except LocalModelConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if shutil.which(settings.local_llama_server) is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"未找到 {settings.local_llama_server}。请先安装支持该 Qwen Omni "
                    "GGUF 的 llama.cpp，或填写本机 OpenAI 兼容接口地址。"
                ),
            )
        unified = ManagedLocalOmniAdapter(
            server=server,
            model_path=local_model_path,
            completions_path=settings.omni_completions_path,
            models_path=settings.omni_models_path,
            model=settings.omni_model,
            chunk_seconds=settings.omni_chunk_seconds,
            chunk_overlap_seconds=settings.omni_chunk_overlap_seconds,
        )
    else:
        try:
            endpoint = validate_private_model_endpoint(
                model_endpoint or settings.omni_endpoint
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="Model endpoint must use http or https.",
            ) from exc
        unified = NetworkOmniAdapter(
            endpoint=endpoint,
            completions_path=settings.omni_completions_path,
            models_path=settings.omni_models_path,
            model=settings.omni_model,
            chunk_seconds=settings.omni_chunk_seconds,
            chunk_overlap_seconds=settings.omni_chunk_overlap_seconds,
            comni_chunk_seconds=settings.comni_chunk_seconds,
            comni_open_timeout=settings.comni_open_timeout_seconds,
            comni_first_event_timeout=(
                settings.comni_first_event_timeout_seconds
            ),
            comni_idle_timeout=settings.comni_idle_timeout_seconds,
            comni_request_timeout=settings.comni_request_timeout_seconds,
            comni_max_message_bytes=settings.comni_max_message_mb * 1024 * 1024,
        )
    model_limit = (
        1 if model_source == "local" else settings.omni_max_concurrency
    )
    asr_verifier = None
    asr_gate = None
    if settings.asr_verifier_enabled:
        try:
            asr_endpoint = validate_private_model_endpoint(
                settings.asr_verifier_endpoint
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="ASR verifier endpoint must use http or https.",
            ) from exc
        asr_verifier = OpenAIAsrVerifier(
            endpoint=asr_endpoint,
            dialect=settings.asr_verifier_dialect,
            transcriptions_path=settings.asr_verifier_transcriptions_path,
            model=settings.asr_verifier_model,
            api_key=(
                settings.asr_verifier_api_key.get_secret_value()
                if settings.asr_verifier_api_key is not None
                else None
            ),
            timeout_seconds=settings.asr_verifier_timeout_seconds,
            vad=settings.asr_verifier_vad,
        )

    if (
        asr_verifier is not None
        and asr_verifier.endpoint.rstrip("/") == unified.endpoint.rstrip("/")
    ):
        shared_gate = model_resources.gate(
            unified.endpoint,
            min(model_limit, settings.asr_verifier_max_concurrency),
        )
        model_gate = shared_gate
        asr_gate = shared_gate
    else:
        model_gate = model_resources.gate(unified.endpoint, model_limit)
        if asr_verifier is not None:
            asr_gate = model_resources.gate(
                asr_verifier.endpoint,
                settings.asr_verifier_max_concurrency,
            )

    return AnalysisOrchestrator(
        unified=unified,
        dsp=BasicDspAdapter(),
        preprocessor=Preprocessor(workspace_dir=settings.workspace_dir),
        dsp_gate=model_resources.gate(
            "music-insight://local-dsp",
            settings.dsp_max_concurrency,
        ),
        model_gate=model_gate,
        asr_verifier=asr_verifier,
        asr_gate=asr_gate,
    )
