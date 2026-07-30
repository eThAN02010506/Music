from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class StemTrack(BaseModel):
    name: Literal["vocals", "drums", "bass", "other"]
    label: str
    audio_url: str


class StemStatusResponse(BaseModel):
    status: Literal["unavailable", "missing", "processing", "ready"]
    backend: str
    model: str
    stems: list[StemTrack]
    detail: str | None = None
