from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MUSIC_INSIGHT_")

    workspace_dir: Path = Field(default=Path(".music_insight"))
    max_upload_mb: int = Field(default=128, ge=1, le=1024)
    max_audio_minutes: float = Field(default=20.0, ge=1, le=180)
    asset_gc_grace_hours: float = Field(default=24.0, ge=0)
    max_active_jobs: int = Field(default=8, ge=1, le=64)
    max_active_jobs_per_user: int = Field(default=3, ge=1, le=16)
    max_direct_work: int = Field(default=2, ge=1, le=16)
    max_direct_work_per_user: int = Field(default=1, ge=1, le=8)
    # A singing comparison carries two independently bounded files.
    max_upload_units: int = Field(default=2, ge=2, le=16)
    auth_kdf_max_concurrency: int = Field(default=4, ge=1, le=16)
    dsp_max_concurrency: int = Field(default=2, ge=1, le=8)

    job_backend: Literal["memory", "redis"] = "memory"
    shared_audio_dir: Path | None = None
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_key_prefix: str = Field(
        default="music-insight",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    redis_job_ttl_seconds: int = Field(
        default=7 * 24 * 60 * 60,
        ge=3600,
        le=90 * 24 * 60 * 60,
    )
    celery_queue_name: str = Field(
        default="music-insight.analysis",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    celery_visibility_timeout_seconds: int = Field(
        default=4 * 60 * 60,
        ge=3600,
        le=7 * 24 * 60 * 60,
    )

    omni_endpoint: str = "http://192.168.1.97:8004"
    omni_completions_path: str = "/v1/chat/completions"
    omni_models_path: str = "/v1/models"
    omni_model: str | None = None
    omni_chunk_seconds: float = 30.0
    omni_chunk_overlap_seconds: float = Field(default=1.5, ge=0, le=10)
    omni_max_concurrency: int = Field(default=1, ge=1, le=8)
    comni_chunk_seconds: float = Field(default=15.0, ge=5, le=60)
    comni_open_timeout_seconds: float = Field(default=10.0, ge=1, le=60)
    comni_first_event_timeout_seconds: float = Field(
        default=180.0,
        ge=10,
        le=900,
    )
    comni_idle_timeout_seconds: float = Field(default=180.0, ge=10, le=900)
    comni_request_timeout_seconds: float = Field(
        default=600.0,
        ge=60,
        le=3600,
    )
    comni_max_message_mb: int = Field(default=8, ge=1, le=64)

    local_model_root: Path = Field(default=Path("src/model"))
    local_omni_endpoint: str = "http://127.0.0.1:8011"
    local_llama_server: str = "llama-server"

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: str) -> str:
        if not value.startswith(("redis://", "rediss://")):
            raise ValueError(
                "redis_url must use redis:// or rediss://"
            )
        return value

    @model_validator(mode="after")
    def validate_distributed_workspace(self) -> "Settings":
        if self.job_backend == "redis" and (
            self.shared_audio_dir is None
            or not self.shared_audio_dir.is_absolute()
        ):
            raise ValueError(
                "shared_audio_dir must be an absolute shared path in redis mode"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
