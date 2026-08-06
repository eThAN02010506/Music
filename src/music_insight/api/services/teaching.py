from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from music_insight.api.contracts.teaching import (
    ListenerProfileUpdate,
    TeachingChatRequest,
    TeachingConversation,
    TeachingGuideResponse,
    TeachingGuideStatus,
    TeachingMessage,
    TeachingMessageStatus,
)
from music_insight.api.contracts.history import HistoryDetail
from music_insight.api.history import HistoryStore
from music_insight.api.services.history import require_history
from music_insight.api.services.teaching_chat import (
    answer_chat_context as _answer_chat_context,
    should_relisten as _should_relisten,
)
from music_insight.api.services.teaching_records import (
    TEACHING_SCHEMA_VERSION,
    bounded_clip as _bounded_clip,
    bounded_error as _bounded_error,
    conversation_from_record as _conversation_from_record,
    duration_for_entry as _duration_for_entry,
    guide_from_record as _guide_from_record,
    localized as _localized,
    message_from_record as _message_from_record,
    turns_from_records as _turns_from_records,
    validate_request_duration as _validate_request_duration,
)
from music_insight.api.teaching import (
    TeachingConflictError,
    TeachingEntryNotFoundError,
)
from music_insight.teaching.fallback import EvidenceTeachingModel
from music_insight.teaching.grounding import (
    analysis_result_hash,
    validate_chat_response,
    validate_understanding_map,
)
from music_insight.teaching.models import (
    ListenerProfile,
    MapGenerationContext,
    MusicUnderstandingMap,
    RelistenRequest,
    TeachingChatContext,
    TeachingTimeSpan,
)
from music_insight.teaching.protocols import (
    TeachingModelAdapter,
    TeachingRelistenProvider,
    TeachingRepository,
    profile_from_record,
)
from music_insight.teaching.retrieval import (
    focus_span,
    nearby_analysis_evidence,
    nearby_events,
    nearby_lyrics,
    section_at_time,
)
from music_insight.schemas import AnalysisResult


TEACHING_PENDING_LEASE = timedelta(minutes=30)


async def _settle_despite_cancellation(task: asyncio.Task):
    """Wait for an already-running thread operation after caller cancellation."""

    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def get_teaching_guide(
    *,
    history: HistoryStore,
    repository: TeachingRepository,
    history_id: str,
    user_id: str,
) -> TeachingGuideResponse:
    entry = await run_in_threadpool(require_history, history, history_id, user_id)
    if entry.result is None:
        raise HTTPException(status_code=409, detail="歌曲分析尚未完成。")
    source_hash = analysis_result_hash(entry.result)
    record = await run_in_threadpool(
        repository.get_understanding_map,
        history_id,
        user_id=user_id,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="尚未生成音乐导赏地图。")
    return _guide_from_record(
        record,
        current_source_hash=source_hash,
        cached=True,
    )


