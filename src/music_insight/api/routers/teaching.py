from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from music_insight.api.accounts import UserPublic
from music_insight.api.contracts.teaching import (
    ConversationCreateRequest,
    ListenerProfileUpdate,
    TeachingChatRequest,
    TeachingConversation,
    TeachingGuideGenerateRequest,
    TeachingGuideResponse,
    TeachingMessage,
)
from music_insight.api.dependencies import get_current_user, get_history_store
from music_insight.api.history import HistoryStore
from music_insight.api.services import teaching as service
from music_insight.api.services.teaching_runtime import (
    resolve_teaching_runtime,
)
from music_insight.config import Settings, get_settings
from music_insight.teaching.models import ListenerProfile
from music_insight.teaching.protocols import (
    TeachingModelAdapter,
    TeachingRelistenProvider,
    TeachingRepository,
)


router = APIRouter(tags=["teaching"])


def get_teaching_repository(request: Request) -> TeachingRepository:
    repository = getattr(request.app.state, "teaching_repository", None)
    if repository is None:
        raise HTTPException(
            status_code=503,
            detail="音乐导赏存储尚未初始化。",
        )
    if not isinstance(repository, TeachingRepository):
        raise HTTPException(status_code=500, detail="音乐导赏存储配置无效。")
    return repository


def get_teaching_model(request: Request) -> TeachingModelAdapter | None:
    model = getattr(request.app.state, "teaching_model", None)
    if model is None:
        return None
    if not isinstance(model, TeachingModelAdapter):
        raise HTTPException(status_code=500, detail="音乐导赏模型配置无效。")
    return model


def get_relisten_provider(
    request: Request,
) -> TeachingRelistenProvider | None:
    provider = getattr(request.app.state, "teaching_relisten_provider", None)
    if provider is None:
        return None
    if not isinstance(provider, TeachingRelistenProvider):
        raise HTTPException(status_code=500, detail="局部重听模型配置无效。")
    return provider


@router.get("/listener-profile", response_model=ListenerProfile)
async def read_listener_profile(
    repository: TeachingRepository = Depends(get_teaching_repository),
    user: UserPublic = Depends(get_current_user),
) -> ListenerProfile:
    return await service.get_listener_profile(
        repository=repository,
        user_id=user.id,
    )


@router.put("/listener-profile", response_model=ListenerProfile)
async def write_listener_profile(
    payload: ListenerProfileUpdate,
    repository: TeachingRepository = Depends(get_teaching_repository),
    user: UserPublic = Depends(get_current_user),
) -> ListenerProfile:
    return await service.update_listener_profile(
        repository=repository,
        user_id=user.id,
        payload=payload,
    )


@router.get(
    "/history/{history_id}/teaching-guide",
    response_model=TeachingGuideResponse,
)
async def read_teaching_guide(
    history_id: str,
    history: HistoryStore = Depends(get_history_store),
    repository: TeachingRepository = Depends(get_teaching_repository),
    user: UserPublic = Depends(get_current_user),
) -> TeachingGuideResponse:
    return await service.get_teaching_guide(
        history=history,
        repository=repository,
        history_id=history_id,
        user_id=user.id,
    )


@router.post(
    "/history/{history_id}/teaching-guide",
    response_model=TeachingGuideResponse,
)
async def create_teaching_guide(
    request: Request,
    history_id: str,
    payload: TeachingGuideGenerateRequest | None = None,
    history: HistoryStore = Depends(get_history_store),
    repository: TeachingRepository = Depends(get_teaching_repository),
    model: TeachingModelAdapter | None = Depends(get_teaching_model),
    settings: Settings = Depends(get_settings),
    user: UserPublic = Depends(get_current_user),
) -> TeachingGuideResponse:
    options = payload or TeachingGuideGenerateRequest()
    runtime_model: TeachingModelAdapter | None = None
    if options.strategy == "evidence":
        return await service.generate_teaching_guide(
            history=history,
            repository=repository,
            history_id=history_id,
            user_id=user.id,
            force=options.force,
            output_language=options.output_language,
            model=None,
        )
    runtime = await resolve_teaching_runtime(
        request=request,
        history=history,
        history_id=history_id,
        user_id=user.id,
        settings=settings,
        model_override=model,
        relisten_override=None,
    )
    runtime_model = runtime.model
    async with request.app.state.direct_work_limiter.lease(user.id):
        return await service.generate_teaching_guide(
            history=history,
            repository=repository,
            history_id=history_id,
            user_id=user.id,
            force=options.force,
            output_language=options.output_language,
            model=runtime_model,
        )


