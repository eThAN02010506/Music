from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
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
from music_insight.api.history import HistoryStore
from music_insight.api.services.history import require_history
from music_insight.api.teaching import (
    TeachingConflictError,
    TeachingDataError,
    TeachingEntryNotFoundError,
)
from music_insight.teaching.fallback import EvidenceTeachingModel
from music_insight.teaching.grounding import (
    analysis_result_hash,
    validate_chat_response,
    validate_understanding_map,
)
from music_insight.teaching.models import (
    ConversationTurn,
    ListenerProfile,
    MapGenerationContext,
    MusicUnderstandingMap,
    RelistenPolicy,
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


TEACHING_SCHEMA_VERSION = 2
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
    if force:
        reserve_options["force"] = True
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
        try:
            cancelled_record, cancelled_owner = (
                await _settle_despite_cancellation(reservation_task)
            )
        except Exception:
            pass
        else:
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
        raise
    except TeachingEntryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not should_generate:
        if (
            record.get("status") == TeachingGuideStatus.COMPLETE
            and record.get("map_payload") is not None
            and record.get("source_result_hash") == source_hash
        ):
            return _guide_from_record(
                record,
                current_source_hash=source_hash,
                cached=True,
            )
        raise HTTPException(
            status_code=409,
            detail="相同版本的导赏地图正在生成，请稍后重试。",
            headers={"Retry-After": "2"},
        )

    reservation_token = str(record["updated_at"])
    fallback = EvidenceTeachingModel()
    generation_warning: str | None = None
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
            listener_profile=profile_from_record(profile_record),
        )
        if model is None:
            understanding_map = await fallback.build_understanding_map(context)
        else:
            try:
                understanding_map = await model.build_understanding_map(context)
                validate_understanding_map(
                    understanding_map,
                    result=entry.result,
                    duration_s=duration_s,
                )
            except Exception as exc:
                generation_warning = (
                    "统一模型导赏输出未通过证据校验，已使用保守地图："
                    f"{_bounded_error(exc, 500)}"
                )
                understanding_map = await fallback.build_understanding_map(context)
        if generation_warning:
            understanding_map = understanding_map.model_copy(
                update={
                    "warnings": [
                        *understanding_map.warnings[:18],
                        generation_warning,
                    ]
                }
            )
        validate_understanding_map(
            understanding_map,
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

    source_hash = analysis_result_hash(entry.result)
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
    )
    if not usable_map:
        guide = await generate_teaching_guide(
            history=history,
            repository=repository,
            history_id=history_id,
            user_id=user_id,
            model=model,
        )
        understanding_map = guide.understanding_map
    else:
        understanding_map = MusicUnderstandingMap.model_validate(
            map_record["map_payload"]
        )
    if understanding_map is None:
        raise HTTPException(status_code=502, detail="导赏地图生成失败。")

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
        try:
            cancelled_record, cancelled_owner = (
                await _settle_despite_cancellation(reservation_task)
            )
        except Exception:
            pass
        else:
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
        raise
    except TeachingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TeachingEntryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not created:
        existing = _message_from_record(reserved)
        if existing.status == TeachingMessageStatus.COMPLETE:
            return existing
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

    message_id = str(reserved["id"])
    reservation_token = str(reserved["updated_at"])
    relisten_warning: str | None = None
    try:
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
        )
        if _should_relisten(payload, context):
            context, relisten_warning = await _relisten(
                history=history,
                provider=relisten_provider,
                history_id=history_id,
                user_id=user_id,
                question=payload.message,
                language=entry.language,
                context=context,
                targets=targets,
            )
        fallback = EvidenceTeachingModel()
        if model is None:
            response = await fallback.answer_music_question(context)
        else:
            try:
                response = await model.answer_music_question(context)
                validate_chat_response(response, context=context)
            except Exception as exc:
                response = await fallback.answer_music_question(context)
                warning = (
                    "统一模型回答未通过证据校验，已使用保守回答："
                    f"{_bounded_error(exc, 400)}"
                )
                response = response.model_copy(
                    update={"warnings": [*response.warnings[:8], warning]}
                )
        if relisten_warning:
            response = response.model_copy(
                update={
                    "warnings": [
                        *response.warnings[:8],
                        relisten_warning,
                    ]
                }
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
    context: TeachingChatContext,
    targets: list[TeachingTimeSpan],
) -> tuple[TeachingChatContext, str | None]:
    if provider is None:
        return context, "当前模型未提供局部重听能力，本次只使用已保存证据回答。"
    audio_path = await run_in_threadpool(
        history.audio_path,
        history_id,
        user_id=user_id,
    )
    if audio_path is None:
        return context, "找不到缓存音频，本次没有进行局部重听。"
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
            )
        )
    except Exception as exc:
        return (
            context,
            f"局部重听失败，已使用原有证据回答：{_bounded_error(exc, 300)}",
        )
    if not relisten.evidence:
        return context, "局部重听没有返回可确认的新增听觉事实。"
    return (
        context.model_copy(update={"relisten_evidence": relisten.evidence}),
        "；".join(relisten.warnings[:3]) if relisten.warnings else None,
    )