async def generate_teaching_guide(
    *,
    history: HistoryStore,
    repository: TeachingRepository,
    history_id: str,
    user_id: str,
    force: bool = False,
    output_language: str = "zh",
    model: TeachingModelAdapter | None = None,
) -> TeachingGuideResponse:
    entry = await run_in_threadpool(require_history, history, history_id, user_id)
    if entry.result is None:
        raise HTTPException(status_code=409, detail="歌曲分析尚未完成。")
    duration_s = _duration_for_entry(entry.duration_s, entry.result)
    source_hash = analysis_result_hash(entry.result)
    reserve_options: dict[str, Any] = {
        "user_id": user_id,
        "schema_version": TEACHING_SCHEMA_VERSION,
        "source_result_hash": source_hash,
        "stale_before": datetime.now(UTC) - TEACHING_PENDING_LEASE,
    }
    existing_record = await run_in_threadpool(
        repository.get_understanding_map,
        history_id,
        user_id=user_id,
    )
    existing_payload = (
        existing_record.get("map_payload")
        if existing_record is not None
        else None
    )
    language_changed = (
        isinstance(existing_payload, Mapping)
        and existing_payload.get("output_language") != output_language
    )
    if force or language_changed:
        reserve_options["force"] = True
    record, cached_response = await _reserve_guide_generation(
        repository,
        history_id=history_id,
        user_id=user_id,
        source_hash=source_hash,
        reserve_options=reserve_options,
    )
    if cached_response is not None:
        return cached_response

    reservation_token = str(record["updated_at"])
    try:
        profile_record = await run_in_threadpool(
            repository.get_listener_profile,
            user_id=user_id,
        )
        context = MapGenerationContext(
            analysis_id=history_id,
            result=entry.result,
            duration_s=duration_s,
            language=entry.language,
            output_language=output_language,
            listener_profile=profile_from_record(profile_record),
        )
        understanding_map, generation_warning = await _build_understanding_map(
            context,
            model=model,
            result=entry.result,
            duration_s=duration_s,
        )
        record = await run_in_threadpool(
            repository.upsert_understanding_map,
            history_id,
            user_id=user_id,
            schema_version=TEACHING_SCHEMA_VERSION,
            source_result_hash=source_hash,
            map_payload=understanding_map.model_dump(mode="json"),
            status="complete",
            last_error=generation_warning,
            reservation_token=reservation_token,
        )
    except asyncio.CancelledError:
        await asyncio.shield(
            _fail_map_reservation(
                repository,
                history_id=history_id,
                user_id=user_id,
                source_hash=source_hash,
                schema_version=TEACHING_SCHEMA_VERSION,
                reservation_token=reservation_token,
                error="导赏地图生成请求已取消。",
            )
        )
        raise
    except TeachingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        await _fail_map_reservation(
            repository,
            history_id=history_id,
            user_id=user_id,
            source_hash=source_hash,
            schema_version=TEACHING_SCHEMA_VERSION,
            reservation_token=reservation_token,
            error=_bounded_error(exc, 1000),
        )
        raise HTTPException(
            status_code=502,
            detail=f"无法生成音乐导赏地图：{_bounded_error(exc, 500)}",
        ) from exc
    return _guide_from_record(
        record,
        current_source_hash=source_hash,
        cached=False,
    )


async def _reserve_guide_generation(
    repository: TeachingRepository,
    *,
    history_id: str,
    user_id: str,
    source_hash: str,
    reserve_options: dict[str, Any],
) -> tuple[dict[str, Any], TeachingGuideResponse | None]:
    reservation_task = asyncio.create_task(
        run_in_threadpool(
            repository.mark_understanding_map_pending,
            history_id,
            **reserve_options,
        )
    )
    try:
        record, should_generate = await asyncio.shield(reservation_task)
    except asyncio.CancelledError:
        await _compensate_cancelled_guide_reservation(
            reservation_task,
            repository=repository,
            history_id=history_id,
            user_id=user_id,
            source_hash=source_hash,
        )
        raise
    except TeachingEntryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if should_generate:
        return record, None
    if (
        record.get("status") == TeachingGuideStatus.COMPLETE
        and record.get("map_payload") is not None
        and record.get("source_result_hash") == source_hash
    ):
        return (
            record,
            _guide_from_record(
                record,
                current_source_hash=source_hash,
                cached=True,
            ),
        )
    raise HTTPException(
        status_code=409,
        detail="相同版本的导赏地图正在生成，请稍后重试。",
        headers={"Retry-After": "2"},
    )


async def _compensate_cancelled_guide_reservation(
    reservation_task: asyncio.Task,
    *,
    repository: TeachingRepository,
    history_id: str,
    user_id: str,
    source_hash: str,
) -> None:
    try:
        cancelled_record, cancelled_owner = (
            await _settle_despite_cancellation(reservation_task)
        )
    except Exception:
        return
    if cancelled_owner:
        await asyncio.shield(
            _fail_map_reservation(
                repository,
                history_id=history_id,
                user_id=user_id,
                source_hash=source_hash,
                schema_version=TEACHING_SCHEMA_VERSION,
                reservation_token=str(cancelled_record["updated_at"]),
                error="导赏地图生成请求已取消。",
            )
        )