@router.get(
    "/history/{history_id}/conversations",
    response_model=list[TeachingConversation],
)
async def read_conversations(
    history_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    history: HistoryStore = Depends(get_history_store),
    repository: TeachingRepository = Depends(get_teaching_repository),
    user: UserPublic = Depends(get_current_user),
) -> list[TeachingConversation]:
    return await service.list_conversations(
        history=history,
        repository=repository,
        history_id=history_id,
        user_id=user.id,
        limit=limit,
    )


@router.post(
    "/history/{history_id}/conversations",
    response_model=TeachingConversation,
    status_code=201,
)
async def create_teaching_conversation(
    history_id: str,
    payload: ConversationCreateRequest,
    history: HistoryStore = Depends(get_history_store),
    repository: TeachingRepository = Depends(get_teaching_repository),
    user: UserPublic = Depends(get_current_user),
) -> TeachingConversation:
    return await service.create_conversation(
        history=history,
        repository=repository,
        history_id=history_id,
        user_id=user.id,
        title=payload.title,
    )


@router.get(
    "/history/{history_id}/conversations/{conversation_id}",
    response_model=TeachingConversation,
)
async def read_teaching_conversation(
    history_id: str,
    conversation_id: str,
    repository: TeachingRepository = Depends(get_teaching_repository),
    user: UserPublic = Depends(get_current_user),
) -> TeachingConversation:
    return await service.get_conversation(
        repository=repository,
        history_id=history_id,
        conversation_id=conversation_id,
        user_id=user.id,
    )


@router.delete(
    "/history/{history_id}/conversations/{conversation_id}",
    status_code=204,
)
async def remove_teaching_conversation(
    history_id: str,
    conversation_id: str,
    repository: TeachingRepository = Depends(get_teaching_repository),
    user: UserPublic = Depends(get_current_user),
) -> Response:
    await service.delete_conversation(
        repository=repository,
        history_id=history_id,
        conversation_id=conversation_id,
        user_id=user.id,
    )
    return Response(status_code=204)


@router.get(
    "/history/{history_id}/conversations/{conversation_id}/messages",
    response_model=list[TeachingMessage],
)
async def read_teaching_messages(
    history_id: str,
    conversation_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    repository: TeachingRepository = Depends(get_teaching_repository),
    user: UserPublic = Depends(get_current_user),
) -> list[TeachingMessage]:
    return await service.list_messages(
        repository=repository,
        history_id=history_id,
        conversation_id=conversation_id,
        user_id=user.id,
        limit=limit,
    )


@router.post(
    "/history/{history_id}/conversations/{conversation_id}/messages",
    response_model=TeachingMessage,
)
async def create_teaching_message(
    request: Request,
    history_id: str,
    conversation_id: str,
    payload: TeachingChatRequest,
    history: HistoryStore = Depends(get_history_store),
    repository: TeachingRepository = Depends(get_teaching_repository),
    model: TeachingModelAdapter | None = Depends(get_teaching_model),
    relisten_provider: TeachingRelistenProvider | None = Depends(
        get_relisten_provider
    ),
    settings: Settings = Depends(get_settings),
    user: UserPublic = Depends(get_current_user),
) -> TeachingMessage:
    runtime = await resolve_teaching_runtime(
        request=request,
        history=history,
        history_id=history_id,
        user_id=user.id,
        settings=settings,
        model_override=model,
        relisten_override=relisten_provider,
    )
    async with request.app.state.direct_work_limiter.lease(user.id):
        return await service.answer_music_question(
            history=history,
            repository=repository,
            history_id=history_id,
            conversation_id=conversation_id,
            user_id=user.id,
            payload=payload,
            model=runtime.model,
            relisten_provider=runtime.relisten,
        )
