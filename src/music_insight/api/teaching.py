from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, ContextManager
from uuid import uuid4

from music_insight.api.database import database_session, migrate_database


_MAP_STATUSES = {"pending", "complete", "stale", "failed"}
_PROFILE_LEVELS = {"beginner", "curious", "intermediate", "advanced"}
_INTERRUPTED_ERROR = "服务重启，未完成的音乐导赏请求已中断。"


class TeachingStoreError(RuntimeError):
    """Base error for teaching persistence failures."""


class TeachingEntryNotFoundError(TeachingStoreError, LookupError):
    """Raised when an owner-scoped teaching resource cannot be found."""


class TeachingConflictError(TeachingStoreError):
    """Raised when an idempotency key or generation lease conflicts."""


class TeachingDataError(TeachingStoreError):
    """Raised when persisted teaching JSON cannot be decoded safely."""


class TeachingStore:
    """Short-transaction SQLite persistence for maps and teaching dialogue.

    Callers reserve a pending map/message, close the transaction, invoke the
    model, and then complete or fail the reservation in a separate transaction.
    No method accepts a model callback, which prevents a network call from
    accidentally holding SQLite's single writer lock.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        migrate_database(database_path)

    def _connect(self) -> ContextManager[sqlite3.Connection]:
        return database_session(self.database_path)

    def get_understanding_map(
        self,
        analysis_id: str,
        *,
        user_id: str,
        source_result_hash: str | None = None,
    ) -> dict[str, Any] | None:
        owner = _require_text(user_id, "user_id")
        analysis = _require_text(analysis_id, "analysis_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT understanding_maps.*
                FROM understanding_maps
                JOIN analyses
                  ON analyses.id = understanding_maps.analysis_id
                WHERE understanding_maps.analysis_id = ?
                  AND analyses.user_id = ?
                """,
                (analysis, owner),
            ).fetchone()
        if row is None:
            return None
        record = _map_record(row)
        if (
            source_result_hash is not None
            and record["map_payload"] is not None
            and record["source_result_hash"] != source_result_hash
        ):
            # The result-changing trigger normally persists this state. Keep
            # this read-side check as a second line of defence for imported DBs.
            record["status"] = "stale"
        return record

    def mark_understanding_map_pending(
        self,
        analysis_id: str,
        *,
        user_id: str,
        schema_version: int,
        source_result_hash: str,
        force: bool = False,
        stale_before: datetime | None = None,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Reserve generation and report whether the caller owns new work."""

        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        owner = _require_text(user_id, "user_id")
        analysis = _require_text(analysis_id, "analysis_id")
        source_hash = _require_text(
            source_result_hash,
            "source_result_hash",
        )
        target_schema = _require_schema_version(schema_version)
        timestamp = _utc_now(now)
        stale_cutoff = (
            _utc_now(stale_before) if stale_before is not None else None
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_owned_analysis(connection, analysis, owner)
            row = connection.execute(
                """
                SELECT *
                FROM understanding_maps
                WHERE analysis_id = ?
                """,
                (analysis,),
            ).fetchone()
            if row is not None:
                record = _map_record(row)
                if (
                    not force
                    and record["status"] == "complete"
                    and record["schema_version"] == target_schema
                    and record["source_result_hash"] == source_hash
                    and record["map_payload"] is not None
                ):
                    return record, False
                if (
                    record["status"] == "pending"
                    and record["pending_schema_version"] == target_schema
                    and record["pending_source_result_hash"] == source_hash
                    and (
                        stale_cutoff is None
                        or record["updated_at"] > stale_cutoff
                    )
                ):
                    return record, False
                connection.execute(
                    """
                    UPDATE understanding_maps
                    SET schema_version = CASE
                            WHEN map_json IS NULL THEN ?
                            ELSE schema_version
                        END,
                        pending_schema_version = ?,
                        pending_source_result_hash = ?,
                        status = 'pending',
                        last_error = NULL,
                        updated_at = ?
                    WHERE analysis_id = ?
                    """,
                    (
                        target_schema,
                        target_schema,
                        source_hash,
                        timestamp,
                        analysis,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO understanding_maps (
                        analysis_id, schema_version, source_result_hash,
                        pending_schema_version, pending_source_result_hash,
                        map_json, status, last_error, created_at, updated_at
                    ) VALUES (?, ?, NULL, ?, ?, NULL, 'pending', NULL, ?, ?)
                    """,
                    (
                        analysis,
                        target_schema,
                        target_schema,
                        source_hash,
                        timestamp,
                        timestamp,
                    ),
                )
            reserved = connection.execute(
                """
                SELECT *
                FROM understanding_maps
                WHERE analysis_id = ?
                """,
                (analysis,),
            ).fetchone()
        return _map_record(reserved), True

    def upsert_understanding_map(
        self,
        analysis_id: str,
        *,
        user_id: str,
        schema_version: int,
        source_result_hash: str,
        map_payload: Mapping[str, object],
        status: str = "complete",
        last_error: str | None = None,
        reservation_token: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically publish a map, rejecting an obsolete completion."""

        if status != "complete":
            raise ValueError("published understanding maps must be complete")
        owner = _require_text(user_id, "user_id")
        analysis = _require_text(analysis_id, "analysis_id")
        source_hash = _require_text(
            source_result_hash,
            "source_result_hash",
        )
        target_schema = _require_schema_version(schema_version)
        payload_json = _encode_json(dict(map_payload))
        timestamp = _utc_now(now)
        clean_error = _clean_optional_error(last_error)
        lease_token = (
            _require_text(reservation_token, "reservation_token")
            if reservation_token is not None
            else None
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_owned_analysis(connection, analysis, owner)
            existing = connection.execute(
                """
                SELECT *
                FROM understanding_maps
                WHERE analysis_id = ?
                """,
                (analysis,),
            ).fetchone()
            if existing is not None:
                existing_record = _map_record(existing)
                pending_hash = existing_record["pending_source_result_hash"]
                pending_schema = existing_record["pending_schema_version"]
                if (
                    existing_record["status"] == "pending"
                    and lease_token is not None
                    and existing_record["updated_at"] != lease_token
                ):
                    raise TeachingConflictError(
                        "understanding map lease is no longer owned by this request"
                    )
                if (
                    existing_record["status"] == "pending"
                    and (
                        pending_hash != source_hash
                        or pending_schema != target_schema
                    )
                ):
                    raise TeachingConflictError(
                        "导赏地图生成结果已过期，未覆盖较新的生成请求。"
                    )
                if (
                    existing_record["status"] == "complete"
                    and existing_record["source_result_hash"] == source_hash
                    and existing_record["schema_version"] == target_schema
                    and existing_record["map_payload"] is not None
                ):
                    return existing_record
                if existing_record["status"] != "pending":
                    raise TeachingConflictError(
                        "导赏地图未预留本次生成，拒绝覆盖现有地图。"
                    )
                connection.execute(
                    """
                    UPDATE understanding_maps
                    SET schema_version = ?,
                        source_result_hash = ?,
                        pending_schema_version = NULL,
                        pending_source_result_hash = NULL,
                        map_json = ?,
                        status = 'complete',
                        last_error = ?,
                        updated_at = ?
                    WHERE analysis_id = ?
                    """,
                    (
                        target_schema,
                        source_hash,
                        payload_json,
                        clean_error,
                        timestamp,
                        analysis,
                    ),
                )
            else:
                if lease_token is not None:
                    raise TeachingConflictError(
                        "understanding map lease no longer exists"
                    )
                connection.execute(
                    """
                    INSERT INTO understanding_maps (
                        analysis_id, schema_version, source_result_hash,
                        pending_schema_version, pending_source_result_hash,
                        map_json, status, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, NULL, NULL, ?, 'complete', ?, ?, ?)
                    """,
                    (
                        analysis,
                        target_schema,
                        source_hash,
                        payload_json,
                        clean_error,
                        timestamp,
                        timestamp,
                    ),
                )
            published = connection.execute(
                """
                SELECT *
                FROM understanding_maps
                WHERE analysis_id = ?
                """,
                (analysis,),
            ).fetchone()
        return _map_record(published)

    def fail_understanding_map(
        self,
        analysis_id: str,
        *,
        user_id: str,
        source_result_hash: str,
        error: str,
        schema_version: int | None = None,
        reservation_token: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Finish a reservation without destroying a previously usable map."""

        owner = _require_text(user_id, "user_id")
        analysis = _require_text(analysis_id, "analysis_id")
        requested_schema = (
            _require_schema_version(schema_version)
            if schema_version is not None
            else None
        )
        source_hash = _require_text(
            source_result_hash,
            "source_result_hash",
        )
        clean_error = _require_text(error, "error")[:2000]
        timestamp = _utc_now(now)
        lease_token = (
            _require_text(reservation_token, "reservation_token")
            if reservation_token is not None
            else None
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_owned_analysis(connection, analysis, owner)
            row = connection.execute(
                """
                SELECT *
                FROM understanding_maps
                WHERE analysis_id = ?
                """,
                (analysis,),
            ).fetchone()
            if row is None:
                if lease_token is not None:
                    raise TeachingConflictError(
                        "understanding map lease no longer exists"
                    )
                target_schema = requested_schema or 1
                connection.execute(
                    """
                    INSERT INTO understanding_maps (
                        analysis_id, schema_version, source_result_hash,
                        pending_schema_version, pending_source_result_hash,
                        map_json, status, last_error, created_at, updated_at
                    ) VALUES (?, ?, NULL, NULL, NULL, NULL, 'failed', ?, ?, ?)
                    """,
                    (
                        analysis,
                        target_schema,
                        clean_error,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                record = _map_record(row)
                if (
                    record["status"] == "pending"
                    and lease_token is not None
                    and record["updated_at"] != lease_token
                ):
                    raise TeachingConflictError(
                        "understanding map lease is no longer owned by this request"
                    )
                target_schema = (
                    requested_schema
                    or record["pending_schema_version"]
                    or record["schema_version"]
                )
                if record["status"] != "pending":
                    if lease_token is not None:
                        return record
                    if (
                        record["map_payload"] is not None
                        and (
                            record["source_result_hash"] != source_hash
                            or record["schema_version"] != target_schema
                        )
                    ):
                        return record
                    preserved_status = record["status"]
                    connection.execute(
                        """
                        UPDATE understanding_maps
                        SET last_error = ?, updated_at = ?
                        WHERE analysis_id = ?
                        """,
                        (clean_error, timestamp, analysis),
                    )
                    unchanged = connection.execute(
                        """
                        SELECT *
                        FROM understanding_maps
                        WHERE analysis_id = ?
                        """,
                        (analysis,),
                    ).fetchone()
                    result = _map_record(unchanged)
                    result["status"] = preserved_status
                    return result
                if (
                    record["status"] == "pending"
                    and (
                        record["pending_source_result_hash"] != source_hash
                        or record["pending_schema_version"] != target_schema
                    )
                ):
                    raise TeachingConflictError(
                        "失败结果属于过期的导赏地图生成请求。"
                    )
                if record["map_payload"] is None:
                    restored_status = "failed"
                elif (
                    record["source_result_hash"] == source_hash
                    and record["schema_version"] == target_schema
                ):
                    restored_status = "complete"
                else:
                    restored_status = "stale"
                connection.execute(
                    """
                    UPDATE understanding_maps
                    SET pending_schema_version = NULL,
                        pending_source_result_hash = NULL,
                        status = ?,
                        last_error = ?,
                        updated_at = ?
                    WHERE analysis_id = ?
                    """,
                    (
                        restored_status,
                        clean_error,
                        timestamp,
                        analysis,
                    ),
                )
            failed = connection.execute(
                """
                SELECT *
                FROM understanding_maps
                WHERE analysis_id = ?
                """,
                (analysis,),
            ).fetchone()
        return _map_record(failed)

    def get_listener_profile(
        self,
        *,
        user_id: str,
    ) -> dict[str, Any] | None:
        owner = _require_text(user_id, "user_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM listener_profiles
                WHERE user_id = ?
                """,
                (owner,),
            ).fetchone()
        return _profile_record(row) if row is not None else None

    def upsert_listener_profile(
        self,
        *,
        user_id: str,
        level: str,
        preferences: Mapping[str, object] | None = None,
        learned_concepts: list[str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        owner = _require_text(user_id, "user_id")
        if level not in _PROFILE_LEVELS:
            raise ValueError(f"unsupported listener level: {level}")
        preferences_json = _encode_json(dict(preferences or {}))
        concepts_json = _encode_json(
            _normalize_concepts(learned_concepts or [])
        )
        timestamp = _utc_now(now)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO listener_profiles (
                        user_id, level, preferences_json,
                        learned_concepts_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        level = excluded.level,
                        preferences_json = excluded.preferences_json,
                        learned_concepts_json =
                            excluded.learned_concepts_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        owner,
                        level,
                        preferences_json,
                        concepts_json,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT *
                    FROM listener_profiles
                    WHERE user_id = ?
                    """,
                    (owner,),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            if "foreign key" in str(exc).lower():
                raise TeachingEntryNotFoundError("用户不存在。") from exc
            raise
        return _profile_record(row)

    def update_listener_profile(
        self,
        *,
        user_id: str,
        level: str,
        preferences: Mapping[str, object] | None = None,
        learned_concepts: list[str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self.upsert_listener_profile(
            user_id=user_id,
            level=level,
            preferences=preferences,
            learned_concepts=learned_concepts,
            now=now,
        )

    def create_conversation(
        self,
        analysis_id: str,
        *,
        user_id: str,
        title: str | None = None,
        summary: Mapping[str, object] | None = None,
        conversation_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        owner = _require_text(user_id, "user_id")
        analysis = _require_text(analysis_id, "analysis_id")
        clean_title = _clean_title(title)
        identifier = (
            _require_text(conversation_id, "conversation_id")
            if conversation_id is not None
            else uuid4().hex
        )
        summary_json = (
            _encode_json(dict(summary)) if summary is not None else None
        )
        timestamp = _utc_now(now)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO music_conversations (
                        id, analysis_id, user_id, title, summary_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        analysis,
                        owner,
                        clean_title,
                        summary_json,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT *
                    FROM music_conversations
                    WHERE id = ?
                    """,
                    (identifier,),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            detail = str(exc).lower()
            if "music conversation owner mismatch" in detail:
                raise TeachingEntryNotFoundError(
                    "歌曲不存在或不属于当前用户。"
                ) from exc
            raise
        return _conversation_record(row)

    def list_conversations(
        self,
        analysis_id: str,
        *,
        user_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        owner = _require_text(user_id, "user_id")
        analysis = _require_text(analysis_id, "analysis_id")
        bounded_limit = max(1, min(limit, 500))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT music_conversations.*,
                       COUNT(music_messages.id) AS message_count
                FROM music_conversations
                LEFT JOIN music_messages
                  ON music_messages.conversation_id =
                     music_conversations.id
                WHERE music_conversations.analysis_id = ?
                  AND music_conversations.user_id = ?
                GROUP BY music_conversations.id
                ORDER BY music_conversations.updated_at DESC,
                         music_conversations.id DESC
                LIMIT ?
                """,
                (analysis, owner, bounded_limit),
            ).fetchall()
        return [_conversation_record(row) for row in rows]

    def get_conversation(
        self,
        conversation_id: str,
        *,
        analysis_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        owner = _require_text(user_id, "user_id")
        analysis = _require_text(analysis_id, "analysis_id")
        conversation = _require_text(conversation_id, "conversation_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT music_conversations.*,
                       COUNT(music_messages.id) AS message_count
                FROM music_conversations
                LEFT JOIN music_messages
                  ON music_messages.conversation_id =
                     music_conversations.id
                WHERE music_conversations.id = ?
                  AND music_conversations.analysis_id = ?
                  AND music_conversations.user_id = ?
                GROUP BY music_conversations.id
                """,
                (conversation, analysis, owner),
            ).fetchone()
        return _conversation_record(row) if row is not None else None

    def delete_conversation(
        self,
        conversation_id: str,
        *,
        analysis_id: str,
        user_id: str,
    ) -> bool:
        owner = _require_text(user_id, "user_id")
        analysis = _require_text(analysis_id, "analysis_id")
        conversation = _require_text(conversation_id, "conversation_id")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM music_conversations
                WHERE id = ? AND analysis_id = ? AND user_id = ?
                """,
                (conversation, analysis, owner),
            )
        return cursor.rowcount == 1

    def reserve_message(
        self,
        conversation_id: str,
        *,
        analysis_id: str,
        user_id: str,
        client_request_id: str,
        request_payload: Mapping[str, object],
        stale_before: datetime | None = None,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Reserve exactly one sequence for an idempotent client request."""

        owner = _require_text(user_id, "user_id")
        analysis = _require_text(analysis_id, "analysis_id")
        conversation = _require_text(conversation_id, "conversation_id")
        request_id = _require_text(
            client_request_id,
            "client_request_id",
        )
        if len(request_id) > 200:
            raise ValueError("client_request_id is too long")
        request_json = _encode_json(dict(request_payload))
        timestamp = _utc_now(now)
        stale_cutoff = (
            _utc_now(stale_before) if stale_before is not None else None
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_owned_conversation(
                connection,
                conversation,
                analysis,
                owner,
            )
            existing = connection.execute(
                """
                SELECT *
                FROM music_messages
                WHERE conversation_id = ?
                  AND client_request_id = ?
                """,
                (conversation, request_id),
            ).fetchone()
            if existing is not None:
                if existing["request_json"] != request_json:
                    raise TeachingConflictError(
                        "同一 client_request_id 不能对应不同问题。"
                    )
                can_retry_failed = existing["status"] == "failed"
                can_reclaim_expired = (
                    existing["status"] == "pending"
                    and stale_cutoff is not None
                    and existing["updated_at"] <= stale_cutoff
                )
                if can_retry_failed or can_reclaim_expired:
                    connection.execute(
                        """
                        UPDATE music_messages
                        SET status = 'pending',
                            response_json = NULL,
                            error = NULL,
                            updated_at = ?
                        WHERE id = ? AND status = ?
                          AND updated_at = ?
                        """,
                        (
                            timestamp,
                            existing["id"],
                            existing["status"],
                            existing["updated_at"],
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE music_conversations
                        SET updated_at = ?
                        WHERE id = ?
                        """,
                        (timestamp, conversation),
                    )
                    reclaimed = connection.execute(
                        """
                        SELECT *
                        FROM music_messages
                        WHERE id = ?
                        """,
                        (existing["id"],),
                    ).fetchone()
                    return _message_record(reclaimed), True
                return _message_record(existing), False
            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM music_messages
                    WHERE conversation_id = ?
                    """,
                    (conversation,),
                ).fetchone()[0]
            )
            identifier = uuid4().hex
            connection.execute(
                """
                INSERT INTO music_messages (
                    id, conversation_id, sequence, client_request_id,
                    status, request_json, response_json, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, NULL, NULL, ?, ?)
                """,
                (
                    identifier,
                    conversation,
                    sequence,
                    request_id,
                    request_json,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE music_conversations
                SET updated_at = ?
                WHERE id = ?
                """,
                (timestamp, conversation),
            )
            row = connection.execute(
                """
                SELECT *
                FROM music_messages
                WHERE id = ?
                """,
                (identifier,),
            ).fetchone()
        return _message_record(row), True

    def complete_message(
        self,
        message_id: str,
        *,
        user_id: str,
        response_payload: Mapping[str, object],
        reservation_token: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        owner = _require_text(user_id, "user_id")
        identifier = _require_text(message_id, "message_id")
        response_json = _encode_json(dict(response_payload))
        timestamp = _utc_now(now)
        lease_token = (
            _require_text(reservation_token, "reservation_token")
            if reservation_token is not None
            else None
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _owned_message(connection, identifier, owner)
            if row is None:
                return None
            if row["status"] == "complete":
                if row["response_json"] != response_json:
                    raise TeachingConflictError(
                        "已完成的请求不能写入不同回答。"
                    )
                return _message_record(row)
            if row["status"] != "pending":
                raise TeachingConflictError("失败的请求必须先重新预留。")
            if lease_token is not None and row["updated_at"] != lease_token:
                raise TeachingConflictError(
                    "music message lease is no longer owned by this request"
                )
            connection.execute(
                """
                UPDATE music_messages
                SET status = 'complete',
                    response_json = ?,
                    error = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (response_json, timestamp, identifier),
            )
            connection.execute(
                """
                UPDATE music_conversations
                SET updated_at = ?
                WHERE id = ?
                """,
                (timestamp, row["conversation_id"]),
            )
            completed = connection.execute(
                """
                SELECT *
                FROM music_messages
                WHERE id = ?
                """,
                (identifier,),
            ).fetchone()
        return _message_record(completed)

    def fail_message(
        self,
        message_id: str,
        *,
        user_id: str,
        error: str,
        reservation_token: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        owner = _require_text(user_id, "user_id")
        identifier = _require_text(message_id, "message_id")
        clean_error = _require_text(error, "error")[:2000]
        timestamp = _utc_now(now)
        lease_token = (
            _require_text(reservation_token, "reservation_token")
            if reservation_token is not None
            else None
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _owned_message(connection, identifier, owner)
            if row is None:
                return None
            if row["status"] == "complete":
                return _message_record(row)
            if row["status"] == "failed":
                return _message_record(row)
            if lease_token is not None and row["updated_at"] != lease_token:
                raise TeachingConflictError(
                    "music message lease is no longer owned by this request"
                )
            connection.execute(
                """
                UPDATE music_messages
                SET status = 'failed',
                    error = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (clean_error, timestamp, identifier),
            )
            connection.execute(
                """
                UPDATE music_conversations
                SET updated_at = ?
                WHERE id = ?
                """,
                (timestamp, row["conversation_id"]),
            )
            failed = connection.execute(
                """
                SELECT *
                FROM music_messages
                WHERE id = ?
                """,
                (identifier,),
            ).fetchone()
        return _message_record(failed)

    def list_messages(
        self,
        conversation_id: str,
        *,
        analysis_id: str,
        user_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        owner = _require_text(user_id, "user_id")
        analysis = _require_text(analysis_id, "analysis_id")
        conversation = _require_text(conversation_id, "conversation_id")
        bounded_limit = max(1, min(limit, 500))
        with self._connect() as connection:
            _require_owned_conversation(
                connection,
                conversation,
                analysis,
                owner,
            )
            rows = connection.execute(
                """
                SELECT *
                FROM (
                    SELECT *
                    FROM music_messages
                    WHERE conversation_id = ?
                    ORDER BY sequence DESC
                    LIMIT ?
                )
                ORDER BY sequence ASC
                """,
                (conversation, bounded_limit),
            ).fetchall()
        return [_message_record(row) for row in rows]

    def recover_pending(
        self,
        *,
        before: datetime | None = None,
        error: str = _INTERRUPTED_ERROR,
    ) -> dict[str, int]:
        """Recover abandoned reservations selected by an explicit cutoff."""

        cutoff = _utc_now(before)
        clean_error = _require_text(error, "error")[:2000]
        timestamp = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            map_cursor = connection.execute(
                """
                UPDATE understanding_maps
                SET status = CASE
                        WHEN map_json IS NULL THEN 'failed'
                        WHEN source_result_hash =
                             pending_source_result_hash
                         AND schema_version =
                             pending_schema_version
                            THEN 'complete'
                        ELSE 'stale'
                    END,
                    pending_schema_version = NULL,
                    pending_source_result_hash = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE status = 'pending' AND updated_at <= ?
                """,
                (clean_error, timestamp, cutoff),
            )
            message_cursor = connection.execute(
                """
                UPDATE music_messages
                SET status = 'failed',
                    error = ?,
                    updated_at = ?
                WHERE status = 'pending' AND updated_at <= ?
                """,
                (clean_error, timestamp, cutoff),
            )
        return {
            "understanding_maps": map_cursor.rowcount,
            "music_messages": message_cursor.rowcount,
        }


def _require_owned_analysis(
    connection: sqlite3.Connection,
    analysis_id: str,
    user_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT id
        FROM analyses
        WHERE id = ? AND user_id = ?
        """,
        (analysis_id, user_id),
    ).fetchone()
    if row is None:
        raise TeachingEntryNotFoundError(
            "歌曲不存在或不属于当前用户。"
        )
    return row


def _require_owned_conversation(
    connection: sqlite3.Connection,
    conversation_id: str,
    analysis_id: str,
    user_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT *
        FROM music_conversations
        WHERE id = ? AND analysis_id = ? AND user_id = ?
        """,
        (conversation_id, analysis_id, user_id),
    ).fetchone()
    if row is None:
        raise TeachingEntryNotFoundError(
            "导赏对话不存在或不属于当前用户。"
        )
    return row


def _owned_message(
    connection: sqlite3.Connection,
    message_id: str,
    user_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT music_messages.*
        FROM music_messages
        JOIN music_conversations
          ON music_conversations.id = music_messages.conversation_id
        WHERE music_messages.id = ?
          AND music_conversations.user_id = ?
        """,
        (message_id, user_id),
    ).fetchone()


def _map_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "analysis_id": row["analysis_id"],
        "schema_version": row["schema_version"],
        "source_result_hash": row["source_result_hash"],
        "pending_schema_version": row["pending_schema_version"],
        "pending_source_result_hash": row["pending_source_result_hash"],
        "map_payload": _decode_json(row["map_json"], None),
        "status": row["status"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _profile_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "user_id": row["user_id"],
        "level": row["level"],
        "preferences": _decode_json(row["preferences_json"], {}),
        "learned_concepts": _decode_json(
            row["learned_concepts_json"],
            [],
        ),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _conversation_record(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "analysis_id": row["analysis_id"],
        "user_id": row["user_id"],
        "title": row["title"],
        "summary": _decode_json(row["summary_json"], None),
        "message_count": (
            int(row["message_count"]) if "message_count" in keys else 0
        ),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _message_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "sequence": row["sequence"],
        "client_request_id": row["client_request_id"],
        "status": row["status"],
        "request_payload": _decode_json(row["request_json"], {}),
        "response_payload": _decode_json(row["response_json"], None),
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _encode_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("teaching payload must be valid JSON") from exc


def _decode_json(payload: str | None, default: Any) -> Any:
    if payload is None:
        return default
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TeachingDataError("persisted teaching JSON is invalid") from exc


def _normalize_concepts(concepts: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for concept in concepts:
        if not isinstance(concept, str):
            raise ValueError("learned concepts must be strings")
        cleaned = " ".join(concept.split()).strip()[:120]
        if cleaned and cleaned not in seen:
            normalized.append(cleaned)
            seen.add(cleaned)
        if len(normalized) >= 200:
            break
    return normalized


def _clean_title(title: str | None) -> str:
    if title is None:
        return "音乐导赏"
    cleaned = " ".join(title.split()).strip()[:120]
    return cleaned or "音乐导赏"


def _clean_optional_error(error: str | None) -> str | None:
    if error is None:
        return None
    cleaned = " ".join(error.split()).strip()[:2000]
    return cleaned or None


def _require_schema_version(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("schema_version must be a positive integer")
    return value


def _require_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _utc_now(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat()
