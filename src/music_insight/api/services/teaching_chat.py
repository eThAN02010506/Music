from __future__ import annotations

import logging

from music_insight.api.contracts.teaching import TeachingChatRequest
from music_insight.api.services.teaching_records import localized
from music_insight.teaching.fallback import EvidenceTeachingModel
from music_insight.teaching.grounding import validate_chat_response
from music_insight.teaching.models import (
    RelistenPolicy,
    TeachingChatContext,
    TeachingChatResponse,
    normalize_question,
)
from music_insight.teaching.protocols import TeachingModelAdapter


_LOGGER = logging.getLogger(__name__)


async def answer_chat_context(
    context: TeachingChatContext,
    *,
    model: TeachingModelAdapter | None,
    relisten_warning: str | None,
) -> TeachingChatResponse:
    fallback = EvidenceTeachingModel()
    if model is None:
        response = await fallback.answer_music_question(context)
    else:
        try:
            response = await model.answer_music_question(context)
            validate_chat_response(response, context=context)
        except Exception as exc:
            _LOGGER.warning(
                "Teaching chat provider output rejected; using evidence fallback: %s",
                exc,
                exc_info=True,
            )
            response = await fallback.answer_music_question(context)
            warning = localized(
                context.output_language,
                "The model answer did not pass evidence or language "
                "validation, so this conservative answer is shown.",
                "统一模型回答未通过证据或语言校验，当前显示保守回答。",
            )
            response = response.model_copy(
                update={"warnings": [*response.warnings[:8], warning]}
            )
    response = _remove_repeated_suggestions(response, context=context)
    if relisten_warning:
        response = response.model_copy(
            update={
                "warnings": [
                    *response.warnings[:8],
                    relisten_warning,
                ]
            }
        )
    return response


def should_relisten(
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
        for token in (
            "重新听",
            "再听",
            "具体乐器",
            "what instrument",
            "listen again",
        )
    )
    return (has_selected_scope and evidence_sparse) or (
        asks_for_detail and evidence_sparse
    )


def _remove_repeated_suggestions(
    response: TeachingChatResponse,
    *,
    context: TeachingChatContext,
) -> TeachingChatResponse:
    """Do not recommend a question that the listener has already asked."""

    excluded = {
        normalize_question(context.question),
        *(
            normalize_question(turn.question)
            for turn in context.conversation_history[-8:]
        ),
    }
    seen: set[str] = set()
    suggestions: list[str] = []
    for suggestion in response.suggested_questions:
        normalized = normalize_question(suggestion)
        if not normalized or normalized in excluded or normalized in seen:
            continue
        seen.add(normalized)
        suggestions.append(suggestion)
    if suggestions == response.suggested_questions:
        return response
    return response.model_copy(update={"suggested_questions": suggestions})
