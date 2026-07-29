import io
from unittest.mock import patch
import wave

from fastapi.testclient import TestClient

from music_insight.api.app import app
from music_insight.config import Settings, get_settings
from music_insight.singing_score import SingingScore


def _score() -> SingingScore:
    return SingingScore(
        total=88,
        pitch=90,
        rhythm=84,
        completeness=92,
        stability=82,
        median_pitch_error=0.4,
        in_tune_ratio=0.75,
        reference_duration_s=30,
        performance_duration_s=29,
        pitch_curve=[],
        notes=[],
    )


def _wav_bytes(seconds: float = 1.0, sample_rate: int = 8_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return buffer.getvalue()


def _registered_client() -> TestClient:
    client = TestClient(app)
    response = client.post(
        "/auth/register",
        json={"username": "test-singer", "password": "safe password"},
    )
    assert response.status_code == 201
    return client


def test_compare_singing_uploads_scores_and_removes_temporary_files(tmp_path):
    app.dependency_overrides[get_settings] = lambda: Settings(
        workspace_dir=tmp_path
    )
    try:
        client = _registered_client()
        with patch(
            "music_insight.api.services.singing.score_singing",
            return_value=_score(),
        ):
            response = client.post(
                "/singing/compare",
                files={
                    "reference": ("reference.wav", _wav_bytes(), "audio/wav"),
                    "performance": (
                        "performance.wav",
                        _wav_bytes(),
                        "audio/wav",
                    ),
                },
            )
        leaderboard = client.get("/leaderboard")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["total"] == 88
    assert leaderboard.status_code == 200
    assert leaderboard.json()["entries"][0]["total"] == 88
    assert leaderboard.json()["entries"][0]["source"] == "standalone"
    assert not [
        path
        for path in (tmp_path / "users").rglob("*")
        if path.is_file()
    ]


def test_compare_singing_uploads_rejects_non_audio_reference(tmp_path):
    app.dependency_overrides[get_settings] = lambda: Settings(
        workspace_dir=tmp_path
    )
    try:
        response = _registered_client().post(
            "/singing/compare",
            files={
                "reference": ("reference.txt", b"text", "text/plain"),
                "performance": (
                    "performance.wav",
                    _wav_bytes(),
                    "audio/wav",
                ),
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 415
