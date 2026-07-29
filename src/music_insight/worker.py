from __future__ import annotations

import os
import sys

from music_insight.config import Settings
from music_insight.distributed.celery_app import celery_app


def main() -> None:
    settings = Settings()
    concurrency = os.getenv("MUSIC_INSIGHT_WORKER_CONCURRENCY", "1")
    celery_app.worker_main(
        [
            "worker",
            "--loglevel=INFO",
            f"--queues={settings.celery_queue_name}",
            f"--concurrency={concurrency}",
            "--prefetch-multiplier=1",
            *sys.argv[1:],
        ]
    )


if __name__ == "__main__":
    main()
