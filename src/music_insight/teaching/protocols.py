from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from music_insight.teaching.models import (
    ListenerProfile,
    MapGenerationContext,
    MusicUnderstandingMap,
    RelistenRequest,
    RelistenResult,
    TeachingChatContext,
    TeachingChatResponse,
)


@runtime_checkable
class TeachingModelAdapter(Protocol):
    """Public, provider-neutral capability used by the teaching service."""

    async def build_understanding_map(
        self,
        context: MapGenerationContext,
    ) -> MusicUnderstandingMap: ...

    async def answer_music_question(
        self,
        context: TeachingChatContext,
    ) -> TeachingChatResponse: ...


@runtime_checkable
class TeachingRelistenProvider(Protocol):
    """Optional bounded audio capability; implementations receive <=30s ranges."""

    async def listen_to_excerpts(
        self,
        request: RelistenRequest,
    ) -> RelistenResult: ...


@runtime_checkable
class TeachingRepository(Protocol):
    """Owner-scoped persistence contract implemented by the API store adapter."""

    def get_understanding_map(
        self,
        analysis_id: str,
        *,
        user_id: str,
    ) -> dict[str, Any] | None: ...

    def mark_understanding_map_pending(
        self,
        analysis_id: str,
        *,
        user_id: str,
        schema_version: int,
        source_result_hash: str,
        force: bool = False,
        stale_before: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]: ...

    def upsert_understanding_map(
        self,
        analysis_id: str,
        *,
        user_id: str,
        schema_version: int,
        source_result_hash: str,
        map_payload: dict[str, Any],
        status: str = "complete",
        last_error: str | None = None,
        reservation_token: str | None = None,
    ) -> dict[str, Any]: ...

    def fail_understanding_map(
        self,
        analysis_id: str,
        *,
        user_id: str,
        source_result_hash: str,
        error: str,
        schema_version: int | None = None,
        reservation_token: str | None = None,
    ) -> dict[str, Any] | None: ...

    def get_listener_profile(
        self,
        *,
        user_id: str,
    ) -> dict[str, Any] | None: ...

    def upsert_listener_profile(
        self,
        *,
        user_id: str,
        level: str,
        preferences: dict[str, str],
        learned_concepts: list[str],
    ) -> dict[str, Any]: ...

    def create_conversation(
        self,
        analysis_id: str,
        *,
        user_id: str,
        title: str | None = None,
    ) -> dict[str, Any]: ...

    def list_conversations(
        self,
        analysis_id: str,
        *,
        user_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...

    def get_conversation(
        self,
        conversation_id: str,
        *,
        analysis_id: str,
        user_id: str,
    ) -> dict[str, Any] | None: ...

    def delete_conversation(
        self,
        conversation_id: str,
        *,
        analysis_id: str,
        user_id: str,
    ) -> bool: ...

    def reserve_message(
        self,
        conversation_id: str,
        *,
        analysis_id: str,
        user_id: str,
        client_request_id: str,
        request_payload: dict[str, Any],
        stale_before: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]: ...

    def complete_message(
        self,
        message_id: str,
        *,
        user_id: str,
        response_payload: dict[str, Any],
        reservation_token: str | None = None,
    ) -> dict[str, Any] | None: ...

    def fail_message(
        self,
        message_id: str,
        *,
        user_id: str,
        error: str,
        reservation_token: str | None = None,
    ) -> dict[str, Any] | None: ...

    def list_messages(
        self,
        conversation_id: str,
        *,
        analysis_id: str,
        user_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...


def profile_from_record(record: dict[str, Any] | None) -> ListenerProfile:
    if record is None:
        return ListenerProfile()
    return ListenerProfile.model_validate(
        {
            "level": record.get("level", "beginner"),
            "preferences": record.get("preferences") or {},
            "learned_concepts": record.get("learned_concepts") or [],
        }
    )