def _should_relisten(
    payload: TeachingChatRequest,
    context: TeachingChatContext,
) -> bool:
    if payload.relisten_policy == RelistenPolicy.NEVER:
        return False
    if payload.relisten_policy == RelistenPolicy.ALWAYS:
        return True
    has_selected_scope = bool(payload.selected_range or payload.compare_ranges)
    evidence_sparse = (
        not context.nearby_events or not context.nearby_analysis_evidence
    )
    asks_for_detail = any(
        token in payload.message.casefold()
        for token in ("重新听", "再听", "具体乐器", "what instrument", "listen again")
    )
    return (has_selected_scope and evidence_sparse) or (
        asks_for_detail and evidence_sparse
    )


def _guide_from_record(
    record: Mapping[str, Any],
    *,
    current_source_hash: str,
    cached: bool,
) -> TeachingGuideResponse:
    source_hash = record.get("source_result_hash")
    pending_hash = record.get("pending_source_result_hash")
    effective_hash = str(source_hash or pending_hash or current_source_hash)
    payload = record.get("map_payload")
    try:
        schema_version = int(record.get("schema_version") or 1)
        understanding_map = (
            MusicUnderstandingMap.model_validate(payload)
            if payload is not None
            else None
        )
        return TeachingGuideResponse(
            analysis_id=str(record["analysis_id"]),
            schema_version=schema_version,
            source_result_hash=effective_hash,
            status=str(record.get("status") or "failed"),
            understanding_map=understanding_map,
            stale=(
                source_hash != current_source_hash
                or schema_version != TEACHING_SCHEMA_VERSION
            ),
            cached=cached,
            error=record.get("last_error"),
            updated_at=_datetime_or_none(record.get("updated_at")),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=500,
            detail="已保存的导赏地图格式无效。",
        ) from exc


def _conversation_from_record(record: Mapping[str, Any]) -> TeachingConversation:
    summary = record.get("summary")
    normalized_summary: str | None = None
    if isinstance(summary, Mapping):
        candidate = summary.get("text") or summary.get("summary")
        if isinstance(candidate, str):
            normalized_summary = candidate[:2000]
    elif isinstance(summary, str):
        normalized_summary = summary[:2000]
    try:
        return TeachingConversation(
            id=str(record["id"]),
            analysis_id=str(record["analysis_id"]),
            title=record.get("title"),
            summary=normalized_summary,
            message_count=int(record.get("message_count") or 0),
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise TeachingDataError("已保存的导赏对话格式无效。") from exc


def _message_from_record(record: Mapping[str, Any]) -> TeachingMessage:
    try:
        return TeachingMessage(
            id=str(record["id"]),
            conversation_id=str(record["conversation_id"]),
            sequence=int(record["sequence"]),
            status=str(record["status"]),
            client_request_id=str(record["client_request_id"]),
            request=record.get("request_payload") or {},
            response=record.get("response_payload"),
            error=record.get("error"),
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise TeachingDataError("已保存的导赏消息格式无效。") from exc


def _turns_from_records(
    records: list[dict[str, Any]],
) -> list[ConversationTurn]:
    turns: list[ConversationTurn] = []
    for record in records:
        if record.get("status") != "complete":
            continue
        request = record.get("request_payload")
        response = record.get("response_payload")
        if not isinstance(request, Mapping) or not isinstance(response, Mapping):
            continue
        question = request.get("message")
        answer = response.get("answer")
        if not isinstance(question, str) or not isinstance(answer, str):
            continue
        try:
            turns.append(
                ConversationTurn(
                    question=question,
                    answer=answer,
                    created_at=record["created_at"],
                )
            )
        except (KeyError, ValidationError):
            continue
    return turns[-12:]


def _validate_request_duration(
    payload: TeachingChatRequest,
    duration_s: float,
) -> None:
    if payload.current_time_s > duration_s + 0.5:
        raise HTTPException(status_code=422, detail="当前播放位置超过音频时长。")
    ranges = [*payload.compare_ranges]
    if payload.selected_range is not None:
        ranges.append(payload.selected_range)
    if any(span.end_s > duration_s + 0.5 for span in ranges):
        raise HTTPException(status_code=422, detail="选中的时间范围超过音频时长。")


def _duration_for_entry(duration_s: float | None, result) -> float:
    if duration_s is not None and duration_s > 0:
        return duration_s
    endpoints = [
        item.span.end_s
        for item in [
            *result.lyrics,
            *result.sound_events,
            *result.emotion_timeline,
            *result.inferred_atmosphere,
            *result.technical_metrics.energy_curve,
            *result.evidence,
        ]
        if item.span is not None
    ]
    if endpoints:
        return max(endpoints)
    raise HTTPException(status_code=422, detail="缺少可用的音频时长。")


def _bounded_clip(
    span: TeachingTimeSpan,
    *,
    duration_s: float,
) -> TeachingTimeSpan:
    if span.end_s - span.start_s <= 30:
        return span
    midpoint = (span.start_s + span.end_s) / 2
    start_s = max(0.0, midpoint - 15)
    end_s = min(duration_s, start_s + 30)
    start_s = max(0.0, end_s - 30)
    return TeachingTimeSpan(start_s=start_s, end_s=end_s)


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


def _bounded_error(error: BaseException, limit: int) -> str:
    return (str(error).strip() or error.__class__.__name__)[:limit]


def _datetime_or_none(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise ValueError("invalid datetime")
