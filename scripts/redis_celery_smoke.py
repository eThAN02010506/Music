from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from music_insight.api.jobs import JobState
from music_insight.config import Settings
from music_insight.distributed.celery_app import TASK_NAME, create_celery_app
from music_insight.distributed.jobs import RedisAnalysisJobStore
from music_insight.distributed.payloads import DistributedAnalysisPayload
from music_insight.schemas import AudioAsset


async def run() -> None:
    settings = Settings()
    if settings.job_backend != "redis" or settings.shared_audio_dir is None:
        raise RuntimeError(
            "Set redis job backend and an absolute shared audio directory."
        )
    jobs = RedisAnalysisJobStore.from_settings(settings)
    await jobs.initialize()
    job_id = f"smoke-{uuid4().hex}"
    missing_audio = (
        settings.shared_audio_dir
        / "users"
        / "smoke-owner"
        / "uploads"
        / "intentionally-missing.wav"
    )
    payload = DistributedAnalysisPayload(
        job_id=job_id,
        owner_user_id="smoke-owner",
        asset=AudioAsset(
            path=Path(missing_audio),
            media_type="audio/wav",
            size_bytes=1,
            max_duration_s=60,
        ),
        model_source="network",
        model_endpoint=settings.omni_endpoint,
    )
    queue = create_celery_app(settings)
    try:
        await jobs.create_distributed(payload)
        await asyncio.to_thread(
            queue.send_task,
            TASK_NAME,
            args=[job_id],
            task_id=job_id,
            queue=settings.celery_queue_name,
        )
        for _ in range(150):
            snapshot = await jobs.get(
                job_id,
                owner_user_id=payload.owner_user_id,
            )
            if snapshot is not None and snapshot.state in {
                JobState.COMPLETED,
                JobState.FAILED,
                JobState.CANCELLED,
            }:
                if snapshot.state != JobState.FAILED:
                    raise AssertionError(
                        f"Expected validation failure, got {snapshot.state}"
                    )
                print(
                    f"Redis/Celery multi-process smoke passed: {job_id} "
                    f"-> {snapshot.state.value}"
                )
                return
            await asyncio.sleep(0.1)
        raise TimeoutError("Celery worker did not publish a terminal state.")
    finally:
        await jobs.cancel(job_id, owner_user_id=payload.owner_user_id)
        await jobs.remove(job_id, owner_user_id=payload.owner_user_id)
        await jobs.acknowledge_terminal(job_id)
        await jobs.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
