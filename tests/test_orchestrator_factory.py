import pytest
from fastapi import HTTPException

from music_insight.adapters.model_capabilities import (
    COMNI_CHAT_PROTOCOL,
    OPENAI_CHAT_PROTOCOL,
)
from music_insight.adapters.network_omni import NetworkOmniAdapter
from music_insight.adapters.openai_asr_verifier import OpenAIAsrVerifier
from music_insight.api.orchestrator_factory import (
    build_orchestrator,
    validate_private_model_endpoint,
)
from music_insight.config import Settings


def test_network_factory_builds_protocol_routing_adapter_without_io(tmp_path):
    orchestrator = build_orchestrator(
        Settings(workspace_dir=tmp_path),
        model_source="network",
        model_endpoint="http://192.168.50.20:19432",
    )

    assert isinstance(orchestrator.unified, NetworkOmniAdapter)
    assert orchestrator.unified.endpoint == "http://192.168.50.20:19432"
    assert set(orchestrator.unified.registry.protocols) == {
        OPENAI_CHAT_PROTOCOL,
        COMNI_CHAT_PROTOCOL,
    }
    assert orchestrator.unified.resolved_adapter is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:8004",
        "http://localhost:8004",
        "http://192.168.1.97:8005",
        "http://[::1]:8004",
    ],
)
def test_private_model_endpoint_accepts_local_and_lan(endpoint):
    assert validate_private_model_endpoint(endpoint) == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.com",
        "http://8.8.8.8:8004",
    ],
)
def test_private_model_endpoint_rejects_public_destinations(endpoint):
    with pytest.raises(HTTPException) as error:
        validate_private_model_endpoint(endpoint)

    assert error.value.status_code == 422


def test_factory_builds_configured_asr_verifier_without_hardcoded_port(tmp_path):
    orchestrator = build_orchestrator(
        Settings(
            workspace_dir=tmp_path,
            asr_verifier_enabled=True,
            asr_verifier_endpoint="http://192.168.50.20:19433",
            asr_verifier_transcriptions_path="api/transcribe",
            asr_verifier_model="custom-whisper",
            asr_verifier_timeout_seconds=123,
            asr_verifier_vad=False,
        ),
        model_source="network",
        model_endpoint="http://192.168.50.20:19432",
    )

    assert isinstance(orchestrator.asr_verifier, OpenAIAsrVerifier)
    assert orchestrator.asr_verifier.endpoint == "http://192.168.50.20:19433"
    assert orchestrator.asr_verifier.transcriptions_path == "/api/transcribe"
    assert orchestrator.asr_verifier.model == "custom-whisper"
    assert orchestrator.asr_verifier.timeout_seconds == 123
    assert orchestrator.asr_verifier.vad is False
    assert orchestrator.asr_gate is not None
    assert orchestrator.asr_gate is not orchestrator.model_gate


def test_factory_shares_gate_when_primary_and_verifier_use_same_endpoint(tmp_path):
    endpoint = "http://192.168.50.20:19432"
    orchestrator = build_orchestrator(
        Settings(
            workspace_dir=tmp_path,
            omni_max_concurrency=2,
            asr_verifier_enabled=True,
            asr_verifier_endpoint=endpoint,
            asr_verifier_max_concurrency=1,
        ),
        model_source="network",
        model_endpoint=endpoint,
    )

    assert orchestrator.asr_gate is orchestrator.model_gate
    assert orchestrator.asr_gate.limit == 1


def test_factory_rejects_public_asr_verifier_endpoint(tmp_path):
    with pytest.raises(HTTPException) as error:
        build_orchestrator(
            Settings(
                workspace_dir=tmp_path,
                asr_verifier_enabled=True,
                asr_verifier_endpoint="https://example.com",
            ),
            model_source="network",
            model_endpoint="http://127.0.0.1:8004",
        )

    assert error.value.status_code == 422
