from __future__ import annotations

from collections.abc import Callable
import json
import sqlite3

from music_insight.storage.assets import cached_paths

LATEST_SCHEMA_VERSION = 6


def migration_1_history(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS analyses (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            title TEXT NOT NULL,
            file_name TEXT NOT NULL,
            language TEXT,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            audio_path TEXT NOT NULL,
            result_json TEXT,
            error TEXT,
            model_source TEXT NOT NULL DEFAULT 'network',
            model_location TEXT
        )
        """
    )
    _ensure_column(connection, "analyses", "user_id", "TEXT")
    _ensure_column(
        connection,
        "analyses",
        "model_source",
        "TEXT NOT NULL DEFAULT 'network'",
    )
    _ensure_column(connection, "analyses", "model_location", "TEXT")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            result_json TEXT NOT NULL,
            FOREIGN KEY (analysis_id) REFERENCES analyses(id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_analyses_user_created
        ON analyses(user_id, created_at DESC)
        """
    )


def migration_2_accounts(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            username_key TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash BLOB PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS sessions_user_id_idx
        ON sessions(user_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS sessions_expires_at_idx
        ON sessions(expires_at)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS singing_attempts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            source TEXT NOT NULL
                CHECK (source IN ('history', 'standalone')),
            category TEXT NOT NULL DEFAULT 'entertainment',
            history_id TEXT,
            reference_name TEXT,
            performance_name TEXT,
            created_at TEXT NOT NULL,
            total INTEGER NOT NULL CHECK (total BETWEEN 0 AND 100),
            pitch INTEGER NOT NULL CHECK (pitch BETWEEN 0 AND 100),
            rhythm INTEGER NOT NULL CHECK (rhythm BETWEEN 0 AND 100),
            completeness INTEGER NOT NULL
                CHECK (completeness BETWEEN 0 AND 100),
            stability INTEGER NOT NULL
                CHECK (stability BETWEEN 0 AND 100),
            score_json TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS singing_attempts_user_created_idx
        ON singing_attempts(user_id, created_at DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS singing_attempts_leaderboard_idx
        ON singing_attempts(
            category, total DESC, pitch DESC, rhythm DESC, created_at
        )
        """
    )


def migration_3_history_projections_and_assets(
    connection: sqlite3.Connection,
) -> None:
    _ensure_column(connection, "analyses", "summary_text", "TEXT")
    _ensure_column(connection, "analyses", "duration_s", "REAL")
    _ensure_column(
        connection,
        "analyses",
        "lyrics_count",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        connection,
        "analyses",
        "instruments_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(connection, "analyses", "bpm", "REAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_assets (
            analysis_id TEXT NOT NULL,
            path TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('source', 'derived')),
            created_at TEXT NOT NULL,
            PRIMARY KEY (analysis_id, path),
            FOREIGN KEY (analysis_id) REFERENCES analyses(id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS analysis_assets_path_idx
        ON analysis_assets(path)
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO analysis_assets (
            analysis_id, path, kind, created_at
        )
        SELECT id, audio_path, 'source', updated_at
        FROM analyses
        WHERE audio_path <> ''
        """
    )
    _backfill_result_projections_and_assets(connection)


def migration_4_owner_integrity(connection: sqlite3.Connection) -> None:
    """Enforce owner references without rebuilding legacy history tables."""

    connection.execute(
        """
        UPDATE analyses
        SET user_id = NULL
        WHERE user_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM users WHERE users.id = analyses.user_id
          )
        """
    )
    connection.execute(
        """
        UPDATE singing_attempts
        SET history_id = NULL
        WHERE history_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM analyses
              WHERE analyses.id = singing_attempts.history_id
                AND analyses.user_id = singing_attempts.user_id
          )
        """
    )
    for statement in (
        """
        CREATE TRIGGER IF NOT EXISTS analyses_owner_insert
        BEFORE INSERT ON analyses
        WHEN NEW.user_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.user_id)
        BEGIN
            SELECT RAISE(ABORT, 'analyses owner does not exist');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS analyses_owner_update
        BEFORE UPDATE OF user_id ON analyses
        WHEN NEW.user_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.user_id)
        BEGIN
            SELECT RAISE(ABORT, 'analyses owner does not exist');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS singing_history_owner_insert
        BEFORE INSERT ON singing_attempts
        WHEN NEW.history_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM analyses
              WHERE id = NEW.history_id AND user_id = NEW.user_id
          )
        BEGIN
            SELECT RAISE(ABORT, 'singing history owner mismatch');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS singing_history_owner_update
        BEFORE UPDATE OF history_id, user_id ON singing_attempts
        WHEN NEW.history_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM analyses
              WHERE id = NEW.history_id AND user_id = NEW.user_id
          )
        BEGIN
            SELECT RAISE(ABORT, 'singing history owner mismatch');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS analyses_clear_singing_history
        AFTER DELETE ON analyses
        BEGIN
            UPDATE singing_attempts
            SET history_id = NULL
            WHERE history_id = OLD.id;
        END
        """,
    ):
        connection.execute(statement)


def migration_5_asset_content_keys(connection: sqlite3.Connection) -> None:
    """Persist new source digests; legacy rows are lazily hashed by GC once."""

    _ensure_column(connection, "analysis_assets", "content_key", "TEXT")


def migration_6_music_teaching(connection: sqlite3.Connection) -> None:
    """Persist evidence-grounded teaching maps and owner-scoped dialogue."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS understanding_maps (
            analysis_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
            source_result_hash TEXT,
            pending_schema_version INTEGER
                CHECK (pending_schema_version >= 1),
            pending_source_result_hash TEXT,
            map_json TEXT,
            status TEXT NOT NULL
                CHECK (status IN ('pending', 'complete', 'stale', 'failed')),
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (
                    status = 'pending'
                    AND pending_schema_version IS NOT NULL
                    AND pending_source_result_hash IS NOT NULL
                )
                OR (
                    status <> 'pending'
                    AND pending_schema_version IS NULL
                    AND pending_source_result_hash IS NULL
                )
            ),
            CHECK (
                status NOT IN ('complete', 'stale')
                OR (
                    map_json IS NOT NULL
                    AND source_result_hash IS NOT NULL
                )
            ),
            FOREIGN KEY (analysis_id) REFERENCES analyses(id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS understanding_maps_status_updated_idx
        ON understanding_maps(status, updated_at)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS listener_profiles (
            user_id TEXT PRIMARY KEY,
            level TEXT NOT NULL DEFAULT 'beginner'
                CHECK (
                    level IN (
                        'beginner', 'curious', 'intermediate', 'advanced'
                    )
                ),
            preferences_json TEXT NOT NULL DEFAULT '{}',
            learned_concepts_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS music_conversations (
            id TEXT PRIMARY KEY,
            analysis_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            summary_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (analysis_id) REFERENCES analyses(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS music_conversations_analysis_owner_idx
        ON music_conversations(analysis_id, user_id, updated_at DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS music_conversations_owner_updated_idx
        ON music_conversations(user_id, updated_at DESC)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS music_messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK (sequence >= 1),
            client_request_id TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('pending', 'complete', 'failed')),
            request_json TEXT NOT NULL,
            response_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (
                    status = 'pending'
                    AND response_json IS NULL
                    AND error IS NULL
                )
                OR (
                    status = 'complete'
                    AND response_json IS NOT NULL
                    AND error IS NULL
                )
                OR (
                    status = 'failed'
                    AND response_json IS NULL
                    AND error IS NOT NULL
                )
            ),
            FOREIGN KEY (conversation_id) REFERENCES music_conversations(id)
                ON DELETE CASCADE,
            UNIQUE (conversation_id, sequence),
            UNIQUE (conversation_id, client_request_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS music_messages_conversation_sequence_idx
        ON music_messages(conversation_id, sequence)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS music_messages_status_updated_idx
        ON music_messages(status, updated_at)
        """
    )
    for statement in (
        """
        CREATE TRIGGER IF NOT EXISTS music_conversations_owner_insert
        BEFORE INSERT ON music_conversations
        WHEN NOT EXISTS (
            SELECT 1
            FROM analyses
            WHERE id = NEW.analysis_id
              AND user_id = NEW.user_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'music conversation owner mismatch');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS music_conversations_owner_update
        BEFORE UPDATE OF analysis_id, user_id ON music_conversations
        WHEN NOT EXISTS (
            SELECT 1
            FROM analyses
            WHERE id = NEW.analysis_id
              AND user_id = NEW.user_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'music conversation owner mismatch');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS analyses_preserve_conversation_owner
        BEFORE UPDATE OF user_id ON analyses
        WHEN OLD.user_id IS NOT NEW.user_id
          AND EXISTS (
              SELECT 1
              FROM music_conversations
              WHERE analysis_id = OLD.id
          )
        BEGIN
            SELECT RAISE(ABORT, 'analysis has music conversations');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS analyses_stale_understanding_map
        AFTER UPDATE OF result_json ON analyses
        WHEN OLD.result_json IS NOT NEW.result_json
        BEGIN
            UPDATE understanding_maps
            SET status = CASE
                    WHEN map_json IS NULL THEN 'failed'
                    ELSE 'stale'
                END,
                pending_schema_version = NULL,
                pending_source_result_hash = NULL,
                last_error = NULL,
                updated_at = NEW.updated_at
            WHERE analysis_id = NEW.id;
        END
        """,
    ):
        connection.execute(statement)


def result_projection(
    payload: dict[str, object],
) -> tuple[str | None, float | None, int, str, float | None]:
    summary = payload.get("summary")
    lyrics = payload.get("lyrics")
    instruments = payload.get("instruments")
    metrics = payload.get("technical_metrics")
    safe_instruments = (
        [str(item) for item in instruments[:8]]
        if isinstance(instruments, list)
        else []
    )
    duration_s: float | None = None
    bpm: float | None = None
    if isinstance(metrics, dict):
        bpm = _optional_float(metrics.get("bpm"))
        evidence = metrics.get("evidence")
        if isinstance(evidence, list):
            for item in evidence:
                if not isinstance(item, dict):
                    continue
                metadata = item.get("metadata")
                if isinstance(metadata, dict):
                    duration_s = _optional_float(metadata.get("duration_s"))
                    if duration_s is not None:
                        break
    return (
        str(summary) if summary is not None else None,
        duration_s,
        len(lyrics) if isinstance(lyrics, list) else 0,
        json.dumps(safe_instruments, ensure_ascii=False, separators=(",", ":")),
        bpm,
    )


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
        )


def _backfill_result_projections_and_assets(
    connection: sqlite3.Connection,
) -> None:
    rows = connection.execute(
        """
        SELECT id, updated_at, result_json
        FROM analyses
        WHERE result_json IS NOT NULL
        """
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["result_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        projection = result_projection(payload)
        connection.execute(
            """
            UPDATE analyses
            SET summary_text = ?, duration_s = ?, lyrics_count = ?,
                instruments_json = ?, bpm = ?
            WHERE id = ?
            """,
            (*projection, row["id"]),
        )
        for path in cached_paths(payload):
            connection.execute(
                """
                INSERT OR IGNORE INTO analysis_assets (
                    analysis_id, path, kind, created_at
                ) VALUES (?, ?, 'derived', ?)
                """,
                (row["id"], path, row["updated_at"]),
            )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


MIGRATIONS: tuple[
    tuple[int, Callable[[sqlite3.Connection], None]],
    ...,
] = (
    (1, migration_1_history),
    (2, migration_2_accounts),
    (3, migration_3_history_projections_and_assets),
    (4, migration_4_owner_integrity),
    (5, migration_5_asset_content_keys),
    (6, migration_6_music_teaching),
)
