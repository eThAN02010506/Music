from __future__ import annotations

from celery import Celery

from music_insight.config import Settings


TASK_NAME = "music_insight.analysis.run"


def create_celery_app(settings: Settings | None = None) -> Celery:
    configured = settings or Settings()
    app = Celery(
        "music_insight",
        broker=configured.redis_url,
        backend=configured.redis_url,
        include=["music_insight.distributed.worker"],
    )
    visibility_timeout = configured.celery_visibility_timeout_seconds
    app.conf.update(
        accept_content=["json"],
        broker_connection_retry_on_startup=True,
        broker_transport_options={
            "visibility_timeout": visibility_timeout,
        },
        result_backend_transport_options={
            "visibility_timeout": visibility_timeout,
            "global_keyprefix": (
                f"{configured.redis_key_prefix}:celery-result:"
            ),
        },
        result_expires=configured.redis_job_ttl_seconds,
        result_serializer="json",
        task_acks_late=True,
        task_default_queue=configured.celery_queue_name,
        task_reject_on_worker_lost=True,
        task_routes={
            TASK_NAME: {"queue": configured.celery_queue_name},
        },
        task_serializer="json",
        task_track_started=True,
        visibility_timeout=visibility_timeout,
        worker_prefetch_multiplier=1,
    )
    return app


celery_app = create_celery_app()
