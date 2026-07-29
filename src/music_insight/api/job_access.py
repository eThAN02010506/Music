from __future__ import annotations

import inspect
from typing import Any

from music_insight.api.jobs import JobEvent, JobSnapshot
from music_insight.schemas import AnalysisResult


JobStore = Any


async def _call(store: JobStore, method: str, *args, **kwargs):
    value = getattr(store, method)(*args, **kwargs)
    if inspect.isawaitable(value):
        return await value
    return value


async def get_job(
    store: JobStore,
    job_id: str,
    *,
    owner_user_id: str,
) -> JobSnapshot | None:
    return await _call(
        store,
        "get",
        job_id,
        owner_user_id=owner_user_id,
    )


async def get_result(
    store: JobStore,
    job_id: str,
    *,
    owner_user_id: str,
) -> AnalysisResult | None:
    return await _call(
        store,
        "result",
        job_id,
        owner_user_id=owner_user_id,
    )


async def list_jobs(
    store: JobStore,
    *,
    owner_user_id: str,
    limit: int = 50,
) -> list[JobSnapshot]:
    return await _call(
        store,
        "list",
        owner_user_id=owner_user_id,
        limit=limit,
    )


async def job_events(
    store: JobStore,
    job_id: str,
    *,
    owner_user_id: str,
) -> list[JobEvent]:
    return await _call(
        store,
        "events",
        job_id,
        owner_user_id=owner_user_id,
    )


async def remove_job(
    store: JobStore,
    job_id: str,
    *,
    owner_user_id: str,
) -> bool:
    return await _call(
        store,
        "remove",
        job_id,
        owner_user_id=owner_user_id,
    )


async def cancel_job(
    store: JobStore,
    job_id: str,
    *,
    owner_user_id: str,
) -> JobSnapshot | None:
    return await _call(
        store,
        "cancel_and_wait",
        job_id,
        owner_user_id=owner_user_id,
    )


async def ensure_capacity(
    store: JobStore,
    owner_user_id: str,
) -> None:
    await _call(store, "ensure_capacity", owner_user_id)


def is_memory_store(store: JobStore) -> bool:
    return not bool(getattr(store, "is_distributed", False))
