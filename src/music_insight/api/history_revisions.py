from __future__ import annotations

from datetime import datetime
import sqlite3

from music_insight.api.contracts.history import HistoryRevision
from music_insight.api.history_mapping import revision_from_row
from music_insight.api.migrations import result_projection
from music_insight.schemas import (
    AnalysisResult,
    Evidence,
    EvidenceType,
    LyricsSegment,
    VocalPresenceResult,
    VocalPresenceStatus,
)


def apply_lyrics_revision(
    result: AnalysisResult,
    lyrics: list[LyricsSegment],
    *,
    revision_number: int,
) -> AnalysisResult:
    """Return a revised immutable result with an explicit audit evidence item."""

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
    return result.model_copy(
        update={
            "lyrics": lyrics,
            "evidence": [*result.evidence, manual_evidence],
            **(
                {
                    "vocal_presence": VocalPresenceResult(
                        status=VocalPresenceStatus.VOCALS,
                        confidence=1.0,
                        reason="用户人工确认并保存了歌词。",
                        evidence_ids=[manual_evidence.id],
                    )
                }
                if lyrics
                else {}
            ),
        }
    )


def next_revision_number(
    connection: sqlite3.Connection,
    analysis_id: str,
) -> int:
    return (
        connection.execute(
            "SELECT COUNT(*) FROM analysis_revisions WHERE analysis_id = ?",
            (analysis_id,),
        ).fetchone()[0]
        + 1
    )


def persist_lyrics_revision(
    connection: sqlite3.Connection,
    *,
    analysis_id: str,
    user_id: str,
    previous_result_json: str,
    revised: AnalysisResult,
    timestamp: datetime,
) -> bool:
    """Persist the previous revision and its replacement in one transaction."""

    connection.execute(
        """
        INSERT INTO analysis_revisions (analysis_id, created_at, result_json)
        VALUES (?, ?, ?)
        """,
        (analysis_id, timestamp.isoformat(), previous_result_json),
    )
    projection = result_projection(revised.model_dump(mode="json"))
    cursor = connection.execute(
        """
        UPDATE analyses
        SET result_json = ?, updated_at = ?, summary_text = ?,
            duration_s = ?, lyrics_count = ?, instruments_json = ?,
            bpm = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            revised.model_dump_json(),
            timestamp.isoformat(),
            *projection,
            analysis_id,
            user_id,
        ),
    )
    return cursor.rowcount == 1


def load_revisions(
    connection: sqlite3.Connection,
    *,
    analysis_id: str,
    user_id: str,
) -> list[HistoryRevision]:
    rows = connection.execute(
        """
        SELECT
            analysis_revisions.id,
            analysis_revisions.created_at,
            analysis_revisions.result_json
        FROM analysis_revisions
        JOIN analyses
          ON analyses.id = analysis_revisions.analysis_id
        WHERE analysis_revisions.analysis_id = ?
          AND analyses.user_id = ?
        ORDER BY analysis_revisions.id DESC
        """,
        (analysis_id, user_id),
    ).fetchall()
    return [
        revision
        for row in rows
        if (revision := revision_from_row(row)) is not None
    ]
