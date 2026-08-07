from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import time
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from music_insight.async_utils import settle_despite_cancellation
from music_insight.api.history import HistoryStore
from music_insight.api.job_access import (
    cancel_job,
    ensure_capacity,
    is_memory_store,
    remove_job,
)
from music_insight.api.jobs import (
    JobCapacityError,
    JobSnapshot,
    JobState,
)
from music_insight.config import Settings
from music_insight.distributed.payloads import DistributedAnalysisPayload
from music_insight.pipeline.orchestrator import AnalysisOrchestrator
from music_insight.reporting.markdown import render_markdown_report
from music_insight.schemas import AnalysisResult, AudioAsset
from music_insight.storage.assets import content_cache_key

from .uploads import save_audio_upload


async def submit_analysis_job(
    *,
    file: UploadFile,
    language: str | None,
    model_source: str,
    model_endpoint: str | None,
    local_model_path: str | None,
    settings: Settings,
    history: HistoryStore,
    jobs,
    user_id: str,
    orchestrator: AnalysisOrchestrator,
    task_queue=None,
) -> JobSnapshot:
    """Persist an upload and register its job with compensating cleanup.

    ``AnalysisJobStore`` owns job identifiers, so the job must be allocated
    before its history row can be inserted. The work is gated until both
    registrations succeed; any failure cancels/removes the job and upload.
    """

    await _ensure_submission_capacity(jobs, user_id)
    file_name = file.filename or "audio"
    asset = await save_audio_upload(file, language, settings, user_id)
    if not is_memory_store(jobs):
        return await _submit_distributed_analysis_job(
            asset=asset,
            file_name=file_name,
            language=language,
            model_source=model_source,
            model_endpoint=model_endpoint,
            local_model_path=local_model_path,
            settings=settings,
            history=history,
            jobs=jobs,
            user_id=user_id,
            task_queue=task_queue,
        )
    return await _submit_memory_analysis_job(
        asset=asset,
        file_name=file_name,
        language=language,
        model_source=model_source,
        model_endpoint=model_endpoint,
        local_model_path=local_model_path,
        settings=settings,
        history=history,
        jobs=jobs,
        user_id=user_id,
        orchestrator=orchestrator,
    )


async def _ensure_submission_capacity(jobs, user_id: str) -> None:
    try:
        await ensure_capacity(jobs, user_id)
    except JobCapacityError as exc:
        raise HTTPException(
            status_code=503 if exc.global_limit else 429,
            detail=str(exc),
        ) from exc


