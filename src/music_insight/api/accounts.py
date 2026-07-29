from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
from pathlib import Path
import secrets
import sqlite3
from typing import ContextManager, Literal
import unicodedata
from uuid import uuid4

from pydantic import BaseModel, Field

from music_insight.api.database import database_session, migrate_database
from music_insight.singing_score import SingingScore


SESSION_TTL = timedelta(days=30)
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 3
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_SCRYPT_MAX_N = 2**18
_SCRYPT_MAX_R = 16
_SCRYPT_MAX_P = 8
_SESSION_TOKEN_BYTES = 32

SingingSource = Literal["history", "standalone"]
LeaderboardPeriod = Literal["all_time"]


class AccountValidationError(ValueError):
    """Raised when account input does not satisfy the local account policy."""


class UsernameAlreadyExistsError(AccountValidationError):
    """Raised when a normalized username is already registered."""


class UserPublic(BaseModel):
    id: str
    username: str
    created_at: datetime


class AuthCredentials(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=8, max_length=128)


class SingingAttempt(BaseModel):
    id: str
    user_id: str
    source: SingingSource
    category: str
    history_id: str | None = None
    reference_name: str | None = None
    performance_name: str | None = None
    created_at: datetime
    score: SingingScore


class LeaderboardEntry(BaseModel):
    rank: int = Field(ge=1)
    user_id: str
    username: str
    total: int = Field(ge=0, le=100)
    pitch: int = Field(ge=0, le=100)
    rhythm: int = Field(ge=0, le=100)
    completeness: int = Field(ge=0, le=100)
    stability: int = Field(ge=0, le=100)
    achieved_at: datetime
    source: SingingSource
    attempts: int = Field(ge=1)


class Leaderboard(BaseModel):
    category: str
    period: LeaderboardPeriod
    generated_at: datetime
    entries: list[LeaderboardEntry]


