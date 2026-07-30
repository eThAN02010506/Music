from __future__ import annotations

from celery import Celery

from music_insight.config import Settings


TASK_NAME = "music_insight.analysis.run"


def create_celery_app(settings: Settings | None = None) -> Celery:
    configured = settings or Settings()
    app = Celery(
        "music_insight",
        broker=configured.redis_url,
        include=["music_insight.distributed.worker"],
    )
    visibility_timeout = configured.celery_visibility_timeout_seconds
    app.conf.update(
        accept_content=["json"],
        broker_connection_retry_on_startup=True,
        broker_transport_options={
            "visibility_timeout": visibility_timeout,
        },
        task_acks_late=True,
        task_acks_on_failure_or_timeout=True,
        task_default_queue=configured.celery_queue_name,
        task_ignore_result=True,
        task_reject_on_worker_lost=True,
        task_routes={
            TASK_NAME: {"queue": configured.celery_queue_name},
        },
        task_serializer="json",
        task_soft_time_limit=configured.celery_soft_time_limit_seconds,
        visibility_timeout=visibility_timeout,
        worker_cancel_long_running_tasks_on_connection_loss=True,
        worker_prefetch_multiplier=1,
    )
    return app


celery_app = create_celery_app()