async def _build_understanding_map(
    context: MapGenerationContext,
    *,
    model: TeachingModelAdapter | None,
    result: AnalysisResult,
    duration_s: float,
) -> tuple[MusicUnderstandingMap, str | None]:
    fallback = EvidenceTeachingModel()
    generation_warning: str | None = None
    if model is None:
        understanding_map = await fallback.build_understanding_map(context)
    else:
        try:
            understanding_map = await model.build_understanding_map(context)
            validate_understanding_map(
                understanding_map,
                result=result,
                duration_s=duration_s,
            )
        except Exception as exc:
            generation_warning = (
                _localized(
                    context.output_language,
                    "The model guide failed evidence or language validation; "
                    "a conservative guide is shown: ",
                    "统一模型导赏输出未通过证据或语言校验，已使用保守地图：",
                )
                + _bounded_error(exc, 500)
            )
            understanding_map = await fallback.build_understanding_map(context)
    if generation_warning:
        understanding_map = understanding_map.model_copy(
            update={
                "warnings": [
                    *understanding_map.warnings[:18],
                    _localized(
                        context.output_language,
                        "The model guide did not pass evidence or language "
                        "validation, so this conservative guide is shown.",
                        "统一模型导赏未通过证据或语言校验，当前显示保守导赏。",
                    ),
                ]
            }
        )
    validate_understanding_map(
        understanding_map,
        result=result,
        duration_s=duration_s,
    )
    return understanding_map, generation_warning


async def get_listener_profile(
    *,
    repository: TeachingRepository,
    user_id: str,
) -> ListenerProfile:
    record = await run_in_threadpool(
        repository.get_listener_profile,
        user_id=user_id,
    )
    return profile_from_record(record)


async def update_listener_profile(
    *,
    repository: TeachingRepository,
    user_id: str,
    payload: ListenerProfileUpdate,
) -> ListenerProfile:
    try:
        record = await run_in_threadpool(
            repository.upsert_listener_profile,
            user_id=user_id,
            level=payload.level.value,
            preferences=payload.preferences,
            learned_concepts=payload.learned_concepts,
        )
    except TeachingEntryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return profile_from_record(record)


async def create_conversation(
    *,
    history: HistoryStore,
    repository: TeachingRepository,
    history_id: str,
    user_id: str,
    title: str | None,
) -> TeachingConversation:
    await run_in_threadpool(require_history, history, history_id, user_id)
    try:
        record = await run_in_threadpool(
            repository.create_conversation,
            history_id,
            user_id=user_id,
            title=title,
        )
    except TeachingEntryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _conversation_from_record(record)


async def list_conversations(
    *,
    history: HistoryStore,
    repository: TeachingRepository,
    history_id: str,
    user_id: str,
    limit: int,
) -> list[TeachingConversation]:
    await run_in_threadpool(require_history, history, history_id, user_id)
    records = await run_in_threadpool(
        repository.list_conversations,
        history_id,
        user_id=user_id,
        limit=limit,
    )
    return [_conversation_from_record(record) for record in records]


async def get_conversation(
    *,
    repository: TeachingRepository,
    history_id: str,
    conversation_id: str,
    user_id: str,
) -> TeachingConversation:
    record = await run_in_threadpool(
        repository.get_conversation,
        conversation_id,
        analysis_id=history_id,
        user_id=user_id,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="导赏对话不存在。")
    return _conversation_from_record(record)


async def delete_conversation(
    *,
    repository: TeachingRepository,
    history_id: str,
    conversation_id: str,
    user_id: str,
) -> None:
    deleted = await run_in_threadpool(
        repository.delete_conversation,
        conversation_id,
        analysis_id=history_id,
        user_id=user_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="导赏对话不存在。")


