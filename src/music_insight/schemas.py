from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator


class EvidenceType(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    INTERPRETIVE = "interpretive"
    COMPUTED = "computed"


class VocalPresenceStatus(StrEnum):
    VOCALS = "vocals"
    INSTRUMENTAL = "instrumental"
    UNKNOWN = "unknown"


class VocalPresenceResult(BaseModel):
    status: VocalPresenceStatus = VocalPresenceStatus.UNKNOWN
    confidence: float | None = Field(default=None, ge=0, le=1)
    reason: str = Field(
        default="现有证据不足以确认是否包含人声。",
        min_length=1,
        max_length=600,
    )
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)


class TimeSpan(BaseModel):
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.end_s < self.start_s:
            raise ValueError("end_s must be greater than or equal to start_s")
        return self


class Evidence(BaseModel):
    id: str
    source: str
    kind: EvidenceType
    text: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    span: TimeSpan | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AudioAsset(BaseModel):
    path: Path
    media_type: str
    size_bytes: int
    language_hint: str | None = None
    max_duration_s: float | None = Field(default=None, gt=0)


class LyricsSegment(BaseModel):
    text: str
    span: TimeSpan | None = None
    language: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class AsrResult(BaseModel):
    model: str
    lyrics: list[LyricsSegment]
    evidence: list[Evidence]


class AsrVerificationResult(BaseModel):
    """Timestamped transcript returned by an optional secondary ASR service."""

    model: str
    segments: list[LyricsSegment] = Field(default_factory=list)
    segments_received: int = Field(default=0, ge=0)
    segments_invalid: int = Field(default=0, ge=0)
    duration_s: float | None = Field(default=None, ge=0)
    vocals_detected: bool | None = None
    # Confidence in ``vocals_detected`` (not an unconditional vocal score).
    vocal_confidence: float | None = Field(default=None, ge=0, le=1)
    transcript_confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_segment_accounting(self) -> Self:
        if self.segments_received == 0 and self.segments:
            self.segments_received = len(self.segments) + self.segments_invalid
        if self.segments_received < len(self.segments) + self.segments_invalid:
            raise ValueError(
                "segments_received cannot be smaller than parsed plus invalid segments"
            )
        return self


class AudioSceneResult(BaseModel):
    model: str
    lyrics: list[LyricsSegment] = Field(default_factory=list)
    instruments: list[str] = Field(default_factory=list)
    sound_events: list[Evidence] = Field(default_factory=list)
    emotion_timeline: list[Evidence] = Field(default_factory=list)
    inferred_atmosphere: list[Evidence] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    narrative: str | None = None
    vocals_detected: bool | None = None
    vocal_confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list)


class LiteraryResult(BaseModel):
    model: str
    themes: list[str] = Field(default_factory=list)
    narrative: str
    evidence: list[Evidence] = Field(default_factory=list)


class DspResult(BaseModel):
    bpm: float | None = None
    bpm_confidence: float | None = Field(default=None, ge=0, le=1)
    bpm_candidates: list[float] = Field(default_factory=list)
    bpm_ambiguous: bool = False
    key: str | None = None
    key_confidence: float | None = Field(default=None, ge=0, le=1)
    energy_curve: list[Evidence] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    title: str | None = None
    summary: str
    lyrics: list[LyricsSegment]
    instruments: list[str]
    sound_events: list[Evidence]
    emotion_timeline: list[Evidence]
    inferred_atmosphere: list[Evidence] = Field(default_factory=list)
    themes: list[str]
    technical_metrics: DspResult
    evidence: list[Evidence]
    vocal_presence: VocalPresenceResult = Field(
        default_factory=VocalPresenceResult
    )
    warnings: list[str] = Field(default_factory=list)


class AnalysisJob(BaseModel):
    id: str
    asset: AudioAsset


class UnifiedAudioResult(BaseModel):
    asr: AsrResult
    scene: AudioSceneResult
    literary: LiteraryResult


class VerifiedLyricsSynthesisResult(BaseModel):
    """Report fields regenerated after a secondary ASR changes the lyrics."""

    literary: LiteraryResult
    inferred_atmosphere: list[Evidence] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
