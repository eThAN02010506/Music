"""Evidence-grounded music teaching domain.

The teaching package deliberately depends on the stable analysis schemas, not
on HTTP, SQLite, or a specific model provider.  API and persistence adapters
can therefore evolve without weakening the evidence contract.
"""

from music_insight.teaching.models import (
    AnalysisEvidenceRef,
    AnswerEvidence,
    AnswerTimeRange,
    AudioDimension,
    ConversationTurn,
    EvidenceClaimType,
    EvidenceSourceType,
    KeyMoment,
    ListenerLevel,
    ListenerProfile,
    ListeningTask,
    MusicUnderstandingMap,
    PlayerAction,
    PlayerActionType,
    RelistenEvidence,
    RelistenPolicy,
    RelistenRequest,
    RelistenResult,
    SectionMarker,
    TeachingChatContext,
    TeachingChatResponse,
    TeachingTimeSpan,
    UnderstandingEvent,
)

__all__ = [
    "AnalysisEvidenceRef",
    "AnswerEvidence",
    "AnswerTimeRange",
    "AudioDimension",
    "ConversationTurn",
    "EvidenceClaimType",
    "EvidenceSourceType",
    "KeyMoment",
    "ListenerLevel",
    "ListenerProfile",
    "ListeningTask",
    "MusicUnderstandingMap",
    "PlayerAction",
    "PlayerActionType",
    "RelistenEvidence",
    "RelistenPolicy",
    "RelistenRequest",
    "RelistenResult",
    "SectionMarker",
    "TeachingChatContext",
    "TeachingChatResponse",
    "TeachingTimeSpan",
    "UnderstandingEvent",
]
