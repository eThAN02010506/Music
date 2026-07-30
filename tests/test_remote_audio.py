from __future__ import annotations

import asyncio
import io
import wave

from fastapi import UploadFile
from fastapi.testclient import TestClient
import httpx
import pytest
from starlette.datastructures import Headers

from music_insight.api.app import app
from music_insight.api.routers import jobs as job_routes
from music_insight.api.services.remote_audio import (
    PublicOnlyNetworkBackend,
    RemoteAudioMediaTypeError,
    RemoteAudioTooLargeError,
    RemoteAudioUrlError,
    download_remote_audio,
    validate_remote_audio_url,
)
from music_insight.config import Settings, get_settings
from music_insight.schemas import AnalysisResult, DspResult


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/song.wav",
        "http://127.0.0.1/song.wav",
        "http://[::1]/song.wav",
        "http://10.0.0.2/song.wav",
        "http://user:secret@example.com/song.wav",
        "https://example.com/song.wav#fragment",
        "https://[not-an-ip]/song.wav",
        "https://example.com/song.wav\nx-test: injected",
        "https://[2001:4860:4860::8888%25en0]/song.wav",
    ],
)
def test_remote_audio_url_rejects_local_or_credentialed_targets(url: str) -> None:
    with pytest.raises(RemoteAudioUrlError):
        validate_remote_audio_url(url)


def test_remote_audio_url_normalizes_a_public_target() -> None:
    assert validate_remote_audio_url(
        "https://EXAMPLE.com/music/song.mp3?download=1"
    ) == "https://example.com/music/song.mp3?download=1"


def test_public_network_backend_rejects_mixed_dns_without_connecting() -> None:
    class ForbiddenBackend:
        async def connect_tcp(self, *args, **kwargs):
            raise AssertionError("private or mixed DNS must not connect")

    async def resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34", "127.0.0.1"]

    async def exercise() -> None:
        backend = PublicOnlyNetworkBackend(
            resolver=resolver,
            backend=ForbiddenBackend(),  # type: ignore[arg-type]
        )
        with pytest.raises(RemoteAudioUrlError):
            await backend.connect_tcp("example.com", 443)

    asyncio.run(exercise())


def test_remote_audio_download_streams_and_names_audio() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"].startswith("audio/")
        return httpx.Response(
            200,
            headers={"content-type": "audio/mpeg", "content-length": "6"},
            content=b"audio!",
        )

    async def exercise() -> tuple[str | None, str | None, bytes]:
        upload = await download_remote_audio(
            "https://example.com/test",
            max_bytes=100,
            timeout_seconds=10,
            transport=httpx.MockTransport(handler),
        )
        try:
            return upload.filename, upload.content_type, await upload.read()
        finally:
            await upload.close()

    filename, content_type, content = asyncio.run(exercise())
    assert filename == "test.mp3"
    assert content_type == "audio/mpeg"
    assert content == b"audio!"


def test_remote_audio_redirect_is_revalidated() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/private.wav"},
        )

    async def exercise() -> None:
        with pytest.raises(RemoteAudioUrlError):
            await download_remote_audio(
                "https://example.com/redirect",
                max_bytes=100,
                timeout_seconds=10,
                transport=httpx.MockTransport(handler),
            )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("headers", "content", "error"),
    [
        (
            {"content-type": "text/html"},
            b"<html>",
            RemoteAudioMediaTypeError,
        ),
        (
            {"content-type": "audio/wav", "content-length": "101"},
            b"",
            RemoteAudioTooLargeError,
        ),
        (
            {"content-type": "audio/wav"},
            b"x" * 101,
            RemoteAudioTooLargeError,
        ),
    ],
)
def test_remote_audio_enforces_type_and_size(
    headers: dict[str, str],
    content: bytes,
    error: type[Exception],
) -> None:
    async def exercise() -> None:
        with pytest.raises(error):
            await download_remote_audio(
                "https://example.com/audio",
                max_bytes=100,
                timeout_seconds=10,
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200,
                        headers=headers,
                        content=content,
                    )
                ),
            )

    asyncio.run(exercise())


def _wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(8_000)
        target.writeframes(b"\x00\x00" * 8_000)
    return output.getvalue()


def test_remote_audio_job_uses_the_existing_owned_analysis_pipeline(
    tmp_path,
    monkeypatch,
) -> None:
    class StubOrchestrator:
        async def analyze(self, _asset, progress=None):
            if progress is not None:
                await progress("dsp", 0.5, "test")
            return AnalysisResult(
                summary="remote complete",
                technical_metrics=DspResult(),
            )

    async def fake_download(*args, **kwargs) -> UploadFile:
        return UploadFile(
            io.BytesIO(_wav_bytes()),
            size=len(_wav_bytes()),
            filename="remote.wav",
            headers=Headers({"content-type": "audio/wav"}),
        )

    app.dependency_overrides[get_settings] = lambda: Settings(
        workspace_dir=tmp_path,
    )
    monkeypatch.setattr(job_routes, "download_remote_audio", fake_download)
    monkeypatch.setattr(
        job_routes,
        "get_orchestrator",
        lambda *args, **kwargs: StubOrchestrator(),
    )
    try:
        with TestClient(app) as client:
            registered = client.post(
                "/auth/register",
                json={"username": "remote-user", "password": "safe password"},
            )
            response = client.post(
                "/jobs/from-url",
                json={
                    "url": "https://example.com/remote.wav",
                    "language": "en",
                    "model_source": "network",
                },
            )
            history = client.get("/history")
    finally:
        app.dependency_overrides.clear()

    assert registered.status_code == 201
    assert response.status_code == 202
    assert history.status_code == 200
    assert history.json()[0]["file_name"] == "remote.wav"
