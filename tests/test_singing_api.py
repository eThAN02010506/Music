import io
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
import wave

from fastapi.testclient import TestClient

from music_insight.api.accounts import AccountStore
from music_insight.api.app import app
from music_insight.config import Settings, get_settings
from music_insight.singing_score import SingingScore


def _score(total: int = 88) -> SingingScore:
    return SingingScore(
        total=total,
        pitch=min(total + 2, 100),
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


def _registered_client(username: str = "test-singer") -> TestClient:
    client = TestClient(app)
    response = client.post(
        "/auth/register",
        json={"username": username, "password": "safe password"},
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
        hidden_leaderboard = client.get("/leaderboard")
        visibility = client.patch(
            "/auth/me",
            json={"leaderboard_visible": True},
        )
        leaderboard = client.get("/leaderboard")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["total"] == 88
    assert hidden_leaderboard.json()["entries"] == []
    assert visibility.status_code == 200
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


def test_attempt_history_is_paginated_scoped_and_deletable(tmp_path):
    app.dependency_overrides[get_settings] = lambda: Settings(
        workspace_dir=tmp_path
    )
    try:
        first_client = _registered_client("first-singer")
        second_client = _registered_client("second-singer")
        accounts = AccountStore(tmp_path / "history.sqlite3")
        first_user = first_client.get("/auth/me").json()
        second_user = second_client.get("/auth/me").json()
        first_client.patch(
            "/auth/me",
            json={"leaderboard_visible": True},
        )
        second_client.patch(
            "/auth/me",
            json={"leaderboard_visible": True},
        )
        start = datetime(2026, 7, 30, 9, tzinfo=UTC)
        first_old = accounts.record_score(
            first_user["id"],
            _score(85),
            source="standalone",
            reference_name="first-reference.wav",
            performance_name="first-old.wav",
            created_at=start,
        )
        first_new = accounts.record_score(
            first_user["id"],
            _score(95),
            source="standalone",
            reference_name="first-reference.wav",
            performance_name="first-new.wav",
            created_at=start + timedelta(minutes=1),
        )
        second_attempt = accounts.record_score(
            second_user["id"],
            _score(90),
            source="standalone",
            performance_name="second.wav",
            created_at=start + timedelta(minutes=2),
        )

        first_page = first_client.get(
            "/singing/attempts",
            params={"limit": 1, "offset": 1},
        )
        cursor_page = first_client.get(
            "/singing/attempts",
            params={
                "limit": 1,
                "before_created_at": first_new.created_at.isoformat(),
                "before_id": first_new.id,
            },
        )
        incomplete_cursor = first_client.get(
            "/singing/attempts",
            params={"limit": 1, "before_id": first_new.id},
        )
        blank_cursor = first_client.get(
            "/singing/attempts",
            params={"limit": 1, "before_id": "   "},
        )
        mixed_pagination = first_client.get(
            "/singing/attempts",
            params={
                "limit": 1,
                "offset": 1,
                "before_created_at": first_new.created_at.isoformat(),
                "before_id": first_new.id,
            },
        )
        foreign_delete = first_client.delete(
            f"/singing/attempts/{second_attempt.id}"
        )
        second_history = second_client.get("/singing/attempts")
        before_delete_board = first_client.get("/leaderboard")
        own_delete = first_client.delete(
            f"/singing/attempts/{first_new.id}"
        )
        first_history = first_client.get("/singing/attempts")
        promoted_board = first_client.get("/leaderboard")
        delete_last_first = first_client.delete(
            f"/singing/attempts/{first_old.id}"
        )
        one_user_board = first_client.get("/leaderboard")
        delete_last_second = second_client.delete(
            f"/singing/attempts/{second_attempt.id}"
        )
        empty_board = first_client.get("/leaderboard")
        invalid_page = first_client.get(
            "/singing/attempts",
            params={"offset": -1},
        )
        anonymous = TestClient(app)
        anonymous_history = anonymous.get("/singing/attempts")
        anonymous_delete = anonymous.delete(
            f"/singing/attempts/{first_old.id}"
        )
    finally:
        app.dependency_overrides.clear()

    assert first_page.status_code == 200
    assert [item["id"] for item in first_page.json()] == [first_old.id]
    assert all(item["user_id"] == first_user["id"] for item in first_page.json())
    assert cursor_page.status_code == 200
    assert [item["id"] for item in cursor_page.json()] == [first_old.id]
    assert incomplete_cursor.status_code == 422
    assert blank_cursor.status_code == 422
    assert mixed_pagination.status_code == 422
    assert foreign_delete.status_code == 404
    assert [item["id"] for item in second_history.json()] == [second_attempt.id]
    assert [
        (item["rank"], item["total"])
        for item in before_delete_board.json()["entries"]
    ] == [(1, 95), (2, 90)]
    assert own_delete.status_code == 204
    assert [item["id"] for item in first_history.json()] == [first_old.id]
    assert [
        (item["rank"], item["total"], item["is_current_user"])
        for item in promoted_board.json()["entries"]
    ] == [(1, 90, False), (2, 85, True)]
    assert promoted_board.json()["entries"][1]["attempts"] == 1
    assert delete_last_first.status_code == 204
    assert [
        (item["rank"], item["total"])
        for item in one_user_board.json()["entries"]
    ] == [(1, 90)]
    assert delete_last_second.status_code == 204
    assert empty_board.json()["entries"] == []
    assert invalid_page.status_code == 422
    assert anonymous_history.status_code == 401
    assert anonymous_delete.status_code == 401
