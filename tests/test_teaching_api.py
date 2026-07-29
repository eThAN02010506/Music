from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from music_insight.api.accounts import AccountStore
from music_insight.api.app import create_app
from music_insight.api.contracts.teaching import TeachingChatRequest
from music_insight.api.database import database_session
from music_insight.api.history import HistoryStore
from music_insight.api.dependencies import get_history_store
from music_insight.api.services.teaching import (
    TEACHING_SCHEMA_VERSION,
    answer_music_question,
    create_conversation,
    generate_teaching_guide,
    get_teaching_guide,
)
from music_insight.api.teaching import TeachingStore
from music_insight.config import get_settings
from music_insight.schemas import (
    AnalysisResult,
    DspResult,
    Evidence,
    EvidenceType,
    LyricsSegment,
    TimeSpan,
)
from music_insight.teaching.fallback import EvidenceTeachingModel


def _result() -> AnalysisResult:
    sound = Evidence(
        id="sound.1",
        source="model",
        kind=EvidenceType.OBSERVED,
        text="人声进入，钢琴和弦变密",
        confidence=0.85,
        span=TimeSpan(start_s=1, end_s=12),
    )
    energy = Evidence(
        id="energy.1",
        source="dsp",
        kind=EvidenceType.COMPUTED,
        text="能量逐步升高",
        confidence=0.9,
        span=TimeSpan(start_s=5, end_s=20),
    )
    return AnalysisResult(
        summary="歌曲从安静陈述逐步走向开放。",
        lyrics=[
            LyricsSegment(
                text="we begin again",
                span=TimeSpan(start_s=4, end_s=8),
                language="en",
                confidence=0.88,
            )
        ],
        instruments=["piano", "voice"],
        sound_events=[sound],
        emotion_timeline=[],
        inferred_atmosphere=[],
        themes=["重新开始"],
        technical_metrics=DspResult(bpm=76, energy_curve=[energy]),
        evidence=[sound, energy],
    )


def _stores(tmp_path):
    database = tmp_path / "history.sqlite3"
    accounts = AccountStore(database)
    user = accounts.register("teacher-user", "safe password")
    other = accounts.register("other-user", "safe password")
    history = HistoryStore(database)
    repository = TeachingStore(database)
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"placeholder")
    now = datetime.now(UTC)
    history.create(
        job_id="song-1",
        title="Song",
        file_name="song.wav",
        language="en",
        state="completed",
        created_at=now,
        updated_at=now,
        audio_path=audio,
        user_id=user.id,
    )
    history.update(
        "song-1",
        state="completed",
        updated_at=now,
        result=_result(),
        user_id=user.id,
    )
    return history, repository, user, other


class _BlockingRepository:
    def __init__(self, repository: TeachingStore, method_name: str) -> None:
        self.repository = repository
        self.method_name = method_name
        self.started = threading.Event()
        self.release = threading.Event()

    def __getattr__(self, name: str):
        target = getattr(self.repository, name)
        if name != self.method_name:
            return target

        def blocked(*args, **kwargs):
            self.started.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError(f"timed out waiting to release {name}")
            return target(*args, **kwargs)

        return blocked


def test_teaching_routes_are_registered_in_public_contract():
    paths = create_app().openapi()["paths"]

    assert "/listener-profile" in paths
    assert "/history/{history_id}/teaching-guide" in paths
    assert "/history/{history_id}/conversations" in paths
    assert (
        "/history/{history_id}/conversations/{conversation_id}/messages"
        in paths
    )


def test_lifespan_recovers_only_stale_teaching_reservations():
    class RecoveringRepository:
        def __init__(self) -> None:
            self.before = None

        def recover_pending(self, *, before):
            self.before = before
            return {"understanding_maps": 2, "music_messages": 3}

    repository = RecoveringRepository()
    application = create_app()
    application.state.teaching_repository = repository

    with TestClient(application) as client:
        assert client.get("/health").status_code == 200

    assert repository.before is not None
    assert repository.before.tzinfo is not None
    assert application.state.recovered_teaching == {
        "understanding_maps": 2,
        "music_messages": 3,
    }
    assert application.state.recovered_teaching_error is None


