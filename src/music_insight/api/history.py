from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from typing import ContextManager

from music_insight.api.contracts.history import (
    HistoryDetail,
    HistoryLyricsRetryRequest,
    HistoryLyricsRetryResult,
    HistoryLyricsUpdate,
    HistoryRename,
    HistoryRevision,
    HistorySummary,
)
from music_insight.api.database import database_session, migrate_database
from music_insight.api.history_mapping import (
    analysis_result_from_json,
    detail_from_row,
    summary_from_row,
)
from music_insight.api.history_revisions import (
    apply_lyrics_revision,
    load_revisions,
    next_revision_number,
    persist_lyrics_revision,
)
from music_insight.api.migrations import result_projection
from music_insight.schemas import AnalysisResult, LyricsSegment
from music_insight.storage.assets import AssetCleanupReport
from music_insight.storage.history_assets import HistoryAssetRegistry


__all__ = [
    "HistoryDetail",
    "HistoryEntryNotFoundError",
    "HistoryLyricsRetryRequest",
    "HistoryLyricsRetryResult",
    "HistoryLyricsUpdate",
    "HistoryRename",
    "HistoryRevision",
    "HistoryStore",
    "HistorySummary",
]


class HistoryEntryNotFoundError(LookupError):
    """Raised when an owner-scoped history update matched no record."""


