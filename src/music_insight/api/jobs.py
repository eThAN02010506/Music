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
        *,
        owner_user_id: str,
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
        self.observer_lock = asyncio.Lock()
        self.task: asyncio.Task[None] | None = None
        self.cancel_requested = False
        self.observer = observer
        self.owner_user_id = owner_user_id
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


class JobCapacityError(RuntimeError):
    """Raised when the bounded in-memory job queue is full."""

    def __init__(self, message: str, *, global_limit: bool = False) -> None:
        super().__init__(message)
        self.global_limit = global_limit


class AnalysisJobStore:
    """Small in-memory job store for a single local Music Insight process."""

    def __init__(
        self,
        max_completed: int = 100,
        max_events: int = 200,
        *,
        max_active: int = 8,
        max_active_per_owner: int = 3,
    ) -> None:
        self._jobs: dict[str, _Job] = {}
        self.max_completed = max(1, int(max_completed))
        self.max_events = max(10, int(max_events))
        self.max_active = max(1, int(max_active))
        self.max_active_per_owner = max(1, int(max_active_per_owner))

    def create(
        self,
        work: JobWork,
        observer: JobObserver | None = None,
        *,
        owner_user_id: str,
    ) -> JobSnapshot:
        owner = self._require_owner(owner_user_id)
        self._prune()
        self.ensure_capacity(owner)
        job = _Job(
            uuid4().hex,
            observer=observer,
            max_events=self.max_events,
            owner_user_id=owner,
        )
        self._jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, work))
        return job.snapshot()

    def ensure_capacity(self, owner_user_id: str) -> None:
        """Fail before accepting more work than this process can retain."""

        owner = self._require_owner(owner_user_id)
        active = [
            job
            for job in self._jobs.values()
            if job.state in {JobState.QUEUED, JobState.RUNNING}
        ]
        if len(active) >= self.max_active:
            raise JobCapacityError(
                "分析队列已满，请等待现有任务完成后重试。",
                global_limit=True,
            )
        owner_active = sum(job.owner_user_id == owner for job in active)
        if owner_active >= self.max_active_per_owner:
            raise JobCapacityError(
                "你的待处理分析已达到上限，请等待或取消现有任务。",
            )

    async def shutdown(self) -> None:
        """Settle every accepted task before the application event loop exits."""

        active_tasks: list[asyncio.Task[None]] = []
        for job in self._jobs.values():
            if (
                job.state in {JobState.QUEUED, JobState.RUNNING}
                and job.task is not None
            ):
                already_requested = job.cancel_requested
                job.cancel_requested = True
                # A task may already be inside its shielded terminal observer.
                # Cancelling it a second time can interrupt publication after
                # the durable update and leave the in-memory job non-terminal.
                if job.state == JobState.RUNNING and not already_requested:
                    job.task.cancel()
                active_tasks.append(job.task)
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

    def _owned_job(
        self,
        job_id: str,
        owner_user_id: str,
    ) -> _Job | None:
        owner = self._require_owner(owner_user_id)
        job = self._jobs.get(job_id)
        if job is not None and job.owner_user_id != owner:
            return None
        return job

    def get(
        self,
        job_id: str,
        *,
        owner_user_id: str,
    ) -> JobSnapshot | None:
        job = self._owned_job(job_id, owner_user_id)
        return job.snapshot() if job else None

    def get_for_maintenance(self, job_id: str) -> JobSnapshot | None:
        """Explicit process-local unscoped lookup for maintenance tooling."""

        job = self._jobs.get(job_id)
        return job.snapshot() if job else None

    def result(
        self,
        job_id: str,
        *,
        owner_user_id: str,
    ) -> AnalysisResult | None:
        job = self._owned_job(job_id, owner_user_id)
        return job.result if job else None

    def list(
        self,
        *,
        owner_user_id: str,
        limit: int = 50,
    ) -> list[JobSnapshot]:
        owner = self._require_owner(owner_user_id)
        snapshots = [
            job.snapshot()
            for job in self._jobs.values()
            if job.owner_user_id == owner
        ]
        snapshots.sort(key=lambda item: item.updated_at, reverse=True)
        return snapshots[: max(1, min(limit, 500))]

    def list_all_for_maintenance(
        self,
        *,
        limit: int = 50,
    ) -> list[JobSnapshot]:
        """Explicit process-local unscoped list for maintenance tooling."""

        snapshots = [job.snapshot() for job in self._jobs.values()]
        snapshots.sort(key=lambda item: item.updated_at, reverse=True)
        return snapshots[: max(1, min(limit, 500))]

    def events(
        self,
        job_id: str,
        *,
        owner_user_id: str,
    ) -> list[JobEvent]:
        job = self._owned_job(job_id, owner_user_id)
        return list(job.events) if job else []

    def remove(
        self,
        job_id: str,
        *,
        owner_user_id: str,
    ) -> bool:
        job = self._owned_job(job_id, owner_user_id)
        if job is None:
            return False
        if job.state in {JobState.QUEUED, JobState.RUNNING}:
            return False
        del self._jobs[job_id]
        return True

    def cancel(
        self,
        job_id: str,
        *,
        owner_user_id: str,
    ) -> JobSnapshot | None:
        job = self._owned_job(job_id, owner_user_id)
        if not job:
            return None
        if job.state in {JobState.QUEUED, JobState.RUNNING} and job.task:
            already_requested = job.cancel_requested
            job.cancel_requested = True
            # A task cancelled before its coroutine first runs cannot execute
            # ``_run``'s cancellation handler. Let a queued task start and
            # observe the flag; interrupt only work that has actually started.
            if job.state == JobState.RUNNING and not already_requested:
                job.task.cancel()
        return job.snapshot()

    async def cancel_and_wait(
        self,
        job_id: str,
        *,
        owner_user_id: str,
    ) -> JobSnapshot | None:
        """Request cancellation and wait until its observer has persisted it."""

        snapshot = self.cancel(job_id, owner_user_id=owner_user_id)
        if snapshot is None:
            return None
        return await self.wait(job_id, owner_user_id=owner_user_id)

    async def wait(
        self,
        job_id: str,
        *,
        owner_user_id: str,
    ) -> JobSnapshot | None:
        """Wait for work and terminal persistence without exposing internals."""

        job = self._owned_job(job_id, owner_user_id)
        if job is None:
            return None
        if job.task is not None:
            await asyncio.shield(job.task)
        return job.snapshot()

    async def _run(self, job: _Job, work: JobWork) -> None:
        async def update(stage: str, progress: float, message: str) -> None:
            if job.cancel_requested:
                raise asyncio.CancelledError
            job.state = JobState.RUNNING
            job.stage = stage
            job.progress = max(job.progress, min(float(progress), 0.99))
            job.message = message
            job.touch()
            job.record_event()
            await self._notify_observer(job)

        try:
            await update("starting", 0.02, "正在启动分析")
            result = await work(update)
            if job.cancel_requested:
                await self._publish_terminal(job, JobState.CANCELLED)
                return
            await self._publish_terminal(
                job,
                JobState.COMPLETED,
                result=result,
            )
        except asyncio.CancelledError:
            await self._publish_terminal(job, JobState.CANCELLED)
        except Exception as exc:
            if job.cancel_requested:
                await self._publish_terminal(job, JobState.CANCELLED)
            else:
                await self._publish_terminal(
                    job,
                    JobState.FAILED,
                    error=(
                        str(exc).strip() or exc.__class__.__name__
                    )[:1000],
                )
        finally:
            self._prune()

    async def _notify_observer(self, job: _Job) -> None:
        persistence_error = await self._observe(
            job,
            job.snapshot(),
            job.result,
        )
        if persistence_error != job.persistence_error:
            job.persistence_error = persistence_error
            job.touch()

    async def _publish_terminal(
        self,
        job: _Job,
        state: JobState,
        *,
        result: AnalysisResult | None = None,
        error: str | None = None,
    ) -> None:
        """Persist a terminal candidate before publishing it to readers.

        ``get`` and SSE continue to observe the last non-terminal revision while
        the observer is running. This keeps the in-memory terminal state and
        its durable history row causally consistent.
        """

        if job.state in {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }:
            return
        if state == JobState.COMPLETED:
            stage = "completed"
            progress = 1.0
            message = "分析完成"
        elif state == JobState.CANCELLED:
            stage = "cancelled"
            progress = job.progress
            message = "任务已取消"
            result = None
            error = None
        else:
            stage = "failed"
            progress = job.progress
            message = "分析失败"
            result = None

        published_at = datetime.now(UTC)
        candidate = JobSnapshot(
            id=job.id,
            state=state,
            stage=stage,
            progress=progress,
            message=message,
            created_at=job.created_at,
            updated_at=published_at,
            result_url=(
                f"/jobs/{job.id}/result"
                if state == JobState.COMPLETED
                else None
            ),
            error=error,
            persistence_error=job.persistence_error,
            revision=job.revision + 1,
        )
        # Once a terminal observer has committed its candidate, that durable
        # terminal state wins over cancellation arriving during the commit.
        persistence_error = await self._observe(
            job,
            candidate,
            result,
            settle_cancellation=True,
        )

        job.state = state
        job.stage = stage
        job.progress = progress
        job.message = message
        job.updated_at = published_at
        job.result = result
        job.error = error
        job.persistence_error = persistence_error
        job.revision = candidate.revision
        job.record_event()

    async def _observe(
        self,
        job: _Job,
        snapshot: JobSnapshot,
        result: AnalysisResult | None,
        *,
        settle_cancellation: bool = False,
    ) -> str | None:
        if job.observer is None:
            return None
        async with job.observer_lock:
            try:
                observed = job.observer(snapshot, result)
                if inspect.isawaitable(observed):
                    observer_task = asyncio.ensure_future(observed)
                    try:
                        await asyncio.shield(observer_task)
                    except asyncio.CancelledError as cancelled:
                        # Keep shielding through repeated cancellation so no
                        # newer state can overtake the durable observer write.
                        persistence_error: str | None = None
                        while not observer_task.done():
                            try:
                                await asyncio.shield(observer_task)
                            except asyncio.CancelledError:
                                continue
                            except Exception as exc:
                                persistence_error = self._persistence_error(
                                    exc
                                )
                                break
                        if (
                            observer_task.done()
                            and not observer_task.cancelled()
                        ):
                            try:
                                observer_task.result()
                            except Exception as exc:
                                persistence_error = self._persistence_error(
                                    exc
                                )
                        if settle_cancellation:
                            return persistence_error
                        raise cancelled
                return None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return self._persistence_error(exc)

    @staticmethod
    def _persistence_error(exc: Exception) -> str:
        return (str(exc).strip() or exc.__class__.__name__)[:1000]

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

    def raw_revision(
        self,
        job_id: str,
        *,
        owner_user_id: str,
    ) -> int | None:
        job = self._owned_job(job_id, owner_user_id)
        return job.revision if job else None

    @staticmethod
    def _require_owner(owner_user_id: str) -> str:
        if not isinstance(owner_user_id, str) or not owner_user_id.strip():
            raise ValueError("owner_user_id is required for job access")
        return owner_user_id


def snapshot_event(snapshot: JobSnapshot) -> str:
    payload: dict[str, Any] = snapshot.model_dump(mode="json")
    return f"event: progress\ndata: {JobSnapshot(**payload).model_dump_json()}\n\n"