def test_authenticated_teaching_http_flow_uses_structured_message_shape(tmp_path):
    application = create_app()
    application.state.teaching_model = EvidenceTeachingModel()
    with TestClient(application) as client:
        registered = client.post(
            "/auth/register",
            json={"username": "http-teacher", "password": "safe password"},
        )
        assert registered.status_code == 201
        user_id = registered.json()["id"]
        history = get_history_store(get_settings())
        audio = tmp_path / "http-song.wav"
        audio.write_bytes(b"placeholder")
        now = datetime.now(UTC)
        history.create(
            job_id="http-song",
            title="HTTP Song",
            file_name="http-song.wav",
            language="en",
            state="completed",
            created_at=now,
            updated_at=now,
            audio_path=audio,
            user_id=user_id,
        )
        history.update(
            "http-song",
            state="completed",
            updated_at=now,
            result=_result(),
            user_id=user_id,
        )

        guide = client.post(
            "/history/http-song/teaching-guide",
            json={},
        )
        conversation = client.post(
            "/history/http-song/conversations",
            json={"title": "HTTP 导赏"},
        )
        message = client.post(
            (
                "/history/http-song/conversations/"
                f"{conversation.json()['id']}/messages"
            ),
            json={
                "client_request_id": "http-request-1",
                "message": "解释当前十五秒",
                "current_time_s": 10,
                "relisten_policy": "never",
            },
        )

    assert guide.status_code == 200
    assert guide.json()["understanding_map"]["events"]
    assert conversation.status_code == 201
    assert message.status_code == 200
    assert message.json()["status"] == "complete"
    assert {
        "answer",
        "time_ranges",
        "evidence",
        "listening_task",
        "suggested_questions",
        "player_actions",
        "confidence",
    } <= set(message.json()["response"])


def test_guide_and_song_chat_persist_and_replay_idempotently(tmp_path):
    history, repository, user, _ = _stores(tmp_path)

    async def exercise():
        generated = await generate_teaching_guide(
            history=history,
            repository=repository,
            history_id="song-1",
            user_id=user.id,
        )
        cached = await get_teaching_guide(
            history=history,
            repository=repository,
            history_id="song-1",
            user_id=user.id,
        )
        regenerated = await generate_teaching_guide(
            history=history,
            repository=repository,
            history_id="song-1",
            user_id=user.id,
            force=True,
        )
        conversation = await create_conversation(
            history=history,
            repository=repository,
            history_id="song-1",
            user_id=user.id,
            title="第一次导赏",
        )
        payload = TeachingChatRequest(
            client_request_id="request-0001",
            message="为什么这里听起来越来越有力量？",
            current_time_s=10,
            relisten_policy="never",
        )
        first = await answer_music_question(
            history=history,
            repository=repository,
            history_id="song-1",
            conversation_id=conversation.id,
            user_id=user.id,
            payload=payload,
        )
        replay = await answer_music_question(
            history=history,
            repository=repository,
            history_id="song-1",
            conversation_id=conversation.id,
            user_id=user.id,
            payload=payload,
        )
        return generated, cached, regenerated, first, replay

    generated, cached, regenerated, first, replay = asyncio.run(exercise())

    assert generated.understanding_map is not None
    assert generated.schema_version == 2
    assert generated.understanding_map.schema_version == 2
    assert generated.cached is False
    assert cached.cached is True
    assert regenerated.cached is False
    assert first.response is not None
    assert first.response.relistened is False
    assert replay.id == first.id
    assert replay.response == first.response
    assert len(
        repository.list_messages(
            first.conversation_id,
            analysis_id="song-1",
            user_id=user.id,
        )
    ) == 1


def test_song_chat_regenerates_a_guide_from_an_older_schema(tmp_path):
    history, repository, user, _ = _stores(tmp_path)

    async def prepare():
        await generate_teaching_guide(
            history=history,
            repository=repository,
            history_id="song-1",
            user_id=user.id,
        )
        return await create_conversation(
            history=history,
            repository=repository,
            history_id="song-1",
            user_id=user.id,
            title="schema refresh",
        )

    conversation = asyncio.run(prepare())
    with database_session(repository.database_path) as connection:
        connection.execute(
            """
            UPDATE understanding_maps
            SET schema_version = ?
            WHERE analysis_id = ?
            """,
            (TEACHING_SCHEMA_VERSION - 1, "song-1"),
        )

    message = asyncio.run(
        answer_music_question(
            history=history,
            repository=repository,
            history_id="song-1",
            conversation_id=conversation.id,
            user_id=user.id,
            payload=TeachingChatRequest(
                client_request_id="schema-refresh-1",
                message="解释当前段落",
                current_time_s=10,
                relisten_policy="never",
            ),
        )
    )
    refreshed = repository.get_understanding_map(
        "song-1",
        user_id=user.id,
    )

    assert message.status == "complete"
    assert refreshed["schema_version"] == TEACHING_SCHEMA_VERSION


