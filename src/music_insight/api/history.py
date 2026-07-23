from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3

from pydantic import BaseModel, Field

from music_insight.schemas import (
    AnalysisResult,
    Evidence,
    EvidenceType,
    LyricsSegment,
)


class HistorySummary(BaseModel):
    id: str
    title: str
    file_name: str
    language: str | None = None
    state: str
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    summary: str | None = None
    duration_s: float | None = None
    lyrics_count: int = 0
    instruments: list[str] = Field(default_factory=list)
    bpm: float | None = None
    model_source: str = "network"
    model_location: str | None = None


class HistoryDetail(HistorySummary):
    result: AnalysisResult | None = None
    audio_url: str | None = None
    revision_count: int = 0


class HistoryRename(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class HistoryLyricsUpdate(BaseModel):
    lyrics: list[LyricsSegment] = Field(max_length=500)


class HistoryRevision(BaseModel):
    id: int
    created_at: datetime
    lyrics: list[LyricsSegment]


class HistoryLyricsRetryRequest(BaseModel):
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)


class HistoryLyricsRetryResult(BaseModel):
    start_s: float
    end_s: float
    lyrics: list[LyricsSegment]
    issues: list[str] = Field(default_factory=list)
    source: str


class HistoryStore:
    """SQLite-backed local analysis history with cached result payloads."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.workspace_dir = database_path.parent.resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS analyses (
                    id TEXT PRIMARY KEY,
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
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(analyses)").fetchall()
            }
            if "model_source" not in columns:
                connection.execute(
                    "ALTER TABLE analyses ADD COLUMN model_source TEXT NOT NULL DEFAULT 'network'"
                )
            if "model_location" not in columns:
                connection.execute(
                    "ALTER TABLE analyses ADD COLUMN model_location TEXT"
                )
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """
                UPDATE analyses
                SET state = 'failed', updated_at = ?,
                    error = COALESCE(error, '服务重启，未完成的任务已中断。')
                WHERE state IN ('queued', 'running')
                """,
                (now,),
            )

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
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO analyses (
                    id, title, file_name, language, state, created_at,
                    updated_at, audio_path, model_source, model_location
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
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

    def update(
        self,
        job_id: str,
        *,
        state: str,
        updated_at: datetime,
        result: AnalysisResult | None = None,
        error: str | None = None,
    ) -> None:
        result_json = result.model_dump_json() if result is not None else None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE analyses
                SET state = ?, updated_at = ?,
                    result_json = COALESCE(?, result_json), error = ?
                WHERE id = ?
                """,
                (state, updated_at.isoformat(), result_json, error, job_id),
            )

    def list(self, limit: int = 100) -> list[HistorySummary]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM analyses ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [self._summary(row) for row in rows]

    def get(self, job_id: str) -> HistoryDetail | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM analyses WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        summary = self._summary(row)
        result = self._result(row["result_json"])
        with self._connect() as connection:
            revision_count = connection.execute(
                "SELECT COUNT(*) FROM analysis_revisions WHERE analysis_id = ?",
                (job_id,),
            ).fetchone()[0]
        return HistoryDetail(
            **summary.model_dump(),
            result=result,
            audio_url=(f"/history/{job_id}/audio" if self.audio_path(job_id) else None),
            revision_count=revision_count,
        )

    def rename(self, job_id: str, title: str) -> HistoryDetail | None:
        cleaned = " ".join(title.split()).strip()[:120]
        if not cleaned:
            return None
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE analyses SET title = ?, updated_at = ? WHERE id = ?",
                (cleaned, datetime.now(UTC).isoformat(), job_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get(job_id)

    def update_lyrics(
        self, job_id: str, lyrics: list[LyricsSegment]
    ) -> HistoryDetail | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM analyses WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None or not row["result_json"]:
                return None
            result = self._result(row["result_json"])
            if result is None:
                return None

            revision_number = (
                connection.execute(
                    "SELECT COUNT(*) FROM analysis_revisions WHERE analysis_id = ?",
                    (job_id,),
                ).fetchone()[0]
                + 1
            )
            now = datetime.now(UTC)
            connection.execute(
                """
                INSERT INTO analysis_revisions (analysis_id, created_at, result_json)
                VALUES (?, ?, ?)
                """,
                (job_id, now.isoformat(), row["result_json"]),
            )
            manual_evidence = Evidence(
                id=f"manual.lyrics.revision.{revision_number}",
                source="用户校对",
                kind=EvidenceType.OBSERVED,
                text="用户在前端人工修订了歌词及时间轴。",
                confidence=1.0,
                metadata={
                    "revision": revision_number,
                    "segment_count": len(lyrics),
                },
            )
            revised = result.model_copy(
                update={
                    "lyrics": lyrics,
                    "evidence": [*result.evidence, manual_evidence],
                }
            )
            connection.execute(
                """
                UPDATE analyses
                SET result_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (revised.model_dump_json(), now.isoformat(), job_id),
            )
        return self.get(job_id)

    def revisions(self, job_id: str) -> list[HistoryRevision]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, result_json
                FROM analysis_revisions
                WHERE analysis_id = ?
                ORDER BY id DESC
                """,
                (job_id,),
            ).fetchall()
        revisions = []
        for row in rows:
            result = self._result(row["result_json"])
            if result is None:
                continue
            revisions.append(
                HistoryRevision(
                    id=row["id"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    lyrics=result.lyrics,
                )
            )
        return revisions

    def delete(self, job_id: str) -> bool:
        path = self.audio_path(job_id)
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM analysis_revisions WHERE analysis_id = ?", (job_id,)
            )
            cursor = connection.execute(
                "DELETE FROM analyses WHERE id = ?", (job_id,)
            )
        if cursor.rowcount == 0:
            return False
        if path is not None:
            try:
                resolved = path.resolve()
                if resolved.is_relative_to(self.workspace_dir):
                    resolved.unlink(missing_ok=True)
            except (OSError, RuntimeError):
                pass
        return True

    def audio_path(self, job_id: str) -> Path | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT audio_path FROM analyses WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        path = Path(row["audio_path"])
        return path if path.exists() and path.is_file() else None

    @classmethod
    def _summary(cls, row: sqlite3.Row) -> HistorySummary:
        result = cls._result(row["result_json"])
        duration_s = None
        summary = None
        lyrics_count = 0
        instruments: list[str] = []
        bpm = None
        if result is not None:
            summary = result.summary
            lyrics_count = len(result.lyrics)
            instruments = result.instruments[:8]
            bpm = result.technical_metrics.bpm
            for evidence in result.technical_metrics.evidence:
                raw_duration = evidence.metadata.get("duration_s")
                if raw_duration is not None:
                    duration_s = float(raw_duration)
                    break
        return HistorySummary(
            id=row["id"],
            title=row["title"],
            file_name=row["file_name"],
            language=row["language"],
            state=row["state"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            error=row["error"],
            summary=summary,
            duration_s=duration_s,
            lyrics_count=lyrics_count,
            instruments=instruments,
            bpm=bpm,
            model_source=row["model_source"],
            model_location=row["model_location"],
        )

    @staticmethod
    def _result(payload: str | None) -> AnalysisResult | None:
        if not payload:
            return None
        return AnalysisResult.model_validate(json.loads(payload))
