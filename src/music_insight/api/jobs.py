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
    persistence_error: str | None = None
    revision: int = 0


class JobEvent(BaseModel):
    state: JobState
    stage: str
    progress: float = Field(ge=0, le=1)
    message: str
    timestamp: datetime
    error: str | None = None


class _Job:
    def __init__(
        self,
        job_id: str,
        observer: JobObserver | None = None,
        max_events: int = 200,
    ) -> None:
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
        self.persistence_error: str | None = None
        self.revision = 0
        self.task: asyncio.Task[None] | None = None
        self.observer = observer
        self.max_events = max(10, max_events)
        self.events = [
            JobEvent(
                state=self.state,
                stage=self.stage,
                progress=self.progress,
                message=self.message,
                timestamp=now,
            )
        ]

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
            persistence_error=self.persistence_error,
            revision=self.revision,
        )

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)
        self.revision += 1

    def record_event(self) -> None:
        self.events.append(
            JobEvent(
                state=self.state,
                stage=self.stage,
                progress=self.progress,
                message=self.message,
                timestamp=self.updated_at,
                error=self.error,
            )
        )
        if len(self.events) > self.max_events:
            del self.events[: len(self.events) - self.max_events]


ProgressUpdate = Callable[[str, float, str], Awaitable[None]]
JobWork = Callable[[ProgressUpdate], Awaitable[AnalysisResult]]
JobObserver = Callable[
    [JobSnapshot, AnalysisResult | None], Awaitable[None] | None
]


class AnalysisJobStore:
    """Small in-memory job store for a single local Music Insight process."""

    def __init__(self, max_completed: int = 100, max_events: int = 200) -> None:
        self._jobs: dict[str, _Job] = {}
        self.max_completed = max(1, int(max_completed))
        self.max_events = max(10, int(max_events))

    def create(
        self,
        work: JobWork,
        observer: JobObserver | None = None,
    ) -> JobSnapshot:
        self._prune()
        job = _Job(
            uuid4().hex,
            observer=observer,
            max_events=self.max_events,
        )
        self._jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, work))
        return job.snapshot()

    def get(self, job_id: str) -> JobSnapshot | None:
        job = self._jobs.get(job_id)
        return job.snapshot() if job else None

    def result(self, job_id: str) -> AnalysisResult | None:
        job = self._jobs.get(job_id)
        return job.result if job else None

    def list(self, limit: int = 50) -> list[JobSnapshot]:
        snapshots = [job.snapshot() for job in self._jobs.values()]
        snapshots.sort(key=lambda item: item.updated_at, reverse=True)
        return snapshots[: max(1, min(limit, 500))]

    def events(self, job_id: str) -> list[JobEvent]:
        job = self._jobs.get(job_id)
        return list(job.events) if job else []

    def remove(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.state in {JobState.QUEUED, JobState.RUNNING}:
            return False
        del self._jobs[job_id]
        return True

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
            job.record_event()
            await self._notify_observer(job)

        try:
            await update("starting", 0.02, "正在启动分析")
            job.result = await work(update)
            job.state = JobState.COMPLETED
            job.stage = "completed"
            job.progress = 1.0
            job.message = "分析完成"
            job.touch()
            job.record_event()
            await self._notify_observer(job)
        except asyncio.CancelledError:
            job.state = JobState.CANCELLED
            job.stage = "cancelled"
            job.message = "任务已取消"
            job.touch()
            job.record_event()
            await self._notify_observer(job)
        except Exception as exc:
            job.state = JobState.FAILED
            job.stage = "failed"
            job.message = "分析失败"
            job.error = (str(exc).strip() or exc.__class__.__name__)[:1000]
            job.touch()
            job.record_event()
            await self._notify_observer(job)
        finally:
            self._prune()

    @staticmethod
    async def _notify_observer(job: _Job) -> None:
        if job.observer is None:
            return
        try:
            observed = job.observer(job.snapshot(), job.result)
            if inspect.isawaitable(observed):
                await observed
            job.persistence_error = None
        except Exception as exc:
            job.persistence_error = (
                str(exc).strip() or exc.__class__.__name__
            )[:1000]
            job.touch()

    def _prune(self) -> None:
        terminal_ids = [
            job_id
            for job_id, job in self._jobs.items()
            if job.state
            in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
        ]
        excess = len(terminal_ids) - self.max_completed
        for job_id in terminal_ids[: max(0, excess)]:
            del self._jobs[job_id]

    def raw_revision(self, job_id: str) -> int | None:
        job = self._jobs.get(job_id)
        return job.revision if job else None


def snapshot_event(snapshot: JobSnapshot) -> str:
    payload: dict[str, Any] = snapshot.model_dump(mode="json")
    return f"event: progress\ndata: {JobSnapshot(**payload).model_dump_json()}\n\n"