def test_cancelled_guide_reservation_settles_then_compensates(tmp_path):
    history, repository, user, _ = _stores(tmp_path)
    blocking = _BlockingRepository(
        repository,
        "mark_understanding_map_pending",
    )

    async def exercise():
        task = asyncio.create_task(
            generate_teaching_guide(
                history=history,
                repository=blocking,
                history_id="song-1",
                user_id=user.id,
            )
        )
        started = await asyncio.to_thread(blocking.started.wait, 2)
        assert started is True
        task.cancel()
        await asyncio.sleep(0.05)
        assert task.done() is False
        blocking.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    record = repository.get_understanding_map("song-1", user_id=user.id)
    assert record["status"] == "failed"
    assert record["pending_source_result_hash"] is None


def test_cancelled_message_reservation_settles_then_compensates(tmp_path):
    history, repository, user, _ = _stores(tmp_path)

    async def prepare():
        await generate_teaching_guide(
            history=history,
            repository=repository,
            history_id="song-1",
            user_id=user.id,
        )
        return await create_conversation(
            history=history,
            repository=repository,
            history_id="song-1",
            user_id=user.id,
            title="cancel",
        )

    conversation = asyncio.run(prepare())
    blocking = _BlockingRepository(repository, "reserve_message")
    payload = TeachingChatRequest(
        client_request_id="cancel-message-1",
        message="解释当前段落",
        current_time_s=10,
        relisten_policy="never",
    )

    async def exercise():
        task = asyncio.create_task(
            answer_music_question(
                history=history,
                repository=blocking,
                history_id="song-1",
                conversation_id=conversation.id,
                user_id=user.id,
                payload=payload,
            )
        )
        started = await asyncio.to_thread(blocking.started.wait, 2)
        assert started is True
        task.cancel()
        await asyncio.sleep(0.05)
        assert task.done() is False
        blocking.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    messages = repository.list_messages(
        conversation.id,
        analysis_id="song-1",
        user_id=user.id,
    )
    assert len(messages) == 1
    assert messages[0]["status"] == "failed"
    assert messages[0]["error"] == "导赏问答请求已取消。"


def test_failed_message_retries_same_id_through_service(tmp_path):
    history, repository, user, _ = _stores(tmp_path)

    async def prepare():
        await generate_teaching_guide(
            history=history,
            repository=repository,
            history_id="song-1",
            user_id=user.id,
        )
        return await create_conversation(
            history=history,
            repository=repository,
            history_id="song-1",
            user_id=user.id,
            title="retry",
        )

    conversation = asyncio.run(prepare())
    payload = TeachingChatRequest(
        client_request_id="retry-service-1",
        message="再解释一次",
        current_time_s=10,
        relisten_policy="never",
    )
    reserved, _ = repository.reserve_message(
        conversation.id,
        analysis_id="song-1",
        user_id=user.id,
        client_request_id=payload.client_request_id,
        request_payload=payload.model_dump(mode="json"),
    )
    repository.fail_message(
        reserved["id"],
        user_id=user.id,
        error="first attempt failed",
        reservation_token=reserved["updated_at"],
    )

    retried = asyncio.run(
        answer_music_question(
            history=history,
            repository=repository,
            history_id="song-1",
            conversation_id=conversation.id,
            user_id=user.id,
            payload=payload,
        )
    )

    assert retried.id == reserved["id"]
    assert retried.sequence == reserved["sequence"]
    assert retried.status == "complete"
    assert retried.response is not None


def test_teaching_services_preserve_history_ownership(tmp_path):
    history, repository, _, other = _stores(tmp_path)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            get_teaching_guide(
                history=history,
                repository=repository,
                history_id="song-1",
                user_id=other.id,
            )
        )

    assert error.value.status_code == 404
