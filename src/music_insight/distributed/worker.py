from __future__ import annotations

import asyncio

from celery.exceptions import Ignore

from music_insight.api.jobs import JobState
from music_insight.api.orchestrator_factory import (
    build_orchestrator,
    create_local_server,
)
from music_insight.config import Settings
from music_insight.distributed.celery_app import TASK_NAME, celery_app
from music_insight.distributed.jobs import (
    DistributedJobCancelled,
    RedisAnalysisJobStore,
)
from music_insight.distributed.payloads import DistributedAnalysisPayload
from music_insight.pipeline.resources import model_resources
from music_insight.schemas import AudioAsset
from music_insight.storage.assets import content_cache_key


def _validated_worker_asset(
    payload: DistributedAnalysisPayload,
    settings: Settings,
) -> AudioAsset:
    if settings.shared_audio_dir is None:
        raise ValueError("Distributed worker has no shared audio directory.")
    workspace = settings.shared_audio_dir.resolve()
    expected_root = (
        workspace / "users" / payload.owner_user_id / "uploads"
    ).resolve()
    path = payload.asset.path.resolve(strict=True)
    if not path.is_relative_to(expected_root):
        raise ValueError("Queued audio path is outside the owner's upload root.")
    size = path.stat().st_size
    if size != payload.asset.size_bytes:
        raise ValueError("Queued audio changed after submission.")
    if size > settings.max_upload_mb * 1024 * 1024:
        raise ValueError("Queued audio exceeds the configured size limit.")
    if payload.content_key:
        actual = content_cache_key(path)
        if actual != payload.content_key:
            raise ValueError(
                "Queued audio content does not match the submission hash; "
                "the file may have been replaced."
            )
    return payload.asset.model_copy(update={"path": path})


async def run_distributed_analysis(
    job_id: str,
    settings: Settings | None = None,
) -> dict[str, str]:
    configured = settings or Settings()
    jobs = RedisAnalysisJobStore.from_settings(configured)
    payload: DistributedAnalysisPayload | None = None
    local_server = None
    try:
        await jobs.initialize()
        payload = await jobs.payload(job_id)
        if payload is None:
            raise LookupError(f"Distributed job not found: {job_id}")
        local_server = (
            create_local_server(configured)
            if payload.model_source == "local"
            else None
        )
        snapshot = await jobs.get(
            payload.job_id,
            owner_user_id=payload.owner_user_id,
        )
        if snapshot is None:
            raise LookupError(f"Distributed job not found: {payload.job_id}")
        if snapshot.state.value in {"completed", "failed", "cancelled"}:
            return {"job_id": payload.job_id, "state": snapshot.state.value}

        asset = _validated_worker_asset(payload, configured)
        started = await jobs.mark_running(
            payload.job_id,
            payload.owner_user_id,
        )
        if not started:
            snapshot = await jobs.get(
                payload.job_id,
                owner_user_id=payload.owner_user_id,
            )
            return {
                "job_id": payload.job_id,
                "state": snapshot.state.value if snapshot else "missing",
            }

        async def progress(stage: str, value: float, message: str) -> None:
            await jobs.update_progress(
                payload.job_id,
                payload.owner_user_id,
                stage,
                value,
                message,
            )

        orchestrator = build_orchestrator(
            configured,
            model_source=payload.model_source,
            model_endpoint=payload.model_endpoint,
            local_model_path=payload.local_model_path,
            local_server=local_server,
        )
        result = await orchestrator.analyze(asset, progress=progress)
        terminal = await jobs.finish(
            payload.job_id,
            payload.owner_user_id,
            state=JobState.COMPLETED,
            result=result,
        )
        return {
            "job_id": payload.job_id,
            "state": terminal.state.value if terminal else "missing",
        }
    except DistributedJobCancelled:
        if payload is None:
            raise RuntimeError(
                "Distributed cancellation arrived before payload loading."
            ) from None
        await jobs.finish(
            payload.job_id,
            payload.owner_user_id,
            JobState.CANCELLED,
        )
        return {"job_id": payload.job_id, "state": "cancelled"}
    except Exception as exc:
        if payload is None:
            raise
        error = (str(exc).strip() or exc.__class__.__name__)[:1000]
        await jobs.finish(
            payload.job_id,
            payload.owner_user_id,
            JobState.FAILED,
            error=error,
        )
        raise
    finally:
        if local_server is not None:
            await local_server.aclose()
        model_resources.clear_current_loop()
        await jobs.shutdown()


@celery_app.task(
    bind=True,
    name=TASK_NAME,
    acks_late=True,
    reject_on_worker_lost=True,
)
def analyze_audio_task(self, job_id: str) -> dict[str, str]:
    try:
        return asyncio.run(run_distributed_analysis(job_id))
    except LookupError as exc:
        # A compensated or expired job must not be retried indefinitely.
        raise Ignore from exc
