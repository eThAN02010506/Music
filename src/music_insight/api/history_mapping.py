from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3

from music_insight.api.contracts.history import (
    HistoryDetail,
    HistoryRevision,
    HistorySummary,
)
from music_insight.schemas import AnalysisResult


def analysis_result_from_json(payload: str | None) -> AnalysisResult | None:
    if not payload:
        return None
    return AnalysisResult.model_validate_json(payload)


def summary_from_row(row: sqlite3.Row) -> HistorySummary:
    try:
        instruments = json.loads(row["instruments_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        instruments = []
    if not isinstance(instruments, list):
        instruments = []
    return HistorySummary(
        id=row["id"],
        title=row["title"],
        file_name=row["file_name"],
        language=row["language"],
        state=row["state"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        error=row["error"],
        summary=row["summary_text"],
        duration_s=row["duration_s"],
        lyrics_count=row["lyrics_count"],
        instruments=[str(item) for item in instruments[:8]],
        bpm=row["bpm"],
        model_source=row["model_source"],
        model_location=row["model_location"],
    )


def detail_from_row(
    row: sqlite3.Row,
    *,
    result: AnalysisResult | None,
    revision_count: int,
) -> HistoryDetail:
    summary = summary_from_row(row)
    audio_path = Path(row["audio_path"])
    return HistoryDetail(
        **summary.model_dump(),
        result=result,
        audio_url=(
            f"/history/{row['id']}/audio"
            if audio_path.exists() and audio_path.is_file()
            else None
        ),
        revision_count=revision_count,
    )


def revision_from_row(row: sqlite3.Row) -> HistoryRevision | None:
    result = analysis_result_from_json(row["result_json"])
    if result is None:
        return None
    return HistoryRevision(
        id=row["id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        lyrics=result.lyrics,
    )
