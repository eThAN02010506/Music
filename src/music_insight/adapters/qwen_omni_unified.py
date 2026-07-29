from __future__ import annotations

from music_insight.adapters.openai_chat_audio import OpenAIChatAudioAdapter


class QwenOmniUnifiedAdapter(OpenAIChatAudioAdapter):
    """Backward-compatible Qwen-labelled OpenAI audio adapter."""

    source = "Qwen Omni"

    def __init__(
        self,
        endpoint: str,
        completions_path: str = "/v1/chat/completions",
        models_path: str = "/v1/models",
        model: str | None = None,
        chunk_seconds: float = 30.0,
        chunk_overlap_seconds: float = 1.5,
        display_name: str = "Qwen Omni",
    ) -> None:
        super().__init__(
            endpoint=endpoint,
            completions_path=completions_path,
            models_path=models_path,
            model=model,
            chunk_seconds=chunk_seconds,
            chunk_overlap_seconds=chunk_overlap_seconds,
            display_name=display_name,
        )
