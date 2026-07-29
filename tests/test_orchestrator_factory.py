import pytest
from fastapi import HTTPException

from music_insight.adapters.model_capabilities import (
    COMNI_CHAT_PROTOCOL,
    OPENAI_CHAT_PROTOCOL,
)
from music_insight.adapters.network_omni import NetworkOmniAdapter
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