async def list_messages(
    *,
    repository: TeachingRepository,
    history_id: str,
    conversation_id: str,
    user_id: str,
    limit: int,
) -> list[TeachingMessage]:
    try:
        records = await run_in_threadpool(
            repository.list_messages,
            conversation_id,
            analysis_id=history_id,
            user_id=user_id,
            limit=limit,
        )
    except TeachingEntryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [_message_from_record(record) for record in records]


async def answer_music_question(
    *,
    history: HistoryStore,
    repository: TeachingRepository,
    history_id: str,
    conversation_id: str,
    user_id: str,
    payload: TeachingChatRequest,
    model: TeachingModelAdapter | None = None,
    relisten_provider: TeachingRelistenProvider | None = None,
) -> TeachingMessage:
    entry = await run_in_threadpool(require_history, history, history_id, user_id)
    if entry.result is None:
        raise HTTPException(status_code=409, detail="歌曲分析尚未完成。")
    duration_s = _duration_for_entry(entry.duration_s, entry.result)
    _validate_request_duration(payload, duration_s)
    conversation = await run_in_threadpool(
        repository.get_conversation,
        conversation_id,
        analysis_id=history_id,
        user_id=user_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="导赏对话不存在。")

    understanding_map = await _load_understanding_map(
        history=history,
        repository=repository,
        history_id=history_id,
        user_id=user_id,
        result=entry.result,
        output_language=payload.output_language,
        model=model,
    )
    reserved, existing = await _reserve_chat_message(
        repository,
        history_id=history_id,
        conversation_id=conversation_id,
        user_id=user_id,
        payload=payload,
    )
    if existing is not None:
        return existing

    message_id = str(reserved["id"])
    reservation_token = str(reserved["updated_at"])
    try:
        context, targets = await _prepare_chat_context(
            repository,
            entry=entry,
            understanding_map=understanding_map,
            payload=payload,
            user_id=user_id,
            history_id=history_id,
            conversation_id=conversation_id,
            duration_s=duration_s,
        )
        relisten_warning: str | None = None
        if _should_relisten(payload, context):
            context, relisten_warning = await _relisten(
                history=history,
                provider=relisten_provider,
                history_id=history_id,
                user_id=user_id,
                question=payload.message,
                language=entry.language,
                output_language=payload.output_language,
                context=context,
                targets=targets,
            )
        response = await _answer_chat_context(
            context,
            model=model,
            relisten_warning=relisten_warning,
        )
        validate_chat_response(response, context=context)
        completed = await run_in_threadpool(
            repository.complete_message,
            message_id,
            user_id=user_id,
            response_payload=response.model_dump(mode="json"),
            reservation_token=reservation_token,
        )
        if completed is None:
            raise RuntimeError("消息预留在完成前消失。")
        return _message_from_record(completed)
    except asyncio.CancelledError:
        await asyncio.shield(
            _fail_message_reservation(
                repository,
                message_id=message_id,
                user_id=user_id,
                reservation_token=reservation_token,
                error="导赏问答请求已取消。",
            )
        )
        raise
    except HTTPException:
        await _fail_message_reservation(
            repository,
            message_id=message_id,
            user_id=user_id,
            reservation_token=reservation_token,
            error="请求处理失败。",
        )
        raise
    except Exception as exc:
        error = _bounded_error(exc, 1000)
        await _fail_message_reservation(
            repository,
            message_id=message_id,
            user_id=user_id,
            reservation_token=reservation_token,
            error=error,
        )
        raise HTTPException(
            status_code=502,
            detail=f"音乐导赏回答失败：{_bounded_error(exc, 500)}",
        ) from exc


