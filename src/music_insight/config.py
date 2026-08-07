from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MUSIC_INSIGHT_",
        # Load a local .env if present (README documents shell exports, but
        # the repo ships .env.example; both should work). An explicit shell
        # env var always wins over the file value.
        env_file=".env",
        extra="ignore",
    )

    workspace_dir: Path = Field(default=Path(".music_insight"))
    max_upload_mb: int = Field(default=128, ge=1, le=1024)
    max_audio_minutes: float = Field(default=20.0, ge=1, le=180)
    remote_audio_timeout_seconds: float = Field(default=120.0, ge=10, le=600)
    remote_audio_max_redirects: int = Field(default=3, ge=0, le=5)
    asset_gc_grace_hours: float = Field(default=24.0, ge=0)
    max_active_jobs: int = Field(default=8, ge=1, le=64)
    max_active_jobs_per_user: int = Field(default=3, ge=1, le=16)
    max_direct_work: int = Field(default=2, ge=1, le=16)
    max_direct_work_per_user: int = Field(default=1, ge=1, le=8)
    direct_work_lease_ttl_seconds: int = Field(
        default=3600,
        ge=60,
        le=24 * 60 * 60,
    )
    stem_backend: Literal["demucs", "disabled"] = "demucs"
    stem_model: str = Field(
        default="htdemucs",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    stem_device: Literal["auto", "cpu", "cuda", "mps"] = "auto"
    stem_timeout_seconds: int = Field(default=7200, ge=60, le=24 * 60 * 60)
    stem_max_concurrency: int = Field(default=1, ge=1, le=4)
    stem_model_cache_dir: Path | None = None
    # A singing comparison carries two independently bounded files.
    max_upload_units: int = Field(default=2, ge=2, le=16)
    auth_kdf_max_concurrency: int = Field(default=4, ge=1, le=16)
    dsp_max_concurrency: int = Field(default=2, ge=1, le=8)
    # None = infer from request scheme; set true/false to pin the Secure flag
    # behind a reverse proxy that terminates TLS.
    cookie_secure: bool | None = None

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
    celery_soft_time_limit_seconds: int = Field(
        default=3 * 60 * 60 + 50 * 60,
        ge=300,
        le=7 * 24 * 60 * 60,
    )

    omni_endpoint: str = "http://192.168.1.97:8004"
    omni_completions_path: str = "/v1/chat/completions"
    omni_models_path: str = "/v1/models"
    omni_model: str | None = None
    omni_chunk_seconds: float = 30.0
    omni_chunk_overlap_seconds: float = Field(default=1.5, ge=0, le=10)
    omni_max_concurrency: int = Field(default=1, ge=1, le=8)
    asr_verifier_enabled: bool = False
    asr_verifier_endpoint: str = "http://192.168.1.97:8003"
    asr_verifier_transcriptions_path: str = "/v1/audio/transcriptions"
    asr_verifier_dialect: Literal["openai_whisper", "crisp_asr"] = "crisp_asr"
    asr_verifier_model: str | None = None
    asr_verifier_api_key: SecretStr | None = None
    asr_verifier_timeout_seconds: float = Field(default=600.0, ge=10, le=3600)
    asr_verifier_vad: bool = False
    asr_verifier_max_concurrency: int = Field(default=1, ge=1, le=8)
    comni_chunk_seconds: float = Field(default=15.0, ge=5, le=60)
    comni_open_timeout_seconds: float = Field(default=10.0, ge=1, le=60)
    comni_first_event_timeout_seconds: float = Field(
        default=600.0,
        ge=10,
        le=900,
    )
    comni_idle_timeout_seconds: float = Field(default=600.0, ge=10, le=900)
    comni_request_timeout_seconds: float = Field(
        default=600.0,
        ge=60,
        le=3600,
    )
    comni_max_message_mb: int = Field(default=8, ge=1, le=64)
    analysis_deadline_seconds: int = Field(
        default=1800,
        ge=60,
        le=4 * 3600,
        description="Whole-song model analysis budget; a half-dead endpoint "
        "cannot stall a task past this ceiling.",
    )

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

    @field_validator("asr_verifier_transcriptions_path")
    @classmethod
    def validate_asr_transcriptions_path(cls, value: str) -> str:
        path = value.strip()
        if not path:
            raise ValueError("asr_verifier_transcriptions_path cannot be empty")
        if not path.startswith("/"):
            path = f"/{path}"
        if path.startswith("//") or "?" in path or "#" in path:
            raise ValueError(
                "asr_verifier_transcriptions_path must be an API path"
            )
        return path

    @model_validator(mode="after")
    def validate_distributed_workspace(self) -> "Settings":
        if (
            self.job_backend == "redis"
            and self.celery_soft_time_limit_seconds
            >= self.celery_visibility_timeout_seconds
        ):
            raise ValueError(
                "celery_soft_time_limit_seconds must be shorter than "
                "celery_visibility_timeout_seconds"
            )
        if (
            self.job_backend == "redis"
            and self.redis_job_ttl_seconds
            <= self.celery_soft_time_limit_seconds
        ):
            raise ValueError(
                "redis_job_ttl_seconds must outlive "
                "celery_soft_time_limit_seconds"
            )
        if (
            self.asr_verifier_enabled
            and self.asr_verifier_dialect == "openai_whisper"
            and not (self.asr_verifier_model or "").strip()
        ):
            raise ValueError(
                "asr_verifier_model is required for openai_whisper dialect"
            )
        if (
            self.asr_verifier_dialect != "crisp_asr"
            and self.asr_verifier_vad
        ):
            raise ValueError(
                "asr_verifier_vad is available only for crisp_asr dialect"
            )
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
