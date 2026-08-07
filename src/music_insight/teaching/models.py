from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from music_insight.schemas import (
    AnalysisResult,
    Evidence,
    VocalPresenceResult,
)


class TeachingModel(BaseModel):
    """Strict base for persisted and model-produced teaching data."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class TeachingTimeSpan(TeachingModel):
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be greater than start_s")
        return self

    def overlaps(self, other: TeachingTimeSpan, *, tolerance: float = 0.0) -> bool:
        return (
            self.start_s <= other.end_s + tolerance
            and other.start_s <= self.end_s + tolerance
        )


class AudioDimension(StrEnum):
    MELODY = "melody"
    HARMONY = "harmony"
    RHYTHM = "rhythm"
    TIMBRE = "timbre"
    DYNAMICS = "dynamics"
    INSTRUMENTATION = "instrumentation"
    SPACE = "space"
    LYRICS = "lyrics"
    STRUCTURE = "structure"
    OTHER = "other"


class EvidenceClaimType(StrEnum):
    OBSERVED_FACT = "observed_fact"
    COMPUTED_FACT = "computed_fact"
    GROUNDED_INTERPRETATION = "grounded_interpretation"
    POSSIBLE_READING = "possible_reading"


class EvidenceSourceType(StrEnum):
    ANALYSIS_EVIDENCE = "analysis_evidence"
    LYRICS = "lyrics"
    METRIC = "metric"
    UNDERSTANDING_EVENT = "understanding_event"
    RELISTEN = "relisten"


class AnalysisEvidenceRef(TeachingModel):
    """Stable pointer into an analysis result or a bounded relisten result."""

    source_type: EvidenceSourceType
    source_id: str = Field(min_length=1, max_length=160)
    dimension: AudioDimension
    statement: str = Field(min_length=1, max_length=4000)
    claim_type: EvidenceClaimType
    span: TeachingTimeSpan | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class LyricsContext(TeachingModel):
    source_id: str = Field(pattern=r"^lyrics:\d+$", max_length=40)
    text: str = Field(min_length=1, max_length=4000)
    span: TeachingTimeSpan | None = None
    language: str | None = Field(default=None, max_length=32)
    confidence: float | None = Field(default=None, ge=0, le=1)


class SectionMarker(TeachingModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    label: str = Field(min_length=1, max_length=80)
    span: TeachingTimeSpan
    expressive_role: str = Field(min_length=1, max_length=800)
    confidence: float = Field(ge=0, le=1)
    alternative_labels: list[str] = Field(default_factory=list, max_length=4)


class EmotionalArcPoint(TeachingModel):
    span: TeachingTimeSpan
    description: str = Field(min_length=1, max_length=600)
    evidence_refs: list[AnalysisEvidenceRef] = Field(
        min_length=1,
        max_length=12,
    )
    confidence: float = Field(ge=0, le=1)


class UnderstandingEvent(TeachingModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    section: str = Field(min_length=1, max_length=80)
    observation: str = Field(min_length=1, max_length=1200)
    interpretation: str = Field(min_length=1, max_length=1200)
    expressive_role: str = Field(min_length=1, max_length=1200)
    audio_evidence: list[AnalysisEvidenceRef] = Field(
        min_length=1,
        max_length=16,
    )
    lyrics_context: list[LyricsContext] = Field(
        default_factory=list,
        max_length=12,
    )
    listening_task: str = Field(min_length=1, max_length=800)
    alternative_readings: list[str] = Field(default_factory=list, max_length=5)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be greater than start_s")
        return self

    @property
    def span(self) -> TeachingTimeSpan:
        return TeachingTimeSpan(start_s=self.start_s, end_s=self.end_s)


class KeyMoment(TeachingModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    event_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    reason: str = Field(min_length=1, max_length=800)
    listening_task: str = Field(min_length=1, max_length=800)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be greater than start_s")
        return self


class MusicUnderstandingMap(TeachingModel):
    schema_version: int = Field(default=3, ge=1, le=3)
    output_language: Literal["zh", "en"] = "zh"
    core_expression: str = Field(min_length=1, max_length=1000)
    overall_atmosphere: str = Field(min_length=1, max_length=1600)
    emotional_arc: list[EmotionalArcPoint] = Field(
        default_factory=list,
        max_length=40,
    )
    sections: list[SectionMarker] = Field(default_factory=list, max_length=40)
    events: list[UnderstandingEvent] = Field(default_factory=list, max_length=160)
    key_moments: list[KeyMoment] = Field(default_factory=list, max_length=5)
    confidence: float = Field(ge=0, le=1)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("warnings")
    @classmethod
    def validate_warnings(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or len(value) > 800:
                raise ValueError("warnings must contain non-empty bounded strings")
        return values

    @model_validator(mode="after")
    def validate_identity_and_order(self) -> Self:
        self._unique_ids(self.sections, "sections")
        self._unique_ids(self.events, "events")
        self._unique_ids(self.key_moments, "key_moments")
        self._ordered(self.sections, lambda value: value.span.start_s, "sections")
        self._ordered(self.events, lambda value: value.start_s, "events")
        self._ordered(
            self.emotional_arc,
            lambda value: value.span.start_s,
            "emotional_arc",
        )
        self._ordered(
            self.key_moments,
            lambda value: value.start_s,
            "key_moments",
        )
        event_ids = {event.id for event in self.events}
        unknown = [
            moment.event_id
            for moment in self.key_moments
            if moment.event_id not in event_ids
        ]
        if unknown:
            raise ValueError(f"key moments reference unknown events: {unknown[:3]}")
        for previous, current in pairwise(self.sections):
            if current.span.start_s < previous.span.end_s:
                raise ValueError("sections must not overlap")
        return self

    @staticmethod
    def _unique_ids(values: list[Any], field_name: str) -> None:
        identifiers = [value.id for value in values]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError(f"{field_name} must use unique ids")

    @staticmethod
    def _ordered(values: list[Any], key, field_name: str) -> None:
        starts = [key(value) for value in values]
        if starts != sorted(starts):
            raise ValueError(f"{field_name} must be ordered by start time")


class ListenerLevel(StrEnum):
    BEGINNER = "beginner"
    CURIOUS = "curious"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class ListenerProfile(TeachingModel):
    level: ListenerLevel = ListenerLevel.BEGINNER
    preferences: dict[str, str] = Field(default_factory=dict)
    learned_concepts: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("preferences")
    @classmethod
    def validate_preferences(cls, values: dict[str, str]) -> dict[str, str]:
        if len(values) > 20:
            raise ValueError("preferences cannot contain more than 20 entries")
        for key, value in values.items():
            if not key.strip() or len(key) > 80 or len(value) > 400:
                raise ValueError("preferences keys or values exceed their limits")
        return values

    @field_validator("learned_concepts")
    @classmethod
    def validate_concepts(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            stripped = value.strip()
            if not stripped or len(stripped) > 120:
                raise ValueError("learned concepts must be non-empty and bounded")
            if stripped not in normalized:
                normalized.append(stripped)
        return normalized


class RelistenPolicy(StrEnum):
    NEVER = "never"
    AUTO = "auto"
    ALWAYS = "always"


class ConversationTurn(TeachingModel):
    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(min_length=1, max_length=12000)
    created_at: datetime


class RelistenEvidence(TeachingModel):
    id: str = Field(pattern=r"^relisten:[A-Za-z0-9_.:-]{1,120}$")
    dimension: AudioDimension
    observation: str = Field(min_length=1, max_length=1000)
    span: TeachingTimeSpan
    confidence: float = Field(ge=0, le=1)


class RelistenRequest(TeachingModel):
    analysis_id: str = Field(min_length=1, max_length=160)
    audio_path: Path
    question: str = Field(min_length=1, max_length=4000)
    ranges: list[TeachingTimeSpan] = Field(min_length=1, max_length=2)
    language: str | None = Field(default=None, max_length=32)
    output_language: Literal["zh", "en"] = "zh"

    @field_validator("ranges")
    @classmethod
    def validate_clip_lengths(
        cls,
        values: list[TeachingTimeSpan],
    ) -> list[TeachingTimeSpan]:
        if any(span.end_s - span.start_s > 30.0 for span in values):
            raise ValueError("each relisten excerpt must not exceed 30 seconds")
        return values


class RelistenResult(TeachingModel):
    evidence: list[RelistenEvidence] = Field(default_factory=list, max_length=20)
    warnings: list[str] = Field(default_factory=list, max_length=10)


class TeachingChatContext(TeachingModel):
    analysis_id: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=1, max_length=4000)
    current_time_s: float = Field(ge=0)
    selected_range: TeachingTimeSpan | None = None
    compare_ranges: list[TeachingTimeSpan] = Field(
        default_factory=list,
        max_length=2,
    )
    current_section: SectionMarker | None = None
    nearby_lyrics: list[LyricsContext] = Field(default_factory=list, max_length=20)
    nearby_events: list[UnderstandingEvent] = Field(
        default_factory=list,
        max_length=12,
    )
    nearby_analysis_evidence: list[Evidence] = Field(
        default_factory=list,
        max_length=30,
    )
    relisten_evidence: list[RelistenEvidence] = Field(
        default_factory=list,
        max_length=20,
    )
    conversation_history: list[ConversationTurn] = Field(
        default_factory=list,
        max_length=12,
    )
    listener_profile: ListenerProfile
    analysis_summary: str = Field(min_length=1, max_length=4000)
    vocal_presence: VocalPresenceResult = Field(
        default_factory=VocalPresenceResult
    )
    duration_s: float = Field(gt=0)
    output_language: Literal["zh", "en"] = "zh"


class AnswerTimeRange(TeachingModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    label: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be greater than start_s")
        return self

    @property
    def span(self) -> TeachingTimeSpan:
        return TeachingTimeSpan(start_s=self.start_s, end_s=self.end_s)


class AnswerEvidence(TeachingModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    statement: str = Field(min_length=1, max_length=1000)
    claim_type: EvidenceClaimType
    dimension: AudioDimension
    source_refs: list[str] = Field(min_length=1, max_length=12)
    time_range_ids: list[str] = Field(min_length=1, max_length=6)
    confidence: float = Field(ge=0, le=1)


class ListeningTask(TeachingModel):
    instruction: str = Field(min_length=1, max_length=800)
    focus: AudioDimension
    time_range_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,80}$")


class PlayerActionType(StrEnum):
    SEEK = "seek"
    PLAY_RANGE = "play_range"
    LOOP_RANGE = "loop_range"
    COMPARE_AB = "compare_ab"


class PlayerAction(TeachingModel):
    type: PlayerActionType
    time_range_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    comparison_time_range_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.:-]{1,80}$",
    )
    label: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_comparison(self) -> Self:
        if self.type == PlayerActionType.COMPARE_AB:
            if self.comparison_time_range_id is None:
                raise ValueError("compare_ab requires comparison_time_range_id")
            if self.comparison_time_range_id == self.time_range_id:
                raise ValueError("compare_ab ranges must be different")
        elif self.comparison_time_range_id is not None:
            raise ValueError(
                "comparison_time_range_id is only valid for compare_ab"
            )
        return self


class TeachingChatResponse(TeachingModel):
    output_language: Literal["zh", "en"] = "zh"
    answer: str = Field(min_length=1, max_length=12000)
    time_ranges: list[AnswerTimeRange] = Field(min_length=1, max_length=8)
    evidence: list[AnswerEvidence] = Field(default_factory=list, max_length=20)
    listening_task: ListeningTask
    suggested_questions: list[str] = Field(default_factory=list, max_length=5)
    player_actions: list[PlayerAction] = Field(default_factory=list, max_length=8)
    alternative_readings: list[str] = Field(default_factory=list, max_length=5)
    warnings: list[str] = Field(default_factory=list, max_length=10)
    confidence: float = Field(ge=0, le=1)
    relistened: bool = False
    insufficient_evidence: bool = False

    @field_validator("suggested_questions", "alternative_readings", "warnings")
    @classmethod
    def validate_bounded_strings(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or len(value) > 600:
                raise ValueError("list items must be non-empty and bounded")
        return values

    @model_validator(mode="after")
    def validate_internal_references(self) -> Self:
        if self.insufficient_evidence:
            if self.confidence > 0.4:
                raise ValueError(
                    "an insufficient-evidence answer must cap confidence at 0.4"
                )
        elif not self.evidence:
            raise ValueError(
                "a normal teaching answer requires at least one evidence item"
            )
        range_ids = [item.id for item in self.time_ranges]
        if len(set(range_ids)) != len(range_ids):
            raise ValueError("time_ranges must use unique ids")
        known_ranges = set(range_ids)
        if self.listening_task.time_range_id not in known_ranges:
            raise ValueError("listening_task references an unknown time range")
        for evidence in self.evidence:
            if not set(evidence.time_range_ids) <= known_ranges:
                raise ValueError("evidence references an unknown time range")
        for action in self.player_actions:
            if action.time_range_id not in known_ranges:
                raise ValueError("player action references an unknown time range")
            if (
                action.comparison_time_range_id is not None
                and action.comparison_time_range_id not in known_ranges
            ):
                raise ValueError(
                    "player comparison references an unknown time range"
                )
        return self


class MapGenerationContext(TeachingModel):
    analysis_id: str = Field(min_length=1, max_length=160)
    result: AnalysisResult
    duration_s: float = Field(gt=0)
    language: str | None = Field(default=None, max_length=32)
    output_language: Literal["zh", "en"] = "zh"
    listener_profile: ListenerProfile


_CJK_RE = re.compile(r"[㐀-鿿]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def chat_focus_spans(context: TeachingChatContext) -> list[TeachingTimeSpan]:
    """Return the time range(s) a chat turn is anchored to.

    A/B comparison uses both ranges; a manual selection uses just that
    selection; otherwise a 15-second window around the current position. This
    is shared by request assembly and response parsing so both layers agree on
    what "nearby" means.
    """

    if context.compare_ranges:
        return context.compare_ranges
    if context.selected_range is not None:
        return [context.selected_range]
    start_s = max(0.0, context.current_time_s - 7.5)
    end_s = min(context.duration_s, start_s + 15.0)
    start_s = max(0.0, end_s - 15.0)
    return [TeachingTimeSpan(start_s=start_s, end_s=end_s)]


def normalize_question(value: str) -> str:
    """Normalize a question for deduplication (compare asked vs suggested).

    Shared by the fallback teacher and the chat suggestion filter so both
    layers use the same definition of "the same question".
    """

    return "".join(character for character in value.casefold() if character.isalnum())


def script_counts(text: str) -> tuple[int, int]:
    """Return (cjk, latin) character counts for language heuristics."""

    return (
        len(_CJK_RE.findall(text)),
        len(_LATIN_RE.findall(text)),
    )


def localized_text(language: str, english: str, chinese: str) -> str:
    """Pick the prose variant matching the requested output language.

    Shared by the fallback teacher, the teaching record mappers, and the wire
    parsing layer so the zh/en contract lives in one place.
    """

    return english if language == "en" else chinese