@dataclass(slots=True)
class _HistoryProgressObserver:
    ready: asyncio.Event
    history: HistoryStore
    user_id: str
    last_stage: str | None = None
    last_progress: float = -1.0
    last_persisted_at: float = 0.0

    async def __call__(
        self,
        current: JobSnapshot,
        result: AnalysisResult | None,
    ) -> None:
        if not self.ready.is_set():
            return
        now = time.monotonic()
        terminal = current.state in {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
        should_persist = (
            terminal
            or current.stage != self.last_stage
            or current.progress - self.last_progress >= 0.05
            or now - self.last_persisted_at >= 2
        )
        if not should_persist:
            return
        await self._persist(current, result)
        self.last_stage = current.stage
        self.last_progress = current.progress
        self.last_persisted_at = now

    async def _persist(
        self,
        current: JobSnapshot,
        result: AnalysisResult | None,
    ) -> None:
        """Write a terminal state with bounded retries.

        The in-memory job backend has no reconciler to backfill a lost terminal
        row, so a transient SQLite write failure on a completed/failed/
        cancelled state must be retried rather than dropped. Progress-only
        writes are best-effort (the next progress tick re-persists them).
        """

        terminal = current.state in {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
        attempts = 3 if terminal else 1
        delay = 0.2
        last_exc: BaseException | None = None
        for attempt in range(attempts):
            try:
                await run_in_threadpool(
                    self.history.update,
                    current.id,
                    state=current.state.value,
                    updated_at=current.updated_at,
                    result=(
                        result if current.state == JobState.COMPLETED else None
                    ),
                    error=current.error,
                    user_id=self.user_id,
                )
                return
            except BaseException as exc:  # noqa: BLE001 - surfaced via _observe
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 1.0)
        if last_exc is not None:
            raise last_exc


async def _submit_memory_analysis_job(
    *,
    asset: AudioAsset,
    file_name: str,
    language: str | None,
    model_source: str,
    model_endpoint: str | None,
    local_model_path: str | None,
    settings: Settings,
    history: HistoryStore,
    jobs,
    user_id: str,
    orchestrator: AnalysisOrchestrator,
) -> JobSnapshot:
    ready = asyncio.Event()
    snapshot: JobSnapshot | None = None

    async def work(update):
        await ready.wait()
        return await orchestrator.analyze(asset, progress=update)

    creation_task: asyncio.Task | None = None
    try:
        snapshot = jobs.create(
            work,
            observer=_HistoryProgressObserver(ready, history, user_id),
            owner_user_id=user_id,
        )
        creation_task = _create_history_task(
            history,
            snapshot=snapshot,
            asset_path=asset.path,
            file_name=file_name,
            language=language,
            model_source=model_source,
            model_endpoint=model_endpoint,
            local_model_path=local_model_path,
            settings=settings,
            user_id=user_id,
        )
        await asyncio.shield(creation_task)
    except BaseException as exc:
        # Cancellation of the request does not stop an in-flight SQLite thread.
        # Settle it before compensation so a late INSERT cannot resurrect the
        # history row after cleanup.
        if creation_task is not None:
            try:
                await settle_despite_cancellation(creation_task)
            except BaseException:
                pass
        cleanup_task = asyncio.create_task(
            _compensate_memory_submission(
                snapshot=snapshot,
                asset_path=asset.path,
                history=history,
                jobs=jobs,
                user_id=user_id,
            )
        )
        await settle_despite_cancellation(cleanup_task)
        _raise_memory_submission_error(exc)

    ready.set()
    return snapshot


def _create_history_task(
    history: HistoryStore,
    *,
    snapshot: JobSnapshot,
    asset_path: Path,
    file_name: str,
    language: str | None,
    model_source: str,
    model_endpoint: str | None,
    local_model_path: str | None,
    settings: Settings,
    user_id: str,
) -> asyncio.Task:
    return asyncio.create_task(
        run_in_threadpool(
            history.create,
            job_id=snapshot.id,
            title=Path(file_name).stem,
            file_name=file_name,
            language=language,
            state=snapshot.state.value,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            audio_path=asset_path,
            model_source=model_source,
            model_location=(
                local_model_path
                if model_source == "local"
                else (model_endpoint or settings.omni_endpoint)
            ),
            user_id=user_id,
        )
    )


async def _compensate_memory_submission(
    *,
    snapshot: JobSnapshot | None,
    asset_path: Path,
    history: HistoryStore,
    jobs,
    user_id: str,
) -> None:
    try:
        if snapshot is None:
            return
        try:
            await jobs.cancel_and_wait(
                snapshot.id,
                owner_user_id=user_id,
            )
        except Exception:
            pass
        try:
            await run_in_threadpool(
                history.delete,
                snapshot.id,
                user_id=user_id,
            )
        except Exception:
            # Preserve the triggering exception. Any partial row stays
            # owner-scoped and startup recovery marks it interrupted.
            pass
        jobs.remove(snapshot.id, owner_user_id=user_id)
    finally:
        try:
            asset_path.unlink(missing_ok=True)
        except OSError:
            pass


def _raise_memory_submission_error(exc: BaseException) -> None:
    if not isinstance(exc, Exception):
        raise exc
    if isinstance(exc, JobCapacityError):
        raise HTTPException(
            status_code=503 if exc.global_limit else 429,
            detail=str(exc),
        ) from exc
    raise HTTPException(
        status_code=500,
        detail="Unable to create analysis job.",
    ) from exc


async def _submit_distributed_analysis_job(
    *,
    asset: AudioAsset,
    file_name: str,
    language: str | None,
    model_source: str,
    model_endpoint: str | None,
    local_model_path: str | None,
    settings: Settings,
    history: HistoryStore,
    jobs,
    user_id: str,
    task_queue,
) -> JobSnapshot:
    """Atomically compensate Redis, history and upload around enqueueing."""

    if task_queue is None:
        asset.path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=503,
            detail="Distributed task queue is not configured.",
        )
    job_id = uuid4().hex
    resolved_path = asset.path.resolve()
    # Hashing a large normalized WAV blocks the event loop; run it in the
    # threadpool so an upload submission cannot stall unrelated requests.
    content_key = await run_in_threadpool(content_cache_key, resolved_path)
    payload = DistributedAnalysisPayload(
        job_id=job_id,
        owner_user_id=user_id,
        asset=asset.model_copy(update={"path": resolved_path}),
        model_source=model_source,
        model_endpoint=model_endpoint,
        local_model_path=local_model_path,
        content_key=content_key,
    )
    snapshot: JobSnapshot | None = None
    history_created = False
    history_task: asyncio.Task | None = None
    enqueue_task: asyncio.Task | None = None
    try:
        snapshot = await jobs.create_distributed(payload)
        history_task = _create_history_task(
            history,
            snapshot=snapshot,
            asset_path=asset.path.resolve(),
            file_name=file_name,
            language=language,
            model_source=model_source,
            model_endpoint=model_endpoint,
            local_model_path=local_model_path,
            settings=settings,
            user_id=user_id,
        )
        await asyncio.shield(history_task)
        history_created = True
        enqueue_task = asyncio.create_task(
            run_in_threadpool(
                task_queue.send_task,
                "music_insight.analysis.run",
                args=[snapshot.id],
                task_id=snapshot.id,
                queue=settings.celery_queue_name,
            )
        )
        await asyncio.shield(enqueue_task)
        return snapshot
    except JobCapacityError as exc:
        asset.path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=503 if exc.global_limit else 429,
            detail=str(exc),
        ) from exc
    except BaseException as exc:
        history_created = await _settle_distributed_submission_tasks(
            history_task,
            enqueue_task,
            history_created=history_created,
        )
        snapshot = await _recover_distributed_snapshot(
            jobs,
            snapshot=snapshot,
            job_id=job_id,
            user_id=user_id,
        )
        await _compensate_distributed_submission(
            jobs,
            history,
            snapshot=snapshot,
            history_created=history_created,
            user_id=user_id,
        )
        asset.path.unlink(missing_ok=True)
        if not isinstance(exc, Exception):
            raise
        raise HTTPException(
            status_code=503,
            detail="Unable to enqueue distributed analysis job.",
        ) from exc


