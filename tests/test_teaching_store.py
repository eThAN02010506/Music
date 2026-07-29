from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import sqlite3

import pytest

from music_insight.api.accounts import AccountStore
from music_insight.api.database import database_session
from music_insight.api.history import HistoryStore
from music_insight.api.migrations import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
)
from music_insight.api.teaching import (
    TeachingConflictError,
    TeachingEntryNotFoundError,
    TeachingStore,
)


def _legacy_v5_database(path: Path) -> None:
    with database_session(path) as connection:
        for version, migration in MIGRATIONS:
            if version > 5:
                break
            migration(connection)
            connection.execute(f"PRAGMA user_version = {version}")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _owned_analysis(tmp_path):
    path = tmp_path / "music.sqlite3"
    accounts = AccountStore(path)
    owner = accounts.register("owner", "safe password")
    other = accounts.register("other", "safe password")
    history = HistoryStore(path)
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"source")
    now = datetime.now(UTC)
    history.create(
        job_id="song-1",
        title="song",
        file_name=audio.name,
        language="zh",
        state="completed",
        created_at=now,
        updated_at=now,
        audio_path=audio,
        user_id=owner.id,
    )
    with database_session(path) as connection:
        connection.execute(
            """
            UPDATE analyses
            SET result_json = ?, updated_at = ?
            WHERE id = ?
            """,
            ('{"summary":"first"}', now.isoformat(), "song-1"),
        )
    return path, owner, other


