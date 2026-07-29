from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from music_insight.schemas import AnalysisResult, LyricsSegment


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


class HistoryWaveform(BaseModel):
    duration_s: float = Field(gt=0)
    peaks: list[list[float]] = Field(min_length=1, max_length=2)
    points_per_channel: int = Field(ge=2, le=8_000)
