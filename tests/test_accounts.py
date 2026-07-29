import base64
from contextlib import closing
from datetime import UTC, datetime, timedelta
import hashlib
import sqlite3

import pytest

from music_insight.api.accounts import (
    AccountStore,
    AccountValidationError,
    UsernameAlreadyExistsError,
)
from music_insight.api.history import HistoryStore
from music_insight.singing_score import SingingScore


def _score(
    total: int,
    *,
    pitch: int | None = None,
    rhythm: int = 80,
    completeness: int = 90,
    stability: int = 85,
) -> SingingScore:
    return SingingScore(
        total=total,
        pitch=total if pitch is None else pitch,
        rhythm=rhythm,
        completeness=completeness,
        stability=stability,
        median_pitch_error=0.5,
        in_tune_ratio=0.75,
        reference_duration_s=30,
        performance_duration_s=29,
        pitch_curve=[],
        notes=["server-generated"],
    )


def test_registration_normalizes_username_and_uses_self_describing_scrypt(tmp_path):
    store = AccountStore(tmp_path / "accounts.sqlite3")

    user = store.register("  Ａlice  ", "correct horse")

    assert user.username == "Alice"
    assert store.count_users() == 1
    assert store.authenticate("ALICE", "correct horse") == user
    assert store.authenticate("alice", "wrong password") is None
    with pytest.raises(UsernameAlreadyExistsError):
        store.register("alice", "another password")

    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        password_hash, username_key = connection.execute(
            "SELECT password_hash, username_key FROM users"
        ).fetchone()
    assert username_key == "alice"
    assert password_hash.startswith("scrypt$n=32768,r=8,p=3,dklen=32$")
    assert "correct horse" not in password_hash


def test_successful_legacy_scrypt_login_transparently_rehashes(tmp_path):
    store = AccountStore(tmp_path / "accounts.sqlite3")
    user = store.register("legacy-user", "correct horse")
    salt = b"fixed-legacy-salt"
    digest = hashlib.scrypt(
        b"correct horse",
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    legacy_hash = "$".join(
        (
            "scrypt",
            "n=16384,r=8,p=1,dklen=32",
            base64.urlsafe_b64encode(salt).decode(),
            base64.urlsafe_b64encode(digest).decode(),
        )
    )
    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (legacy_hash, user.id),
        )

    assert store.authenticate("legacy-user", "correct horse") == user

    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        upgraded_hash = connection.execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (user.id,),
        ).fetchone()[0]
    assert upgraded_hash.startswith("scrypt$n=32768,r=8,p=3,dklen=32$")
    assert upgraded_hash != legacy_hash


def test_sessions_store_only_digest_expire_after_thirty_days_and_revoke(tmp_path):
    store = AccountStore(tmp_path / "accounts.sqlite3")
    now = datetime(2026, 7, 28, 9, tzinfo=UTC)
    user = store.register("session-user", "safe password", now=now)

    token = store.create_session(user.id, now=now)

    with closing(sqlite3.connect(store.database_path)) as connection, connection:
        token_hash, expires_at = connection.execute(
            "SELECT token_hash, expires_at FROM sessions"
        ).fetchone()
    assert bytes(token_hash) == hashlib.sha256(token.encode()).digest()
    assert token.encode() not in bytes(token_hash)
    assert datetime.fromisoformat(expires_at) == now + timedelta(days=30)
    assert store.user_for_token(token, now=now + timedelta(days=29)) == user
    assert store.user_for_token(token, now=now + timedelta(days=30)) is None

    replacement = store.create_session(user.id, now=now + timedelta(days=31))
    assert store.revoke_session(replacement) is True
    assert store.revoke_session(replacement) is False
    assert store.user_for_token(replacement, now=now + timedelta(days=31)) is None