def test_v5_upgrade_adds_teaching_schema_without_losing_history(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    _legacy_v5_database(path)
    with database_session(path) as connection:
        connection.execute(
            """
            INSERT INTO users (
                id, username, username_key, password_hash, created_at
            ) VALUES ('user-1', 'user', 'user', 'hash', '2026-01-01')
            """
        )
        connection.execute(
            """
            INSERT INTO analyses (
                id, user_id, title, file_name, state, created_at,
                updated_at, audio_path
            ) VALUES (
                'song-1', 'user-1', 'song', 'song.wav', 'completed',
                '2026-01-01', '2026-01-01', '/tmp/song.wav'
            )
            """
        )

    TeachingStore(path)

    with database_session(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        analysis = connection.execute(
            "SELECT title FROM analyses WHERE id = 'song-1'"
        ).fetchone()
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(music_messages)"
        ).fetchall()
    assert version == LATEST_SCHEMA_VERSION == 6
    assert {
        "understanding_maps",
        "listener_profiles",
        "music_conversations",
        "music_messages",
    } <= tables
    assert analysis["title"] == "song"
    assert any(
        row["table"] == "music_conversations"
        and row["on_delete"] == "CASCADE"
        for row in foreign_keys
    )


def test_understanding_map_lifecycle_hash_invalidation_and_failed_refresh(
    tmp_path,
):
    path, owner, other = _owned_analysis(tmp_path)
    store = TeachingStore(path)
    first_hash = _digest("first")
    second_hash = _digest("second")
    first_map = {"core_expression": "quiet", "events": []}

    pending, should_generate = store.mark_understanding_map_pending(
        "song-1",
        user_id=owner.id,
        schema_version=1,
        source_result_hash=first_hash,
    )
    duplicate, duplicate_should_generate = (
        store.mark_understanding_map_pending(
            "song-1",
            user_id=owner.id,
            schema_version=1,
            source_result_hash=first_hash,
        )
    )
    assert should_generate is True
    assert duplicate_should_generate is False
    assert pending["status"] == duplicate["status"] == "pending"

    complete = store.upsert_understanding_map(
        "song-1",
        user_id=owner.id,
        schema_version=1,
        source_result_hash=first_hash,
        map_payload=first_map,
    )
    assert complete["status"] == "complete"
    assert complete["map_payload"] == first_map
    assert (
        store.get_understanding_map("song-1", user_id=other.id)
        is None
    )
    cached, cached_should_generate = store.mark_understanding_map_pending(
        "song-1",
        user_id=owner.id,
        schema_version=1,
        source_result_hash=first_hash,
    )
    forced, forced_should_generate = store.mark_understanding_map_pending(
        "song-1",
        user_id=owner.id,
        schema_version=1,
        source_result_hash=first_hash,
        force=True,
    )
    duplicate_force, duplicate_force_should_generate = (
        store.mark_understanding_map_pending(
            "song-1",
            user_id=owner.id,
            schema_version=1,
            source_result_hash=first_hash,
            force=True,
        )
    )
    assert cached_should_generate is False
    assert cached["status"] == "complete"
    assert forced_should_generate is True
    assert forced["status"] == "pending"
    assert forced["map_payload"] == first_map
    assert duplicate_force_should_generate is False
    assert duplicate_force["status"] == "pending"
    restored = store.fail_understanding_map(
        "song-1",
        user_id=owner.id,
        schema_version=1,
        source_result_hash=first_hash,
        error="forced refresh failed",
    )
    assert restored["status"] == "complete"
    assert restored["map_payload"] == first_map

    with database_session(path) as connection:
        connection.execute(
            """
            UPDATE analyses
            SET result_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                '{"summary":"second"}',
                datetime.now(UTC).isoformat(),
                "song-1",
            ),
        )
    stale = store.get_understanding_map(
        "song-1",
        user_id=owner.id,
        source_result_hash=second_hash,
    )
    assert stale["status"] == "stale"
    assert stale["map_payload"] == first_map
    with pytest.raises(TeachingConflictError, match="未预留"):
        store.upsert_understanding_map(
            "song-1",
            user_id=owner.id,
            schema_version=2,
            source_result_hash=second_hash,
            map_payload={"events": ["unreserved"]},
        )

    _, should_refresh = store.mark_understanding_map_pending(
        "song-1",
        user_id=owner.id,
        schema_version=2,
        source_result_hash=second_hash,
    )
    failed = store.fail_understanding_map(
        "song-1",
        user_id=owner.id,
        schema_version=2,
        source_result_hash=second_hash,
        error="model offline",
    )
    assert should_refresh is True
    assert failed["status"] == "stale"
    assert failed["schema_version"] == 1
    assert failed["source_result_hash"] == first_hash
    assert failed["map_payload"] == first_map
    assert failed["last_error"] == "model offline"


def test_obsolete_map_completion_cannot_overwrite_newer_reservation(tmp_path):
    path, owner, _ = _owned_analysis(tmp_path)
    store = TeachingStore(path)
    old_hash = _digest("version-1")
    new_hash = _digest("version-2")
    store.mark_understanding_map_pending(
        "song-1",
        user_id=owner.id,
        schema_version=1,
        source_result_hash=old_hash,
    )
    store.mark_understanding_map_pending(
        "song-1",
        user_id=owner.id,
        schema_version=2,
        source_result_hash=new_hash,
    )

    with pytest.raises(TeachingConflictError, match="已过期"):
        store.upsert_understanding_map(
            "song-1",
            user_id=owner.id,
            schema_version=1,
            source_result_hash=old_hash,
            map_payload={"events": ["obsolete"]},
        )

    current = store.get_understanding_map(
        "song-1",
        user_id=owner.id,
    )
    assert current["status"] == "pending"
    assert current["pending_source_result_hash"] == new_hash
    assert current["map_payload"] is None


def test_late_failure_cannot_downgrade_a_newer_complete_map(tmp_path):
    path, owner, _ = _owned_analysis(tmp_path)
    store = TeachingStore(path)
    old_hash = _digest("version-1")
    new_hash = _digest("version-2")
    store.upsert_understanding_map(
        "song-1",
        user_id=owner.id,
        schema_version=1,
        source_result_hash=new_hash,
        map_payload={"events": ["new"]},
    )

    result = store.fail_understanding_map(
        "song-1",
        user_id=owner.id,
        schema_version=1,
        source_result_hash=old_hash,
        error="late failure",
    )

    assert result["status"] == "complete"
    assert result["source_result_hash"] == new_hash
    assert result["map_payload"] == {"events": ["new"]}
    assert result["last_error"] is None


def test_listener_profile_upsert_is_owner_scoped_and_json_safe(tmp_path):
    path, owner, other = _owned_analysis(tmp_path)
    store = TeachingStore(path)

    created = store.upsert_listener_profile(
        user_id=owner.id,
        level="curious",
        preferences={"language": "zh", "detail": 2},
        learned_concepts=["音色", " 音色 ", "切分"],
    )
    updated = store.update_listener_profile(
        user_id=owner.id,
        level="intermediate",
        preferences={"language": "en"},
        learned_concepts=["和声"],
    )

    assert created["learned_concepts"] == ["音色", "切分"]
    assert updated["level"] == "intermediate"
    assert updated["preferences"] == {"language": "en"}
    assert store.get_listener_profile(user_id=other.id) is None
    with pytest.raises(TeachingEntryNotFoundError, match="用户不存在"):
        store.upsert_listener_profile(
            user_id="missing-user",
            level="beginner",
        )
    with pytest.raises(ValueError, match="valid JSON"):
        store.upsert_listener_profile(
            user_id=owner.id,
            level="beginner",
            preferences={"bad": float("nan")},
        )


def test_conversation_messages_are_idempotent_ordered_and_owner_scoped(
    tmp_path,
):
    path, owner, other = _owned_analysis(tmp_path)
    store = TeachingStore(path)
    conversation = store.create_conversation(
        "song-1",
        user_id=owner.id,
        title="第一轮复听",
    )
    with pytest.raises(TeachingEntryNotFoundError):
        store.create_conversation(
            "song-1",
            user_id=other.id,
        )

    first, first_reserved = store.reserve_message(
        conversation["id"],
        analysis_id="song-1",
        user_id=owner.id,
        client_request_id="client-1",
        request_payload={"question": "这里为什么紧张？"},
    )
    duplicate, duplicate_reserved = store.reserve_message(
        conversation["id"],
        analysis_id="song-1",
        user_id=owner.id,
        client_request_id="client-1",
        request_payload={"question": "这里为什么紧张？"},
    )
    assert first_reserved is True
    assert duplicate_reserved is False
    assert duplicate["id"] == first["id"]
    assert first["sequence"] == 1
    with pytest.raises(TeachingConflictError, match="不同问题"):
        store.reserve_message(
            conversation["id"],
            analysis_id="song-1",
            user_id=owner.id,
            client_request_id="client-1",
            request_payload={"question": "换了一个问题"},
        )

    completed = store.complete_message(
        first["id"],
        user_id=owner.id,
        response_payload={"answer": "因为节奏密度增加。"},
    )
    assert completed["status"] == "complete"
    assert completed["response_payload"]["answer"].startswith("因为")
    assert (
        store.complete_message(
            first["id"],
            user_id=other.id,
            response_payload={"answer": "cross owner"},
        )
        is None
    )

    second, _ = store.reserve_message(
        conversation["id"],
        analysis_id="song-1",
        user_id=owner.id,
        client_request_id="client-2",
        request_payload={"question": "再听一次"},
    )
    failed = store.fail_message(
        second["id"],
        user_id=owner.id,
        error="model unavailable",
    )
    messages = store.list_messages(
        conversation["id"],
        analysis_id="song-1",
        user_id=owner.id,
    )
    assert failed["status"] == "failed"
    assert [message["sequence"] for message in messages] == [1, 2]
    assert store.get_conversation(
        conversation["id"],
        analysis_id="song-1",
        user_id=other.id,
    ) is None


def test_pending_recovery_preserves_old_map_and_fails_abandoned_message(
    tmp_path,
):
    path, owner, _ = _owned_analysis(tmp_path)
    store = TeachingStore(path)
    old_hash = _digest("result-old")
    new_hash = _digest("result-new")
    old_map = {"events": [{"start_s": 0, "end_s": 10}]}
    store.upsert_understanding_map(
        "song-1",
        user_id=owner.id,
        schema_version=1,
        source_result_hash=old_hash,
        map_payload=old_map,
    )
    store.mark_understanding_map_pending(
        "song-1",
        user_id=owner.id,
        schema_version=2,
        source_result_hash=new_hash,
    )
    conversation = store.create_conversation(
        "song-1",
        user_id=owner.id,
    )
    message, _ = store.reserve_message(
        conversation["id"],
        analysis_id="song-1",
        user_id=owner.id,
        client_request_id="abandoned",
        request_payload={"question": "还在吗？"},
    )

    counts = store.recover_pending(
        before=datetime.now(UTC) + timedelta(seconds=1)
    )

    recovered_map = store.get_understanding_map(
        "song-1",
        user_id=owner.id,
    )
    recovered_message = store.list_messages(
        conversation["id"],
        analysis_id="song-1",
        user_id=owner.id,
    )[0]
    assert counts == {
        "understanding_maps": 1,
        "music_messages": 1,
    }
    assert recovered_map["status"] == "stale"
    assert recovered_map["map_payload"] == old_map
    assert recovered_message["id"] == message["id"]
    assert recovered_message["status"] == "failed"
    assert "服务重启" in recovered_message["error"]


def test_expired_map_lease_is_reclaimed_once_and_old_owner_cannot_publish(
    tmp_path,
):
    path, owner, _ = _owned_analysis(tmp_path)
    store = TeachingStore(path)
    digest = _digest("same-result")
    old_now = datetime.now(UTC) - timedelta(hours=1)
    cutoff = datetime.now(UTC) - timedelta(minutes=30)
    reclaim_now = datetime.now(UTC)

    first, first_reserved = store.mark_understanding_map_pending(
        "song-1",
        user_id=owner.id,
        schema_version=2,
        source_result_hash=digest,
        force=True,
        stale_before=cutoff,
        now=old_now,
    )
    reclaimed, reclaimed_reserved = store.mark_understanding_map_pending(
        "song-1",
        user_id=owner.id,
        schema_version=2,
        source_result_hash=digest,
        force=True,
        stale_before=cutoff,
        now=reclaim_now,
    )
    duplicate, duplicate_reserved = store.mark_understanding_map_pending(
        "song-1",
        user_id=owner.id,
        schema_version=2,
        source_result_hash=digest,
        force=True,
        stale_before=cutoff,
        now=reclaim_now + timedelta(seconds=1),
    )

    assert first_reserved is True
    assert reclaimed_reserved is True
    assert duplicate_reserved is False
    assert first["updated_at"] != reclaimed["updated_at"]
    assert duplicate["updated_at"] == reclaimed["updated_at"]
    with pytest.raises(TeachingConflictError, match="lease"):
        store.upsert_understanding_map(
            "song-1",
            user_id=owner.id,
            schema_version=2,
            source_result_hash=digest,
            map_payload={"events": ["old-worker"]},
            reservation_token=first["updated_at"],
        )

    complete = store.upsert_understanding_map(
        "song-1",
        user_id=owner.id,
        schema_version=2,
        source_result_hash=digest,
        map_payload={"events": ["new-worker"]},
        reservation_token=reclaimed["updated_at"],
    )
    assert complete["status"] == "complete"
    assert complete["map_payload"] == {"events": ["new-worker"]}
    late_failure = store.fail_understanding_map(
        "song-1",
        user_id=owner.id,
        schema_version=2,
        source_result_hash=digest,
        reservation_token=first["updated_at"],
        error="old worker cancelled",
    )
    assert late_failure["status"] == "complete"
    assert late_failure["last_error"] is None


def test_expired_message_lease_is_reclaimed_idempotently(tmp_path):
    path, owner, _ = _owned_analysis(tmp_path)
    store = TeachingStore(path)
    conversation = store.create_conversation("song-1", user_id=owner.id)
    old_now = datetime.now(UTC) - timedelta(hours=1)
    cutoff = datetime.now(UTC) - timedelta(minutes=30)
    reclaim_now = datetime.now(UTC)
    options = {
        "analysis_id": "song-1",
        "user_id": owner.id,
        "client_request_id": "lease-message",
        "request_payload": {"question": "这里是什么声音？"},
    }

    first, first_reserved = store.reserve_message(
        conversation["id"],
        **options,
        stale_before=cutoff,
        now=old_now,
    )
    reclaimed, reclaimed_reserved = store.reserve_message(
        conversation["id"],
        **options,
        stale_before=cutoff,
        now=reclaim_now,
    )
    duplicate, duplicate_reserved = store.reserve_message(
        conversation["id"],
        **options,
        stale_before=cutoff,
        now=reclaim_now + timedelta(seconds=1),
    )

    assert first_reserved is True
    assert reclaimed_reserved is True
    assert duplicate_reserved is False
    assert first["id"] == reclaimed["id"] == duplicate["id"]
    with pytest.raises(TeachingConflictError, match="lease"):
        store.complete_message(
            first["id"],
            user_id=owner.id,
            response_payload={"answer": "old"},
            reservation_token=first["updated_at"],
        )
    completed = store.complete_message(
        reclaimed["id"],
        user_id=owner.id,
        response_payload={"answer": "new"},
        reservation_token=reclaimed["updated_at"],
    )
    assert completed["status"] == "complete"
    assert completed["response_payload"] == {"answer": "new"}


def test_failed_message_retries_same_payload_with_same_id_and_new_lease(
    tmp_path,
):
    path, owner, _ = _owned_analysis(tmp_path)
    store = TeachingStore(path)
    conversation = store.create_conversation("song-1", user_id=owner.id)
    options = {
        "analysis_id": "song-1",
        "user_id": owner.id,
        "client_request_id": "retry-message",
        "request_payload": {"question": "请再解释一次"},
    }
    first, first_reserved = store.reserve_message(conversation["id"], **options)
    failed = store.fail_message(
        first["id"],
        user_id=owner.id,
        error="provider unavailable",
        reservation_token=first["updated_at"],
    )

    retried, retry_reserved = store.reserve_message(
        conversation["id"],
        **options,
    )

    assert first_reserved is True
    assert failed["status"] == "failed"
    assert retry_reserved is True
    assert retried["id"] == first["id"]
    assert retried["sequence"] == first["sequence"]
    assert retried["status"] == "pending"
    assert retried["error"] is None
    assert retried["updated_at"] != first["updated_at"]


def test_analysis_and_conversation_deletes_cascade_teaching_children(
    tmp_path,
):
    path, owner, _ = _owned_analysis(tmp_path)
    store = TeachingStore(path)
    digest = _digest("summary-first")
    store.upsert_understanding_map(
        "song-1",
        user_id=owner.id,
        schema_version=1,
        source_result_hash=digest,
        map_payload={"events": []},
    )
    conversation = store.create_conversation(
        "song-1",
        user_id=owner.id,
    )
    store.reserve_message(
        conversation["id"],
        analysis_id="song-1",
        user_id=owner.id,
        client_request_id="cascade",
        request_payload={"question": "test"},
    )

    with database_session(path) as connection:
        connection.execute("DELETE FROM analyses WHERE id = ?", ("song-1",))
        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "understanding_maps",
                "music_conversations",
                "music_messages",
            )
        }
    assert counts == {
        "understanding_maps": 0,
        "music_conversations": 0,
        "music_messages": 0,
    }


def test_parallel_message_reservations_get_unique_monotonic_sequences(
    tmp_path,
):
    path, owner, _ = _owned_analysis(tmp_path)
    store = TeachingStore(path)
    conversation = store.create_conversation(
        "song-1",
        user_id=owner.id,
    )

    def reserve(index: int):
        return store.reserve_message(
            conversation["id"],
            analysis_id="song-1",
            user_id=owner.id,
            client_request_id=f"parallel-{index}",
            request_payload={"question": str(index)},
        )[0]

    with ThreadPoolExecutor(max_workers=8) as executor:
        messages = list(executor.map(reserve, range(16)))

    assert sorted(message["sequence"] for message in messages) == list(
        range(1, 17)
    )


def test_database_trigger_rejects_cross_owner_conversation(tmp_path):
    path, owner, other = _owned_analysis(tmp_path)
    with database_session(path) as connection:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="music conversation owner mismatch",
        ):
            connection.execute(
                """
                INSERT INTO music_conversations (
                    id, analysis_id, user_id, title, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "cross-owner",
                    "song-1",
                    other.id,
                    "invalid",
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
        assert connection.execute(
            """
            SELECT COUNT(*)
            FROM music_conversations
            WHERE analysis_id = ? AND user_id = ?
            """,
            ("song-1", other.id),
        ).fetchone()[0] == 0