async def _load_understanding_map(
    *,
    history: HistoryStore,
    repository: TeachingRepository,
    history_id: str,
    user_id: str,
    result: AnalysisResult,
    output_language: str,
    model: TeachingModelAdapter | None,
) -> MusicUnderstandingMap:
    source_hash = analysis_result_hash(result)
    map_record = await run_in_threadpool(
        repository.get_understanding_map,
        history_id,
        user_id=user_id,
    )
    usable_map = (
        map_record is not None
        and map_record.get("status")
        in {TeachingGuideStatus.COMPLETE, TeachingGuideStatus.PENDING}
        and map_record.get("schema_version") == TEACHING_SCHEMA_VERSION
        and map_record.get("source_result_hash") == source_hash
        and map_record.get("map_payload") is not None
        and map_record["map_payload"].get("output_language") == output_language
    )
    if usable_map:
        return MusicUnderstandingMap.model_validate(map_record["map_payload"])
    guide = await generate_teaching_guide(
        history=history,
        repository=repository,
        history_id=history_id,
        user_id=user_id,
        force=True,
        output_language=output_language,
        model=model,
    )
    if guide.understanding_map is None:
        raise HTTPException(status_code=502, detail="导赏地图生成失败。")
    return guide.understanding_map


async def _reserve_chat_message(
    repository: TeachingRepository,
    *,
    history_id: str,
    conversation_id: str,
    user_id: str,
    payload: TeachingChatRequest,
) -> tuple[dict[str, Any], TeachingMessage | None]:
    reservation_task = asyncio.create_task(
        run_in_threadpool(
            repository.reserve_message,
            conversation_id,
            analysis_id=history_id,
            user_id=user_id,
            client_request_id=payload.client_request_id,
            request_payload=payload.model_dump(mode="json"),
            stale_before=datetime.now(UTC) - TEACHING_PENDING_LEASE,
        )
    )
    try:
        reserved, created = await asyncio.shield(reservation_task)
    except asyncio.CancelledError:
        await _compensate_cancelled_message_reservation(
            reservation_task,
            repository=repository,
            user_id=user_id,
        )
        raise
    except TeachingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TeachingEntryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if created:
        return reserved, None
    existing = _message_from_record(reserved)
    if existing.status == TeachingMessageStatus.COMPLETE:
        return reserved, existing
    if existing.status == TeachingMessageStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail="相同问题正在处理，请稍后重试。",
            headers={"Retry-After": "1"},
        )
    raise HTTPException(
        status_code=409,
        detail="此前请求失败，请使用新的 client_request_id 重试。",
    )


async def _compensate_cancelled_message_reservation(
    reservation_task: asyncio.Task,
    *,
    repository: TeachingRepository,
    user_id: str,
) -> None:
    try:
        cancelled_record, cancelled_owner = (
            await _settle_despite_cancellation(reservation_task)
        )
    except Exception:
        return
    if cancelled_owner:
        await asyncio.shield(
            _fail_message_reservation(
                repository,
                message_id=str(cancelled_record["id"]),
                user_id=user_id,
                reservation_token=str(cancelled_record["updated_at"]),
                error="导赏问答请求已取消。",
            )
        )


async def _prepare_chat_context(
    repository: TeachingRepository,
    *,
    entry: HistoryDetail,
    understanding_map: MusicUnderstandingMap,
    payload: TeachingChatRequest,
    user_id: str,
    history_id: str,
    conversation_id: str,
    duration_s: float,
) -> tuple[TeachingChatContext, list[TeachingTimeSpan]]:
    profile_record, message_records = await _profile_and_messages(
        repository,
        user_id=user_id,
        history_id=history_id,
        conversation_id=conversation_id,
    )
    target = focus_span(
        current_time_s=payload.current_time_s,
        duration_s=duration_s,
        selected_range=payload.selected_range,
    )
    comparison_ranges = payload.compare_ranges
    targets = comparison_ranges or [target]
    context = TeachingChatContext(
        analysis_id=history_id,
        question=payload.message,
        current_time_s=payload.current_time_s,
        selected_range=payload.selected_range,
        compare_ranges=comparison_ranges,
        current_section=section_at_time(
            understanding_map,
            payload.current_time_s,
        ),
        nearby_lyrics=nearby_lyrics(entry.result, targets=targets),
        nearby_events=nearby_events(
            understanding_map,
            target=target,
            comparison_ranges=comparison_ranges,
        ),
        nearby_analysis_evidence=nearby_analysis_evidence(
            entry.result,
            targets=targets,
        ),
        conversation_history=_turns_from_records(message_records),
        listener_profile=profile_from_record(profile_record),
        analysis_summary=entry.result.summary[:4000],
        vocal_presence=entry.result.vocal_presence,
        duration_s=duration_s,
        output_language=payload.output_language,
    )
    return context, targets


