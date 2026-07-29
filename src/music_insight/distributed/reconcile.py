from __future__ import annotations

import asyncio

from starlette.concurrency import run_in_threadpool

from music_insight.api.history import (
    HistoryEntryNotFoundError,
    HistoryStore,
)
from music_insight.api.jobs import JobState
from music_insight.distributed.jobs import RedisAnalysisJobStore


async def reconcile_terminal_history_once(
    jobs: RedisAnalysisJobStore,
    history: HistoryStore,
    *,
    limit: int = 50,
) -> int:
    """Materialize worker terminal results through the API's SQLite writer."""

    reconciled = 0
    for job_id in await jobs.pending_terminal_job_ids(limit):
        payload = await jobs.payload(job_id)
        if payload is None:
            await jobs.acknowledge_terminal(job_id)
            continue
        snapshot = await jobs.get(
            job_id,
            owner_user_id=payload.owner_user_id,
        )
        if snapshot is None:
            await jobs.acknowledge_terminal(job_id)
            continue
        if snapshot.state not in {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }:
            # A stale notification cannot publish a non-terminal row.
            await jobs.acknowledge_terminal(job_id)
            continue
        result = (
            await jobs.result(
                job_id,
                owner_user_id=payload.owner_user_id,
            )
            if snapshot.state == JobState.COMPLETED
            else None
        )
        try:
            await run_in_threadpool(
                history.update,
                job_id,
                state=snapshot.state.value,
                updated_at=snapshot.updated_at,
                result=result,
                error=snapshot.error,
                user_id=payload.owner_user_id,
            )
        except HistoryEntryNotFoundError:
            # The owner may have deleted a cancelled history row between the
            # cancellation response and this reconciliation pass.
            await jobs.acknowledge_terminal(job_id)
            continue
        except Exception:
            # Keep the sorted-set entry for an at-least-once retry.
            continue
        await jobs.acknowledge_terminal(job_id)
        reconciled += 1
    return reconciled


async def reconcile_terminal_history(
    jobs: RedisAnalysisJobStore,
    history: HistoryStore,
    *,
    interval_seconds: float = 0.5,
) -> None:
    while True:
        await reconcile_terminal_history_once(jobs, history)
        await asyncio.sleep(interval_seconds)