class HistoryStore:
    """SQLite history whose business operations always require an owner."""

    def __init__(
        self,
        database_path: Path,
        *,
        source_roots: tuple[Path, ...] = (),
    ) -> None:
        self.database_path = database_path
        self.workspace_dir = database_path.parent.resolve()
        self.assets = HistoryAssetRegistry(
            self.workspace_dir,
            source_roots=source_roots,
        )
        migrate_database(database_path)

    def _connect(self) -> ContextManager[sqlite3.Connection]:
        return database_session(self.database_path)

    def recover_interrupted_jobs(
        self,
        *,
        active_job_ids: set[str] | None = None,
    ) -> int:
        """Mark non-terminal rows that no active runtime still owns."""

        with self._connect() as connection:
            now = datetime.now(UTC).isoformat()
            active = sorted(active_job_ids or set())
            exclusion = (
                f" AND id NOT IN ({','.join('?' for _ in active)})"
                if active
                else ""
            )
            parameters = tuple(active)
            interrupted_ids = [
                row["id"]
                for row in connection.execute(
                    f"""
                    SELECT id
                    FROM analyses
                    WHERE state IN ('queued', 'running')
                    {exclusion}
                    """,
                    parameters,
                ).fetchall()
            ]
            cursor = connection.execute(
                f"""
                UPDATE analyses
                SET state = 'failed', updated_at = ?,
                    error = COALESCE(error, '服务重启，未完成的任务已中断。'),
                    result_json = NULL, summary_text = NULL,
                    duration_s = NULL, lyrics_count = 0,
                    instruments_json = '[]', bpm = NULL
                WHERE state IN ('queued', 'running')
                {exclusion}
                """,
                (now, *parameters),
            )
            if interrupted_ids:
                connection.executemany(
                    """
                    DELETE FROM analysis_assets
                    WHERE analysis_id = ? AND kind = 'derived'
                    """,
                    ((analysis_id,) for analysis_id in interrupted_ids),
                )
        return cursor.rowcount

    def create(
        self,
        *,
        job_id: str,
        title: str,
        file_name: str,
        language: str | None,
        state: str,
        created_at: datetime,
        updated_at: datetime,
        audio_path: Path,
        model_source: str = "network",
        model_location: str | None = None,
        user_id: str,
    ) -> None:
        owner = self._require_owner(user_id)
        self._create_record(
            job_id=job_id,
            title=title,
            file_name=file_name,
            language=language,
            state=state,
            created_at=created_at,
            updated_at=updated_at,
            audio_path=audio_path,
            model_source=model_source,
            model_location=model_location,
            user_id=owner,
        )

    def create_legacy_unowned_for_migration(
        self,
        *,
        job_id: str,
        title: str,
        file_name: str,
        language: str | None,
        state: str,
        created_at: datetime,
        updated_at: datetime,
        audio_path: Path,
        model_source: str = "network",
        model_location: str | None = None,
    ) -> None:
        """Explicit maintenance hook; normal application code must not use it."""

        self._create_record(
            job_id=job_id,
            title=title,
            file_name=file_name,
            language=language,
            state=state,
            created_at=created_at,
            updated_at=updated_at,
            audio_path=audio_path,
            model_source=model_source,
            model_location=model_location,
            user_id=None,
        )

    def _create_record(
        self,
        *,
        job_id: str,
        title: str,
        file_name: str,
        language: str | None,
        state: str,
        created_at: datetime,
        updated_at: datetime,
        audio_path: Path,
        model_source: str,
        model_location: str | None,
        user_id: str | None,
    ) -> None:
        # Hash before opening the write transaction so a large source file does
        # not hold SQLite's single writer lock during disk I/O.
        source_content_key = self.assets.source_content_key(audio_path)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO analyses (
                    id, user_id, title, file_name, language, state, created_at,
                    updated_at, audio_path, model_source, model_location
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    user_id,
                    title,
                    file_name,
                    language,
                    state,
                    created_at.isoformat(),
                    updated_at.isoformat(),
                    str(audio_path),
                    model_source,
                    model_location,
                ),
            )
            self.assets.register_source(
                connection,
                analysis_id=job_id,
                path=audio_path,
                timestamp=updated_at,
                content_key=source_content_key,
            )

    def update(
        self,
        job_id: str,
        *,
        state: str,
        updated_at: datetime,
        result: AnalysisResult | None = None,
        error: str | None = None,
        user_id: str,
    ) -> None:
        owner = self._require_owner(user_id)
        payload = result.model_dump(mode="json") if result is not None else None
        result_json = result.model_dump_json() if result is not None else None
        with self._connect() as connection:
            if payload is None:
                cursor = connection.execute(
                    """
                    UPDATE analyses
                    SET state = ?, updated_at = ?, error = ?,
                        result_json = NULL, summary_text = NULL,
                        duration_s = NULL, lyrics_count = 0,
                        instruments_json = '[]', bpm = NULL
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        state,
                        updated_at.isoformat(),
                        error,
                        job_id,
                        owner,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE analyses
                    SET state = ?, updated_at = ?, result_json = ?, error = ?,
                        summary_text = ?, duration_s = ?, lyrics_count = ?,
                        instruments_json = ?, bpm = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        state,
                        updated_at.isoformat(),
                        result_json,
                        error,
                        *result_projection(payload),
                        job_id,
                        owner,
                    ),
                )
            if cursor.rowcount != 1:
                raise HistoryEntryNotFoundError(
                    f"历史记录不存在或不属于当前用户：{job_id}"
                )
            if payload is None:
                self.assets.clear_result(
                    connection,
                    analysis_id=job_id,
                )
            else:
                self.assets.register_result(
                    connection,
                    analysis_id=job_id,
                    payload=payload,
                    timestamp=updated_at,
                )

    def list(
        self,
        *,
        user_id: str,
        limit: int = 100,
    ) -> list[HistorySummary]:
        owner = self._require_owner(user_id)
        return self._list_records(limit=limit, user_id=owner)

    def list_all_for_maintenance(
        self,
        *,
        limit: int = 100,
    ) -> list[HistorySummary]:
        """Explicit unscoped inspection for migration tooling and tests."""

        return self._list_records(limit=limit, user_id=None)

    def _list_records(
        self,
        *,
        limit: int,
        user_id: str | None,
    ) -> list[HistorySummary]:
        owner_clause = "" if user_id is None else "WHERE user_id = ?"
        parameters = (
            (max(1, min(limit, 500)),)
            if user_id is None
            else (user_id, max(1, min(limit, 500)))
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    id, title, file_name, language, state, created_at,
                    updated_at, error, summary_text, duration_s,
                    lyrics_count, instruments_json, bpm, model_source,
                    model_location
                FROM analyses
                {owner_clause}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._summary(row) for row in rows]

    def get(
        self,
        job_id: str,
        *,
        user_id: str,
    ) -> HistoryDetail | None:
        return self._get_record(job_id, user_id=self._require_owner(user_id))

    def get_for_maintenance(self, job_id: str) -> HistoryDetail | None:
        """Explicit unscoped lookup for migration tooling and tests."""

        return self._get_record(job_id, user_id=None)

    def _get_record(
        self,
        job_id: str,
        *,
        user_id: str | None,
    ) -> HistoryDetail | None:
        owner_clause = "" if user_id is None else " AND user_id = ?"
        parameters = (job_id,) if user_id is None else (job_id, user_id)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM analyses WHERE id = ?{owner_clause}",
                parameters,
            ).fetchone()
            if row is None:
                return None
            result = self._result(row["result_json"])
            revision_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM analysis_revisions
                WHERE analysis_revisions.analysis_id = ?
                """,
                (job_id,),
            ).fetchone()[0]
        return detail_from_row(
            row,
            result=result,
            revision_count=revision_count,
        )

    def rename(
        self,
        job_id: str,
        title: str,
        *,
        user_id: str,
    ) -> HistoryDetail | None:
        owner = self._require_owner(user_id)
        cleaned = " ".join(title.split()).strip()[:120]
        if not cleaned:
            return None
        parameters = (
            cleaned,
            datetime.now(UTC).isoformat(),
            job_id,
            owner,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE analyses
                SET title = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                parameters,
            )
            if cursor.rowcount == 0:
                return None
        return self.get(job_id, user_id=owner)

    def update_lyrics(
        self,
        job_id: str,
        lyrics: list[LyricsSegment],
        *,
        user_id: str,
    ) -> HistoryDetail | None:
        owner = self._require_owner(user_id)
        with self._connect() as connection:
            # Serialize the read/number/archive/replace sequence. Without an
            # immediate write transaction, two editors can archive the same
            # version and overwrite each other's lyrics.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT result_json
                FROM analyses
                WHERE id = ? AND user_id = ?
                """,
                (job_id, owner),
            ).fetchone()
            if row is None or not row["result_json"]:
                return None
            result = self._result(row["result_json"])
            if result is None:
                return None

            revision_number = next_revision_number(connection, job_id)
            now = datetime.now(UTC)
            revised = apply_lyrics_revision(
                result,
                lyrics,
                revision_number=revision_number,
            )
            persisted = persist_lyrics_revision(
                connection,
                analysis_id=job_id,
                user_id=owner,
                previous_result_json=row["result_json"],
                revised=revised,
                timestamp=now,
            )
            if not persisted:
                raise HistoryEntryNotFoundError(
                    f"歌词更新时历史记录消失：{job_id}"
                )
        return self.get(job_id, user_id=owner)

    def revisions(
        self,
        job_id: str,
        *,
        user_id: str,
    ) -> list[HistoryRevision]:
        owner = self._require_owner(user_id)
        with self._connect() as connection:
            return load_revisions(
                connection,
                analysis_id=job_id,
                user_id=owner,
            )

    def delete(self, job_id: str, *, user_id: str) -> bool:
        owner = self._require_owner(user_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT audio_path
                FROM analyses
                WHERE id = ? AND user_id = ?
                """,
                (job_id, owner),
            ).fetchone()
            if row is None:
                return False
            source_path = Path(row["audio_path"])
            candidates = self.assets.deletion_candidates(
                connection,
                analysis_id=job_id,
                source_path=source_path,
            )
            cursor = connection.execute(
                "DELETE FROM analyses WHERE id = ? AND user_id = ?",
                (job_id, owner),
            )
            if cursor.rowcount != 1:
                return False
            remaining_assets = self.assets.reference_rows(connection)
        # Immediate deletion candidates are source uploads, never shared cache
        # paths. Avoid hashing every remaining source file just to remove one.
        references = self.assets.snapshot(remaining_assets)
        self.assets.remove_candidates(
            candidates,
            references,
        )
        return True

    def audio_path(
        self,
        job_id: str,
        *,
        user_id: str,
    ) -> Path | None:
        owner = self._require_owner(user_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT audio_path
                FROM analyses
                WHERE id = ? AND user_id = ?
                """,
                (job_id, owner),
            ).fetchone()
        if row is None:
            return None
        path = Path(row["audio_path"])
        return path if path.exists() and path.is_file() else None

    def claim_legacy(self, user_id: str) -> int:
        """Assign only ownerless analyses from older schemas to one user."""

        owner = self._require_owner(user_id)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE analyses
                SET user_id = ?
                WHERE user_id IS NULL
                """,
                (owner,),
            )
        return cursor.rowcount

    def garbage_collect_assets(
        self,
        *,
        min_age: timedelta = timedelta(days=1),
        now: datetime | None = None,
    ) -> AssetCleanupReport:
        """Remove old workspace assets that no persisted analysis references.

        A grace period protects in-flight and temporary uploads. Content-addressed
        normalized/stem caches are retained while any source audio with the same
        digest remains, even if that cache reference has not been persisted yet.
        """

        if min_age < timedelta(0):
            raise ValueError("min_age cannot be negative")
        with self._connect() as connection:
            self.assets.refresh_result_references(connection)
            asset_rows = self.assets.reference_rows(connection)
        # Preserve content-addressed caches while any referenced source has the
        # same digest, including rows written by an older schema that did not
        # persist derived cache paths.
        references = self.assets.snapshot(
            asset_rows,
            include_cache_keys=True,
        )
        return self.assets.collect_orphans(
            references,
            min_age=min_age,
            now=now,
        )

    @classmethod
    def _summary(cls, row: sqlite3.Row) -> HistorySummary:
        return summary_from_row(row)

    @staticmethod
    def _result(payload: str | None) -> AnalysisResult | None:
        return analysis_result_from_json(payload)

    @staticmethod
    def _require_owner(user_id: str) -> str:
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id is required for history access")
        return user_id
