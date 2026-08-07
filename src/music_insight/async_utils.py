from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar


P = ParamSpec("P")
T = TypeVar("T")


async def settle_despite_cancellation(task: asyncio.Task):
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


async def run_sync_settled(
    function: Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run blocking work without releasing outer resources before it stops.

    Python cannot terminate a thread that is already running. If the awaiting
    request is cancelled, keep observing the worker under ``shield`` and only
    propagate cancellation after the worker has settled. This prevents rapid
    cancel/retry cycles from bypassing an outer concurrency gate.
    """

    worker = asyncio.create_task(
        asyncio.to_thread(function, *args, **kwargs)
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancelled:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if worker.done() and not worker.cancelled():
            # Retrieve any exception so asyncio does not report an unobserved
            # task; cancellation of the caller remains the public outcome.
            try:
                worker.result()
            except BaseException:
                pass
        raise cancelled
