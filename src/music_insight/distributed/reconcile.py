from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from redis.exceptions import RedisError
from starlette.concurrency import run_in_threadpool

from music_insight.api.history import (
    HistoryEntryNotFoundError,
    HistoryStore,
)
from music_insight.api.jobs import JobState
from music_insight.distributed.jobs import (
    DistributedJobUnavailable,
    RedisAnalysisJobStore,
)


_MAX_BACKOFF_SECONDS = 30.0
_INITIAL_BACKOFF_SECONDS = 0.5
_STATUS_KEY_SUFFIX = "reconcile_status"


async def reconcile_terminal_history_once(
    jobs: RedisAnalysisJobStore,
    history: HistoryStore,
    *,
    limit: int = 50,
    stale_after_seconds: float | None = None,
) -> int:
    """Materialize worker terminal results through the API's SQLite writer.

    Every Redis read is isolated per job so one unavailable backend or one
    corrupt record cannot kill the whole reconciler loop. A corrupt record is
    left in the sorted set (not acknowledged) so it is retried after TTL expiry
    instead of being dropped silently.
    """

    reconciled = 0
    corrupt = 0
    try:
        pending = await jobs.pending_terminal_job_ids(limit)
    except DistributedJobUnavailable:
        await _publish_status(jobs, error="pending list unavailable")
        return 0
    for job_id in pending:
        try:
            progressed = await _reconcile_one(
                jobs,
                history,
                job_id,
            )
        except DistributedJobUnavailable:
            await _publish_status(
                jobs,
                error=f"job {job_id} backend unavailable",
            )
            return reconciled
        except ValidationError:
            # Corrupt JSON never becomes valid by retrying immediately; leave
            # it for the TTL expiry so we do not busy-loop on one record.
            corrupt += 1
            continue
        except HistoryEntryNotFoundError:
            # The owner may have deleted the history row between the
            # cancellation response and this reconciliation pass.
            await jobs.acknowledge_terminal(job_id)
            continue
        except RedisError:
            # A redis outage surfaced from inside _reconcile_one (e.g. the
            # finish/acknowledge write path). Do not swallow it into the generic
            # at-least-once retry bucket: report and stop this pass so the
            # backing loop backs off instead of hot-looping.
            await _publish_status(
                jobs,
                error=f"job {job_id} write unavailable",
            )
            return reconciled
        except Exception:
            # Keep the sorted-set entry for an at-least-once retry.
            continue
        reconciled += progressed
    if stale_after_seconds is not None:
        settled = await _settle_stale_active_jobs(
            jobs,
            history,
            stale_after_seconds=stale_after_seconds,
        )
        reconciled += settled
    if corrupt:
        await _publish_status(jobs, error=f"{corrupt} corrupt record(s) skipped")
    else:
        await _publish_status(jobs)
    return reconciled


async def _settle_stale_active_jobs(
    jobs: RedisAnalysisJobStore,
    history: HistoryStore,
    *,
    stale_after_seconds: float,
) -> int:
    """Fail running jobs whose worker has been silent past the lease window.

    The terminal sorted set only receives jobs that already reached a terminal
    state, so a worker that died mid-run would leave its job "running" forever
    and never be seen by the normal reconcile pass. Scanning the active set
    catches those orphans.
    """

    settled = 0
    try:
        active_ids = await jobs.active_job_ids()
    except (DistributedJobUnavailable, RedisError):
        await _publish_status(jobs, error="active list unavailable")
        return 0
    cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
    for job_id in active_ids:
        try:
            payload = await jobs.payload(job_id)
            if payload is None:
                continue
            snapshot = await jobs.get(
                job_id,
                owner_user_id=payload.owner_user_id,
            )
            if snapshot is None or snapshot.state in {
                JobState.COMPLETED,
                JobState.FAILED,
                JobState.CANCELLED,
            }:
                continue
            if snapshot.updated_at > cutoff:
                # Worker is still reporting progress; leave it alone.
                continue
            await jobs.finish(
                job_id,
                payload.owner_user_id,
                JobState.FAILED,
                error="分析超时或 Worker 失联，任务已自动结算。",
            )
            await jobs.acknowledge_terminal(job_id)
            settled += 1
        except (DistributedJobUnavailable, RedisError):
            await _publish_status(
                jobs,
                error=f"job {job_id} stale check unavailable",
            )
            return settled
        except ValidationError:
            continue
        except Exception:
            continue
    return settled


async def _reconcile_one(
    jobs: RedisAnalysisJobStore,
    history: HistoryStore,
    job_id: str,
) -> int:
    payload = await jobs.payload(job_id)
    if payload is None:
        await jobs.acknowledge_terminal(job_id)
        return 0
    snapshot = await jobs.get(
        job_id,
        owner_user_id=payload.owner_user_id,
    )
    if snapshot is None:
        await jobs.acknowledge_terminal(job_id)
        return 0
    if snapshot.state not in {
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.CANCELLED,
    }:
        # The terminal sorted set should only contain terminal jobs; a
        # non-terminal entry is a stale notification and cannot be published.
        await jobs.acknowledge_terminal(job_id)
        return 0
    result = (
        await jobs.result(
            job_id,
            owner_user_id=payload.owner_user_id,
        )
        if snapshot.state == JobState.COMPLETED
        else None
    )
    existing = await run_in_threadpool(
        history.get,
        job_id,
        user_id=payload.owner_user_id,
    )
    if (
        existing is not None
        and existing.state in {"completed", "failed", "cancelled"}
    ):
        # The SQLite row already reached a terminal state (e.g. a rename or
        # another coordinator pass won), so writing again would roll back the
        # user-visible updated_at. Acknowledge and leave the row alone.
        await jobs.acknowledge_terminal(job_id)
        return 0
    await run_in_threadpool(
        history.update,
        job_id,
        state=snapshot.state.value,
        updated_at=snapshot.updated_at,
        result=result,
        error=snapshot.error,
        user_id=payload.owner_user_id,
    )
    await jobs.acknowledge_terminal(job_id)
    return 1


async def reconcile_terminal_history(
    jobs: RedisAnalysisJobStore,
    history: HistoryStore,
    *,
    interval_seconds: float = 0.5,
    stale_after_seconds: float | None = None,
) -> None:
    delay = _INITIAL_BACKOFF_SECONDS
    while True:
        try:
            await reconcile_terminal_history_once(
                jobs,
                history,
                stale_after_seconds=stale_after_seconds,
            )
            delay = _INITIAL_BACKOFF_SECONDS
        except Exception:
            # The loop must survive transient failures; back off exponentially
            # instead of spinning or dying.
            delay = min(delay * 2, _MAX_BACKOFF_SECONDS)
        await asyncio.sleep(delay)


def _status_key(jobs: RedisAnalysisJobStore) -> str:
    raw = jobs._tag if hasattr(jobs, "_tag") else "music-insight"
    return f"{raw.strip('{}')}:{_STATUS_KEY_SUFFIX}"


async def _publish_status(
    jobs: RedisAnalysisJobStore,
    *,
    error: str | None = None,
) -> None:
    """Record reconciler health so a dead loop is visible in the debug console."""

    try:
        now = datetime.now(UTC).isoformat()
        pipe = jobs.client.pipeline(transaction=False)
        pipe.hset(_status_key(jobs), "last_ok_at", now)
        if error:
            pipe.hset(_status_key(jobs), "last_error", error[:500])
        pipe.expire(_status_key(jobs), int(jobs.ttl_seconds))
        await pipe.execute()
    except Exception:
        # Status reporting must never take down the reconciler itself.
        return
