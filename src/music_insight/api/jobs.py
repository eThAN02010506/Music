from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
import inspect
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from music_insight.schemas import AnalysisResult


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobSnapshot(BaseModel):
    id: str
    state: JobState
    stage: str
    progress: float = Field(ge=0, le=1)
    message: str
    created_at: datetime
    updated_at: datetime
    result_url: str | None = None
    error: str | None = None
    revision: int = 0


class _Job:
    def __init__(self, job_id: str, observer: JobObserver | None = None) -> None:
        now = datetime.now(UTC)
        self.id = job_id
        self.state = JobState.QUEUED
        self.stage = "queued"
        self.progress = 0.0
        self.message = "任务已加入队列"
        self.created_at = now
        self.updated_at = now
        self.result: AnalysisResult | None = None
        self.error: str | None = None
        self.revision = 0
        self.task: asyncio.Task[None] | None = None
        self.observer = observer

    def snapshot(self) -> JobSnapshot:
        return JobSnapshot(
            id=self.id,
            state=self.state,
            stage=self.stage,
            progress=self.progress,
            message=self.message,
            created_at=self.created_at,
            updated_at=self.updated_at,
            result_url=(
                f"/jobs/{self.id}/result"
                if self.state == JobState.COMPLETED
                else None
            ),
            error=self.error,
            revision=self.revision,
        )

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)
        self.revision += 1


ProgressUpdate = Callable[[str, float, str], Awaitable[None]]
JobWork = Callable[[ProgressUpdate], Awaitable[AnalysisResult]]
JobObserver = Callable[
    [JobSnapshot, AnalysisResult | None], Awaitable[None] | None
]


class AnalysisJobStore:
    """Small in-memory job store for a single local Music Insight process."""

    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}

    def create(
        self,
        work: JobWork,
        observer: JobObserver | None = None,
    ) -> JobSnapshot:
        job = _Job(uuid4().hex, observer=observer)
        self._jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, work))
        return job.snapshot()

    def get(self, job_id: str) -> JobSnapshot | None:
        job = self._jobs.get(job_id)
        return job.snapshot() if job else None

    def result(self, job_id: str) -> AnalysisResult | None:
        job = self._jobs.get(job_id)
        return job.result if job else None

    def cancel(self, job_id: str) -> JobSnapshot | None:
        job = self._jobs.get(job_id)
        if not job:
            return None
        if job.state in {JobState.QUEUED, JobState.RUNNING} and job.task:
            job.task.cancel()
        return job.snapshot()

    async def _run(self, job: _Job, work: JobWork) -> None:
        async def update(stage: str, progress: float, message: str) -> None:
            job.state = JobState.RUNNING
            job.stage = stage
            job.progress = max(job.progress, min(float(progress), 0.99))
            job.message = message
            job.touch()
            await self._notify_observer(job)

        try:
            await update("starting", 0.02, "正在启动分析")
            job.result = await work(update)
            job.state = JobState.COMPLETED
            job.stage = "completed"
            job.progress = 1.0
            job.message = "分析完成"
            job.touch()
            await self._notify_observer(job)
        except asyncio.CancelledError:
            job.state = JobState.CANCELLED
            job.stage = "cancelled"
            job.message = "任务已取消"
            job.touch()
            await self._notify_observer(job)
        except Exception as exc:
            job.state = JobState.FAILED
            job.stage = "failed"
            job.message = "分析失败"
            job.error = (str(exc).strip() or exc.__class__.__name__)[:1000]
            job.touch()
            await self._notify_observer(job)

    @staticmethod
    async def _notify_observer(job: _Job) -> None:
        if job.observer is None:
            return
        observed = job.observer(job.snapshot(), job.result)
        if inspect.isawaitable(observed):
            await observed

    def raw_revision(self, job_id: str) -> int | None:
        job = self._jobs.get(job_id)
        return job.revision if job else None


def snapshot_event(snapshot: JobSnapshot) -> str:
    payload: dict[str, Any] = snapshot.model_dump(mode="json")
    return f"event: progress\ndata: {JobSnapshot(**payload).model_dump_json()}\n\n"
