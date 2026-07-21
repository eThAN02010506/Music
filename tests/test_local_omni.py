from pathlib import Path

import pytest
from fastapi import HTTPException

from music_insight.adapters.local_omni import (
    LocalModelConfigurationError,
    LocalOmniServer,
)
from music_insight.api.app import get_orchestrator
from music_insight.config import Settings


def test_local_server_resolves_model_directory_and_projector(tmp_path: Path):
    model = tmp_path / "Qwen-Omni.gguf"
    projector = tmp_path / "mmproj-Qwen-Omni.gguf"
    model.write_bytes(b"model")
    projector.write_bytes(b"projector")
    server = LocalOmniServer(
        root=tmp_path,
        endpoint="http://127.0.0.1:8010",
        executable="llama-server",
    )

    assert server.resolve(".") == (model, projector)
    assert server.resolve(str(model)) == (model, projector)


def test_local_server_rejects_paths_outside_allowed_root(tmp_path: Path):
    root = tmp_path / "models"
    root.mkdir()
    outside = tmp_path / "outside.gguf"
    outside.write_bytes(b"model")
    server = LocalOmniServer(
        root=root,
        endpoint="http://127.0.0.1:8010",
        executable="llama-server",
    )

    with pytest.raises(LocalModelConfigurationError, match="允许目录"):
        server.resolve(str(outside))


def test_local_server_requires_audio_projector(tmp_path: Path):
    (tmp_path / "model.gguf").write_bytes(b"model")
    server = LocalOmniServer(
        root=tmp_path,
        endpoint="http://127.0.0.1:8010",
        executable="llama-server",
    )

    with pytest.raises(LocalModelConfigurationError, match="mmproj"):
        server.resolve(".")


def test_default_orchestrator_keeps_8004():
    orchestrator = get_orchestrator(Settings())

    assert orchestrator.unified.endpoint == "http://192.168.1.97:8004"


def test_local_orchestrator_reports_missing_runner_before_job(tmp_path: Path):
    (tmp_path / "model.gguf").write_bytes(b"model")
    (tmp_path / "mmproj-model.gguf").write_bytes(b"projector")
    settings = Settings(
        local_model_root=tmp_path,
        local_llama_server="definitely-not-a-real-llama-server",
    )

    with pytest.raises(HTTPException, match="llama.cpp") as raised:
        get_orchestrator(settings, model_source="local", local_model_path=".")

    assert raised.value.status_code == 422
