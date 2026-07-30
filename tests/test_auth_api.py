from datetime import UTC, datetime

from fastapi.testclient import TestClient

from music_insight.api.accounts import AccountStore
from music_insight.api.app import app
from music_insight.api.history import HistoryStore
from music_insight.config import Settings, get_settings
from music_insight.singing_score import SingingScore


def _register(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/register",
        json={"username": username, "password": "safe password"},
    )
    assert response.status_code == 201
    return response.json()


def _score(total: int) -> SingingScore:
    return SingingScore(
        total=total,
        pitch=total,
        rhythm=total - 2,
        completeness=total + 2,
        stability=total - 1,
        median_pitch_error=0.4,
        in_tune_ratio=0.8,
        reference_duration_s=30,
        performance_duration_s=29,
        pitch_curve=[],
        notes=[],
    )


def test_auth_cookie_history_debug_and_audio_are_user_scoped(tmp_path):
    app.dependency_overrides[get_settings] = lambda: Settings(
        workspace_dir=tmp_path
    )
    first_client = TestClient(app)
    second_client = TestClient(app)
    anonymous = TestClient(app)
    try:
        assert anonymous.get("/history").status_code == 401
        first = _register(first_client, "first-user")
        second = _register(second_client, "second-user")

        history = HistoryStore(tmp_path / "history.sqlite3")
        now = datetime.now(UTC)
        first_audio = tmp_path / "first.wav"
        second_audio = tmp_path / "second.wav"
        first_audio.write_bytes(b"first")
        second_audio.write_bytes(b"second")
        history.create(
            job_id="first-job",
            title="First song",
            file_name="first.wav",
            language="en",
            state="completed",
            created_at=now,
            updated_at=now,
            audio_path=first_audio,
            user_id=first["id"],
        )
        history.create(
            job_id="second-job",
            title="Second song",
            file_name="second.wav",
            language="zh",
            state="completed",
            created_at=now,
            updated_at=now,
            audio_path=second_audio,
            user_id=second["id"],
        )

        assert [
            item["id"] for item in first_client.get("/history").json()
        ] == ["first-job"]
        assert [
            item["id"] for item in second_client.get("/history").json()
        ] == ["second-job"]
        assert first_client.get("/history/second-job").status_code == 404
        assert first_client.get("/history/second-job/audio").status_code == 404
        assert first_client.patch(
            "/history/second-job",
            json={"title": "stolen"},
        ).status_code == 404
        debug = first_client.get("/debug/state")
        assert debug.status_code == 200
        assert [item["id"] for item in debug.json()["history"]] == ["first-job"]

        logout = first_client.post("/auth/logout")
        assert logout.status_code == 204
        assert first_client.get("/auth/me").status_code == 401
        login = first_client.post(
            "/auth/login",
            json={"username": "FIRST-USER", "password": "safe password"},
        )
        assert login.status_code == 200
        assert first_client.get("/auth/me").json()["id"] == first["id"]
    finally:
        app.dependency_overrides.clear()


def test_leaderboard_uses_server_records_and_hides_user_ids(tmp_path):
    app.dependency_overrides[get_settings] = lambda: Settings(
        workspace_dir=tmp_path
    )
    first_client = TestClient(app)
    second_client = TestClient(app)
    try:
        first = _register(first_client, "first-rank")
        second = _register(second_client, "second-rank")
        accounts = AccountStore(tmp_path / "history.sqlite3")
        accounts.record_score(first["id"], _score(82), source="history")
        accounts.record_score(first["id"], _score(93), source="standalone")
        accounts.record_score(second["id"], _score(88), source="history")
        assert first_client.get("/leaderboard").json()["entries"] == []
        assert first_client.patch(
            "/auth/me",
            json={"leaderboard_visible": True},
        ).json()["leaderboard_visible"] is True
        assert second_client.patch(
            "/auth/me",
            json={"leaderboard_visible": True},
        ).json()["leaderboard_visible"] is True

        response = first_client.get("/leaderboard")
        assert response.status_code == 200
        payload = response.json()
        assert payload["category"] == "entertainment"
        assert [entry["username"] for entry in payload["entries"]] == [
            "first-rank",
            "second-rank",
        ]
        assert [entry["total"] for entry in payload["entries"]] == [93, 88]
        assert payload["entries"][0]["attempts"] == 2
        assert payload["entries"][0]["is_current_user"] is True
        assert payload["entries"][1]["is_current_user"] is False
        assert all("user_id" not in entry for entry in payload["entries"])
    finally:
        app.dependency_overrides.clear()