class AccountStore:
    """SQLite-backed local accounts, sessions, and singing score records."""

    def __init__(
        self,
        database_path: Path,
        *,
        session_ttl: timedelta = SESSION_TTL,
    ) -> None:
        if session_ttl <= timedelta(0):
            raise ValueError("session_ttl must be positive")
        self.database_path = database_path
        self.session_ttl = session_ttl
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._dummy_password_hash = _DUMMY_PASSWORD_HASH
        migrate_database(database_path)

    def _connect(self) -> ContextManager[sqlite3.Connection]:
        return database_session(self.database_path)

    def register(
        self,
        username: str,
        password: str,
        *,
        now: datetime | None = None,
    ) -> UserPublic:
        display_name, username_key = normalize_username(username)
        _validate_password(password)
        created_at = _utc_now(now)
        user_id = uuid4().hex
        password_hash = _hash_password(password)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO users (
                        id, username, username_key, password_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        display_name,
                        username_key,
                        password_hash,
                        created_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "username_key" in str(exc).lower():
                raise UsernameAlreadyExistsError("用户名已被使用。") from exc
            raise
        return UserPublic(
            id=user_id,
            username=display_name,
            created_at=created_at,
        )

    def authenticate(self, username: str, password: str) -> UserPublic | None:
        try:
            _, username_key = normalize_username(username)
        except AccountValidationError:
            _verify_password(password, self._dummy_password_hash)
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username_key = ?",
                (username_key,),
            ).fetchone()
        stored_hash = row["password_hash"] if row is not None else self._dummy_password_hash
        valid = _verify_password(password, stored_hash)
        if row is None or not valid:
            return None
        if _password_needs_rehash(stored_hash):
            upgraded_hash = _hash_password(password)
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE users
                    SET password_hash = ?
                    WHERE id = ? AND password_hash = ?
                    """,
                    (upgraded_hash, row["id"], stored_hash),
                )
        return self._user(row)

    def create_session(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> str:
        created_at = _utc_now(now)
        expires_at = created_at + self.session_ttl
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if exists is None:
                raise AccountValidationError("用户不存在。")
            connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ?",
                (created_at.isoformat(),),
            )
            for _ in range(3):
                token = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
                try:
                    connection.execute(
                        """
                        INSERT INTO sessions (
                            token_hash, user_id, created_at, expires_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            _token_digest(token),
                            user_id,
                            created_at.isoformat(),
                            expires_at.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                return token
        raise RuntimeError("无法生成唯一会话令牌。")

    def user_for_token(
        self,
        token: str,
        *,
        now: datetime | None = None,
    ) -> UserPublic | None:
        if not token:
            return None
        checked_at = _utc_now(now)
        token_hash = _token_digest(token)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    users.id, users.username, users.created_at,
                    sessions.expires_at
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if datetime.fromisoformat(row["expires_at"]) <= checked_at:
                connection.execute(
                    "DELETE FROM sessions WHERE token_hash = ?",
                    (token_hash,),
                )
                return None
        return self._user(row)

    def revoke_session(self, token: str) -> bool:
        if not token:
            return False
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?",
                (_token_digest(token),),
            )
        return cursor.rowcount > 0

    def record_score(
        self,
        user_id: str,
        score: SingingScore,
        *,
        source: SingingSource = "standalone",
        category: str = "entertainment",
        history_id: str | None = None,
        reference_name: str | None = None,
        performance_name: str | None = None,
        created_at: datetime | None = None,
    ) -> SingingAttempt:
        if source not in {"history", "standalone"}:
            raise AccountValidationError("未知的演唱评分来源。")
        cleaned_category = _clean_category(category)
        validated_score = SingingScore.model_validate(score)
        attempt_id = uuid4().hex
        timestamp = _utc_now(created_at)
        clean_history_id = _clean_optional(history_id, max_length=120)
        clean_reference = _clean_optional(reference_name, max_length=255)
        clean_performance = _clean_optional(performance_name, max_length=255)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO singing_attempts (
                        id, user_id, source, category, history_id,
                        reference_name, performance_name, created_at,
                        total, pitch, rhythm, completeness, stability,
                        score_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        user_id,
                        source,
                        cleaned_category,
                        clean_history_id,
                        clean_reference,
                        clean_performance,
                        timestamp.isoformat(),
                        validated_score.total,
                        validated_score.pitch,
                        validated_score.rhythm,
                        validated_score.completeness,
                        validated_score.stability,
                        validated_score.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            detail = str(exc).lower()
            if "singing history owner mismatch" in detail:
                raise AccountValidationError(
                    "参考历史不存在或不属于当前用户。"
                ) from exc
            if "foreign key" in detail:
                raise AccountValidationError("用户不存在。") from exc
            raise
        return SingingAttempt(
            id=attempt_id,
            user_id=user_id,
            source=source,
            category=cleaned_category,
            history_id=clean_history_id,
            reference_name=clean_reference,
            performance_name=clean_performance,
            created_at=timestamp,
            score=validated_score,
        )

    def list_attempts(
        self,
        user_id: str,
        *,
        limit: int = 50,
    ) -> list[SingingAttempt]:
        bounded_limit = max(1, min(limit, 500))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM singing_attempts
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, bounded_limit),
            ).fetchall()
        return [self._attempt(row) for row in rows]

    def leaderboard(
        self,
        *,
        limit: int = 100,
        category: str = "entertainment",
        period: LeaderboardPeriod = "all_time",
        now: datetime | None = None,
    ) -> Leaderboard:
        if period != "all_time":
            raise AccountValidationError("当前仅支持 all_time 排行榜。")
        cleaned_category = _clean_category(category)
        bounded_limit = max(1, min(limit, 500))
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT
                        singing_attempts.*,
                        COUNT(*) OVER (
                            PARTITION BY singing_attempts.user_id
                        ) AS attempt_count,
                        ROW_NUMBER() OVER (
                            PARTITION BY singing_attempts.user_id
                            ORDER BY
                                total DESC,
                                pitch DESC,
                                rhythm DESC,
                                stability DESC,
                                completeness DESC,
                                created_at ASC,
                                id ASC
                        ) AS user_rank
                    FROM singing_attempts
                    WHERE category = ?
                )
                SELECT
                    ranked.*,
                    users.username,
                    users.username_key
                FROM ranked
                JOIN users ON users.id = ranked.user_id
                WHERE ranked.user_rank = 1
                ORDER BY
                    ranked.total DESC,
                    ranked.pitch DESC,
                    ranked.rhythm DESC,
                    ranked.stability DESC,
                    ranked.completeness DESC,
                    ranked.created_at ASC,
                    users.username_key ASC
                LIMIT ?
                """,
                (cleaned_category, bounded_limit),
            ).fetchall()
        entries = [
            LeaderboardEntry(
                rank=index,
                user_id=row["user_id"],
                username=row["username"],
                total=row["total"],
                pitch=row["pitch"],
                rhythm=row["rhythm"],
                completeness=row["completeness"],
                stability=row["stability"],
                achieved_at=datetime.fromisoformat(row["created_at"]),
                source=row["source"],
                attempts=row["attempt_count"],
            )
            for index, row in enumerate(rows, start=1)
        ]
        return Leaderboard(
            category=cleaned_category,
            period=period,
            generated_at=_utc_now(now),
            entries=entries,
        )

    def count_users(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    @staticmethod
    def _user(row: sqlite3.Row) -> UserPublic:
        return UserPublic(
            id=row["id"],
            username=row["username"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _attempt(row: sqlite3.Row) -> SingingAttempt:
        return SingingAttempt(
            id=row["id"],
            user_id=row["user_id"],
            source=row["source"],
            category=row["category"],
            history_id=row["history_id"],
            reference_name=row["reference_name"],
            performance_name=row["performance_name"],
            created_at=datetime.fromisoformat(row["created_at"]),
            score=SingingScore.model_validate_json(row["score_json"]),
        )


def normalize_username(username: str) -> tuple[str, str]:
    if not isinstance(username, str):
        raise AccountValidationError("用户名必须是文本。")
    normalized = unicodedata.normalize("NFKC", username)
    display_name = " ".join(normalized.split())
    if not 2 <= len(display_name) <= 40:
        raise AccountValidationError("用户名长度需为 2 到 40 个字符。")
    if any(unicodedata.category(character).startswith("C") for character in display_name):
        raise AccountValidationError("用户名包含不支持的控制字符。")
    return display_name, display_name.casefold()


# Kept temporarily for callers from builds predating the public helper.
_normalize_username = normalize_username


def _validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise AccountValidationError("密码必须是文本。")
    if not 8 <= len(password) <= 128:
        raise AccountValidationError("密码长度需为 8 到 128 个字符。")


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    parameters = (
        f"n={_SCRYPT_N},r={_SCRYPT_R},p={_SCRYPT_P},dklen={_SCRYPT_DKLEN}"
    )
    return "$".join(
        (
            "scrypt",
            parameters,
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(derived).decode("ascii"),
        )
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        parameters, salt, expected = _parse_password_hash(encoded)
        if (
            parameters["n"] <= 1
            or parameters["n"] & (parameters["n"] - 1)
            or parameters["n"] > _SCRYPT_MAX_N
            or parameters["r"] <= 0
            or parameters["r"] > _SCRYPT_MAX_R
            or parameters["p"] <= 0
            or parameters["p"] > _SCRYPT_MAX_P
            or not 16 <= parameters["dklen"] <= 128
            or len(expected) != parameters["dklen"]
        ):
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=parameters["n"],
            r=parameters["r"],
            p=parameters["p"],
            dklen=parameters["dklen"],
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError, UnicodeError):
        return False
    return hmac.compare_digest(actual, expected)


def _parse_password_hash(
    encoded: str,
) -> tuple[dict[str, int], bytes, bytes]:
    algorithm, parameter_text, salt_text, expected_text = encoded.split("$", 3)
    if algorithm != "scrypt":
        raise ValueError("unsupported password hash")
    parameters = {
        key: int(value)
        for item in parameter_text.split(",")
        for key, value in (item.split("=", 1),)
    }
    if set(parameters) != {"n", "r", "p", "dklen"}:
        raise ValueError("invalid password hash parameters")
    salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
    expected = base64.urlsafe_b64decode(expected_text.encode("ascii"))
    if len(salt) < 16:
        raise ValueError("password hash salt is too short")
    return parameters, salt, expected


def _password_needs_rehash(encoded: str) -> bool:
    try:
        parameters, _, _ = _parse_password_hash(encoded)
    except (ValueError, TypeError, UnicodeError):
        return False
    return parameters != {
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
        "dklen": _SCRYPT_DKLEN,
    }


def _token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _utc_now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clean_optional(value: str | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split()).strip()
    return cleaned[:max_length] or None


def _clean_category(category: str) -> str:
    cleaned = unicodedata.normalize("NFKC", str(category)).strip().casefold()
    if not cleaned or len(cleaned) > 40:
        raise AccountValidationError("排行榜分类无效。")
    return cleaned


# One process-wide dummy hash equalizes unknown-user authentication without
# repeating a memory-hard KDF in every AccountStore constructor.
_DUMMY_PASSWORD_HASH = _hash_password(secrets.token_urlsafe(24))
