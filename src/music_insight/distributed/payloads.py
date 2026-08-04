from __future__ import annotations

from pydantic import BaseModel, Field

from music_insight.schemas import AudioAsset


class DistributedAnalysisPayload(BaseModel):
    """JSON-only payload accepted by a Celery analysis worker."""

    job_id: str = Field(min_length=1, max_length=128)
    owner_user_id: str = Field(min_length=1, max_length=128)
    asset: AudioAsset
    model_source: str = Field(pattern=r"^(network|local)$")
    model_endpoint: str | None = None
    local_model_path: str | None = None
    content_key: str | None = Field(
        default=None,
        max_length=96,
        description="SHA-256 prefix of the source audio, recorded at enqueue "
        "time so a worker can reject same-size file replacement.",
    )
