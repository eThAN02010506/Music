from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MUSIC_INSIGHT_")

    workspace_dir: Path = Field(default=Path(".music_insight"))
    max_upload_mb: int = 512

    omni_endpoint: str = "http://192.168.1.97:8004"
    omni_completions_path: str = "/v1/chat/completions"
    omni_models_path: str = "/v1/models"
    omni_model: str | None = None
    omni_chunk_seconds: float = 30.0

    local_model_root: Path = Field(default=Path("src/model"))
    local_omni_endpoint: str = "http://127.0.0.1:8010"
    local_llama_server: str = "llama-server"


@lru_cache
def get_settings() -> Settings:
    return Settings()
