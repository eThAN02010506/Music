from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from music_insight.teaching.models import (
    ListenerLevel,
    ListenerProfile,
    MusicUnderstandingMap,
    RelistenPolicy,
    TeachingChatResponse,
    TeachingModel,
    TeachingTimeSpan,
)


class TeachingGuideStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    STALE = "stale"
    FAILED = "failed"


class TeachingGuideGenerateRequest(TeachingModel):
    force: bool = False


class TeachingGuideResponse(TeachingModel):
    analysis_id: str = Field(min_length=1, max_length=160)
    schema_version: int = Field(ge=1)
    source_result_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: TeachingGuideStatus
    understanding_map: MusicUnderstandingMap | None = None
    stale: bool = False
    cached: bool = False
    error: str | None = Field(default=None, max_length=1000)
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_complete_map(self) -> Self:
        if (
            self.status == TeachingGuideStatus.COMPLETE
            and self.understanding_map is None
        ):
            raise ValueError("a complete teaching guide requires a map")
        return self


class ListenerProfileUpdate(TeachingModel):
    level: ListenerLevel
    preferences: dict[str, str] = Field(default_factory=dict)
    learned_concepts: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_as_profile(self) -> Self:
        ListenerProfile.model_validate(self.model_dump())
        return self


class ConversationCreateRequest(TeachingModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)


class TeachingConversation(TeachingModel):
    id: str = Field(min_length=1, max_length=160)
    analysis_id: str = Field(min_length=1, max_length=160)
    title: str | None = Field(default=None, max_length=120)
    summary: str | None = Field(default=None, max_length=2000)
    message_count: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime


class TeachingChatRequest(TeachingModel):
    client_request_id: str = Field(
        pattern=r"^[A-Za-z0-9_.:-]{8,128}$",
    )
    message: str = Field(min_length=1, max_length=4000)
    current_time_s: float = Field(ge=0)
    selected_range: TeachingTimeSpan | None = None
    compare_ranges: list[TeachingTimeSpan] = Field(
        default_factory=list,
        max_length=2,
    )
    relisten_policy: RelistenPolicy = RelistenPolicy.AUTO

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if self.compare_ranges and len(self.compare_ranges) != 2:
            raise ValueError("compare_ranges must contain exactly two ranges")
        if self.compare_ranges and self.selected_range is not None:
            raise ValueError(
                "selected_range and compare_ranges cannot be used together"
            )
        ranges = [*self.compare_ranges]
        if self.selected_range is not None:
            ranges.append(self.selected_range)
        if any(span.end_s - span.start_s > 120 for span in ranges):
            raise ValueError("a selected or comparison range cannot exceed 120 seconds")
        return self


class TeachingMessageStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    FAILED = "failed"


class TeachingMessage(TeachingModel):
    id: str = Field(min_length=1, max_length=160)
    conversation_id: str = Field(min_length=1, max_length=160)
    sequence: int = Field(ge=1)
    status: TeachingMessageStatus
    client_request_id: str = Field(min_length=8, max_length=128)
    request: TeachingChatRequest
    response: TeachingChatResponse | None = None
    error: str | None = Field(default=None, max_length=1000)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_terminal_payload(self) -> Self:
        if self.status == TeachingMessageStatus.COMPLETE and self.response is None:
            raise ValueError("a complete message requires a response")
        return self