async def _profile_and_messages(
    repository: TeachingRepository,
    *,
    user_id: str,
    history_id: str,
    conversation_id: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    profile = await run_in_threadpool(
        repository.get_listener_profile,
        user_id=user_id,
    )
    messages = await run_in_threadpool(
        repository.list_messages,
        conversation_id,
        analysis_id=history_id,
        user_id=user_id,
        limit=13,
    )
    return profile, messages


async def _relisten(
    *,
    history: HistoryStore,
    provider: TeachingRelistenProvider | None,
    history_id: str,
    user_id: str,
    question: str,
    language: str | None,
    output_language: str,
    context: TeachingChatContext,
    targets: list[TeachingTimeSpan],
) -> tuple[TeachingChatContext, str | None]:
    if provider is None:
        return context, _localized(
            output_language,
            "The current model cannot re-listen to excerpts; this answer uses saved evidence.",
            "当前模型未提供局部重听能力，本次只使用已保存证据回答。",
        )
    audio_path = await run_in_threadpool(
        history.audio_path,
        history_id,
        user_id=user_id,
    )
    if audio_path is None:
        return context, _localized(
            output_language,
            "The cached audio was not found, so no excerpt was re-listened to.",
            "找不到缓存音频，本次没有进行局部重听。",
        )
    ranges = [
        _bounded_clip(span, duration_s=context.duration_s)
        for span in targets[:2]
    ]
    try:
        relisten = await provider.listen_to_excerpts(
            RelistenRequest(
                analysis_id=history_id,
                audio_path=Path(audio_path),
                question=question,
                ranges=ranges,
                language=language,
                output_language=output_language,
            )
        )
    except Exception as exc:
        return (
            context,
            _localized(
                output_language,
                "Excerpt re-listening failed; saved evidence was used: ",
                "局部重听失败，已使用原有证据回答：",
            )
            + _bounded_error(exc, 300),
        )
    if not relisten.evidence:
        return context, _localized(
            output_language,
            "Excerpt re-listening returned no new verifiable audible fact.",
            "局部重听没有返回可确认的新增听觉事实。",
        )
    return (
        context.model_copy(update={"relisten_evidence": relisten.evidence}),
        "；".join(relisten.warnings[:3]) if relisten.warnings else None,
    )


async def _fail_map_reservation(
    repository: TeachingRepository,
    *,
    history_id: str,
    user_id: str,
    source_hash: str,
    schema_version: int,
    reservation_token: str,
    error: str,
) -> None:
    try:
        await run_in_threadpool(
            repository.fail_understanding_map,
            history_id,
            user_id=user_id,
            source_result_hash=source_hash,
            schema_version=schema_version,
            reservation_token=reservation_token,
            error=error,
        )
    except Exception:
        return


async def _fail_message_reservation(
    repository: TeachingRepository,
    *,
    message_id: str,
    user_id: str,
    reservation_token: str,
    error: str,
) -> None:
    try:
        await run_in_threadpool(
            repository.fail_message,
            message_id,
            user_id=user_id,
            reservation_token=reservation_token,
            error=error,
        )
    except Exception:
        return
