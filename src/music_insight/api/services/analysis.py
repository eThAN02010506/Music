from __future__ import annotations

import asyncio
from pathlib import Path
import time
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

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
from music_insight.schemas import AnalysisResult

from .uploads import save_audio_upload


async def _settle_despite_cancellation(
    task: asyncio.Task,
):
    """Wait for a child operation while preserving the caller's cancellation.

    ``asyncio.to_thread`` and AnyIO's thread pool cannot stop a function that
    has already begun. Compensation must therefore wait for that function to
    finish before deleting the row or file it may still create.
    """

    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


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

    try:
        await ensure_capacity(jobs, user_id)
    except JobCapacityError as exc:
        raise HTTPException(
            status_code=503 if exc.global_limit else 429,
            detail=str(exc),
        ) from exc

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

    ready = asyncio.Event()
    snapshot: JobSnapshot | None = None
    last_persisted_stage: str | None = None
    last_persisted_progress = -1.0
    last_persisted_at = 0.0

    async def work(update):
        await ready.wait()
        return await orchestrator.analyze(asset, progress=update)

    async def observe(
        current: JobSnapshot,
        result: AnalysisResult | None,
    ) -> None:
        nonlocal last_persisted_at
        nonlocal last_persisted_progress
        nonlocal last_persisted_stage
        if not ready.is_set():
            return
        now = time.monotonic()
        terminal = current.state in {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
        should_persist = (
            terminal
            or current.stage != last_persisted_stage
            or current.progress - last_persisted_progress >= 0.05
            or now - last_persisted_at >= 2
        )
        if not should_persist:
            return
        await run_in_threadpool(
            history.update,
            current.id,
            state=current.state.value,
            updated_at=current.updated_at,
            result=result if current.state == JobState.COMPLETED else None,
            error=current.error,
            user_id=user_id,
        )
        last_persisted_stage = current.stage
        last_persisted_progress = current.progress
        last_persisted_at = now

    creation_task: asyncio.Task | None = None
    try:
        snapshot = jobs.create(
            work,
            observer=observe,
            owner_user_id=user_id,
        )
        creation_task = asyncio.create_task(
            run_in_threadpool(
                history.create,
                job_id=snapshot.id,
                title=Path(file_name).stem,
                file_name=file_name,
                language=language,
                state=snapshot.state.value,
                created_at=snapshot.created_at,
                updated_at=snapshot.updated_at,
                audio_path=asset.path,
                model_source=model_source,
                model_location=(
                    local_model_path
                    if model_source == "local"
                    else (model_endpoint or settings.omni_endpoint)
                ),
                user_id=user_id,
            ),
        )
        await asyncio.shield(creation_task)
    except BaseException as exc:
        # Cancellation of the request does not stop an in-flight SQLite thread.
        # Settle it before compensation so a late INSERT cannot resurrect the
        # history row after cleanup.
        if creation_task is not None:
            try:
                await _settle_despite_cancellation(creation_task)
            except BaseException:
                pass

        async def compensate() -> None:
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
                    asset.path.unlink(missing_ok=True)
                except OSError:
                    pass

        cleanup_task = asyncio.create_task(compensate())
        await _settle_despite_cancellation(cleanup_task)
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, JobCapacityError):
            raise HTTPException(
                status_code=503 if exc.global_limit else 429,
                detail=str(exc),
            ) from exc
        raise HTTPException(
            status_code=500,
            detail="Unable to create analysis job.",
        ) from exc

    ready.set()
    return snapshot


async def _submit_distributed_analysis_job(
    *,
    asset,
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
    payload = DistributedAnalysisPayload(
        job_id=job_id,
        owner_user_id=user_id,
        asset=asset.model_copy(update={"path": asset.path.resolve()}),
        model_source=model_source,
        model_endpoint=model_endpoint,
        local_model_path=local_model_path,
    )
    snapshot: JobSnapshot | None = None
    history_created = False
    history_task: asyncio.Task | None = None
    enqueue_task: asyncio.Task | None = None
    try:
        snapshot = await jobs.create_distributed(payload)
        history_task = asyncio.create_task(
            run_in_threadpool(
                history.create,
                job_id=snapshot.id,
                title=Path(file_name).stem,
                file_name=file_name,
                language=language,
                state=snapshot.state.value,
                created_at=snapshot.created_at,
                updated_at=snapshot.updated_at,
                audio_path=asset.path.resolve(),
                model_source=model_source,
                model_location=(
                    local_model_path
                    if model_source == "local"
                    else (model_endpoint or settings.omni_endpoint)
                ),
                user_id=user_id,
            ),
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
        for task in (history_task, enqueue_task):
            if task is None:
                continue
            try:
                await _settle_despite_cancellation(task)
                if task is history_task:
                    history_created = True
            except BaseException:
                pass
        if snapshot is None:
            try:
                snapshot = await jobs.get(
                    job_id,
                    owner_user_id=user_id,
                )
            except Exception:
                pass
        if snapshot is not None:
            try:
                await cancel_job(
                    jobs,
                    snapshot.id,
                    owner_user_id=user_id,
                )
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
                await remove_job(
                    jobs,
                    snapshot.id,
                    owner_user_id=user_id,
                )
            except Exception:
                pass
        asset.path.unlink(missing_ok=True)
        if not isinstance(exc, Exception):
            raise
        raise HTTPException(
            status_code=503,
            detail="Unable to enqueue distributed analysis job.",
        ) from exc


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