async def _settle_distributed_submission_tasks(
    history_task: asyncio.Task | None,
    enqueue_task: asyncio.Task | None,
    *,
    history_created: bool,
) -> bool:
    for task in (history_task, enqueue_task):
        if task is None:
            continue
        try:
            await settle_despite_cancellation(task)
            if task is history_task:
                history_created = True
        except BaseException:
            pass
    return history_created


async def _recover_distributed_snapshot(
    jobs,
    *,
    snapshot: JobSnapshot | None,
    job_id: str,
    user_id: str,
) -> JobSnapshot | None:
    if snapshot is not None:
        return snapshot
    try:
        return await jobs.get(job_id, owner_user_id=user_id)
    except Exception:
        return None


async def _compensate_distributed_submission(
    jobs,
    history: HistoryStore,
    *,
    snapshot: JobSnapshot | None,
    history_created: bool,
    user_id: str,
) -> None:
    if snapshot is None:
        return
    try:
        await cancel_job(jobs, snapshot.id, owner_user_id=user_id)
    except Exception:
        pass
    if history_created:
        try:
            await run_in_threadpool(
                history.delete,
                snapshot.id,
                user_id=user_id,
            )
        except Exception:
            pass
    try:
        await remove_job(jobs, snapshot.id, owner_user_id=user_id)
    except Exception:
        pass


async def analyze_upload(
    *,
    file: UploadFile,
    language: str | None,
    settings: Settings,
    user_id: str,
    orchestrator: AnalysisOrchestrator,
) -> AnalysisResult:
    asset = await save_audio_upload(
        file,
        language,
        settings,
        user_id,
        temporary=True,
    )
    try:
        return await orchestrator.analyze(asset)
    finally:
        asset.path.unlink(missing_ok=True)


async def analyze_upload_markdown(
    *,
    file: UploadFile,
    language: str | None,
    settings: Settings,
    user_id: str,
    orchestrator: AnalysisOrchestrator,
) -> str:
    result = await analyze_upload(
        file=file,
        language=language,
        settings=settings,
        user_id=user_id,
        orchestrator=orchestrator,
    )
    return render_markdown_report(result)