def test_personal_attempts_are_isolated_and_standalone_scores_are_ranked(tmp_path):
    store = AccountStore(tmp_path / "accounts.sqlite3")
    first = store.register("第一位", "safe password")
    second = store.register("Second", "safe password")
    start = datetime(2026, 7, 1, tzinfo=UTC)
    audio = tmp_path / "original.wav"
    audio.write_bytes(b"history-source")
    HistoryStore(store.database_path).create(
        job_id="analysis-1",
        title="original",
        file_name=audio.name,
        language="zh",
        state="completed",
        created_at=start,
        updated_at=start,
        audio_path=audio,
        user_id=first.id,
    )

    first_old = store.record_score(
        first.id,
        _score(71),
        source="history",
        history_id="analysis-1",
        reference_name="original.wav",
        performance_name="first.wav",
        created_at=start,
    )
    first_best = store.record_score(
        first.id,
        _score(92, pitch=94),
        source="standalone",
        reference_name="reference.wav",
        performance_name="standalone.wav",
        created_at=start + timedelta(days=2),
    )
    second_best = store.record_score(
        second.id,
        _score(88, pitch=91),
        source="standalone",
        created_at=start + timedelta(days=1),
    )

    first_attempts = store.list_attempts(first.id)
    assert [attempt.id for attempt in first_attempts] == [
        first_best.id,
        first_old.id,
    ]
    assert all(attempt.user_id == first.id for attempt in first_attempts)
    assert [attempt.id for attempt in store.list_attempts(second.id)] == [
        second_best.id
    ]
    assert first_attempts[0].score.notes == ["server-generated"]

    board = store.leaderboard(now=start + timedelta(days=3))
    assert board.category == "entertainment"
    assert board.period == "all_time"
    assert [entry.username for entry in board.entries] == ["第一位", "Second"]
    assert [entry.total for entry in board.entries] == [92, 88]
    assert [entry.rank for entry in board.entries] == [1, 2]
    assert board.entries[0].source == "standalone"
    assert board.entries[0].attempts == 2
    assert board.entries[0].pitch == 94
    assert board.entries[0].achieved_at == start + timedelta(days=2)

    assert [
        attempt.id
        for attempt in store.list_attempts(first.id, limit=1, offset=1)
    ] == [first_old.id]
    assert store.delete_attempt(second.id, first_old.id) is False
    assert store.delete_attempt(first.id, first_old.id) is True
    assert store.delete_attempt(first.id, first_old.id) is False
    assert [attempt.id for attempt in store.list_attempts(first.id)] == [
        first_best.id
    ]
    assert store.leaderboard().entries[0].attempts == 1


def test_attempt_keyset_pagination_is_stable_across_ties_and_deletion(tmp_path):
    store = AccountStore(tmp_path / "accounts.sqlite3")
    user = store.register("cursor-user", "safe password")
    timestamp = datetime(2026, 7, 30, 9, tzinfo=UTC)
    recorded = [
        store.record_score(
            user.id,
            _score(70 + index),
            created_at=timestamp,
        )
        for index in range(12)
    ]
    expected_ids = sorted(
        (attempt.id for attempt in recorded),
        reverse=True,
    )

    first_with_lookahead = store.list_attempts(user.id, limit=11)
    first_page = first_with_lookahead[:10]
    cursor = first_page[-1]
    assert len(first_with_lookahead) == 11
    assert [attempt.id for attempt in first_page] == expected_ids[:10]

    # An offset query would skip one older row after this deletion. The keyset
    # remains anchored to the immutable (created_at, id) tuple instead.
    assert store.delete_attempt(user.id, first_page[0].id) is True
    store.record_score(
        user.id,
        _score(99),
        created_at=timestamp + timedelta(minutes=1),
    )
    second_page = store.list_attempts(
        user.id,
        limit=11,
        before_created_at=cursor.created_at,
        before_id=cursor.id,
    )
    assert [attempt.id for attempt in second_page] == expected_ids[10:]

    with pytest.raises(AccountValidationError):
        store.list_attempts(
            user.id,
            before_created_at=cursor.created_at,
        )
    with pytest.raises(AccountValidationError):
        store.list_attempts(
            user.id,
            offset=1,
            before_created_at=cursor.created_at,
            before_id=cursor.id,
        )


def test_deleting_scores_promotes_personal_best_and_rebuilds_ranks(tmp_path):
    store = AccountStore(tmp_path / "accounts.sqlite3")
    first = store.register("first-board", "safe password")
    second = store.register("second-board", "safe password")
    start = datetime(2026, 7, 30, 10, tzinfo=UTC)
    first_lower = store.record_score(
        first.id,
        _score(85),
        created_at=start,
    )
    first_best = store.record_score(
        first.id,
        _score(95),
        created_at=start + timedelta(minutes=1),
    )
    second_only = store.record_score(
        second.id,
        _score(90),
        created_at=start + timedelta(minutes=2),
    )

    assert [entry.total for entry in store.leaderboard().entries] == [95, 90]

    assert store.delete_attempt(first.id, first_best.id) is True
    promoted = store.leaderboard().entries
    assert [(entry.rank, entry.user_id, entry.total) for entry in promoted] == [
        (1, second.id, 90),
        (2, first.id, 85),
    ]
    assert promoted[1].attempts == 1

    assert store.delete_attempt(first.id, first_lower.id) is True
    remaining = store.leaderboard().entries
    assert [(entry.rank, entry.user_id) for entry in remaining] == [
        (1, second.id)
    ]

    assert store.delete_attempt(second.id, second_only.id) is True
    assert store.leaderboard().entries == []


def test_foreign_keys_and_input_validation_are_enabled(tmp_path):
    store = AccountStore(tmp_path / "accounts.sqlite3")

    with store._connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    with pytest.raises(AccountValidationError):
        store.register("x", "safe password")
    with pytest.raises(AccountValidationError):
        store.register("valid-name", "short")
    with pytest.raises(AccountValidationError):
        store.create_session("missing-user")
    with pytest.raises(AccountValidationError):
        store.record_score("missing-user", _score(80))
    with pytest.raises(AccountValidationError):
        store.leaderboard(period="weekly")  # type: ignore[arg-type]
