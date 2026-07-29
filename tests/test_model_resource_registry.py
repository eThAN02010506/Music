from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import gc
from threading import Barrier, Event, Lock
from typing import Any, cast
from weakref import ref

import pytest

from music_insight.pipeline.orchestrator import AnalysisOrchestrator
from music_insight.pipeline.resources import (
    LoopLocalGate,
    ModelResourceRegistry,
)


def _orchestrator(gate: LoopLocalGate) -> AnalysisOrchestrator:
    unused_adapter = cast(Any, object())
    return AnalysisOrchestrator(
        unified=unused_adapter,
        dsp=unused_adapter,
        preprocessor=unused_adapter,
        model_gate=gate,
    )


def test_registry_canonicalizes_service_origin_and_loopback_aliases() -> None:
    registry = ModelResourceRegistry()

    gates = [
        registry.gate("http://localhost:8005/", 4),
        registry.gate("HTTP://LOCALHOST.:8005/v1", 3),
        registry.gate("http://127.0.0.1:8005/other/", 2),
        registry.gate("http://127.20.30.40:8005?route=model", 2),
        registry.gate("http://[::1]:8005/", 1),
        registry.gate("http://0.0.0.0:8005/", 1),
        registry.gate("http://[::]:8005/", 1),
    ]

    assert all(gate is gates[0] for gate in gates)
    assert gates[0].limit == 1
    assert registry.gate("http://localhost:8006", 1) is not gates[0]
    assert registry.gate("https://localhost:8005", 1) is not gates[0]
    assert registry.gate("http://localhost", 1) is registry.gate(
        "http://127.0.0.1:80/v1",
        2,
    )


def test_registry_uses_one_gate_and_strictest_limit_across_threads() -> None:
    registry = ModelResourceRegistry()
    registrations = [
        ("http://localhost:8004/", 8),
        ("http://127.0.0.1:8004", 4),
        ("http://[::1]:8004/v1", 2),
        ("http://127.0.0.2:8004/api", 1),
    ] * 8

    with ThreadPoolExecutor(max_workers=8) as executor:
        gates = list(
            executor.map(
                lambda item: registry.gate(item[0], item[1]),
                registrations,
            )
        )

    assert len({id(gate) for gate in gates}) == 1
    assert gates[0].limit == 1


def test_gate_enforces_limit_across_independent_event_loops() -> None:
    gate = LoopLocalGate(limit=1)
    start = Barrier(2)
    counter_guard = Lock()
    active = 0
    maximum_active = 0

    async def exercise() -> None:
        nonlocal active, maximum_active
        start.wait(timeout=1)
        async with gate:
            with counter_guard:
                active += 1
                maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.03)
            with counter_guard:
                active -= 1

    def run() -> None:
        asyncio.run(exercise())

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run) for _ in range(2)]
        for future in futures:
            future.result(timeout=2)

    assert maximum_active == 1


def test_cancelled_waiter_does_not_consume_capacity_or_break_fifo() -> None:
    gate = LoopLocalGate(limit=1)

    async def exercise() -> list[str]:
        entered: list[str] = []

        async def contend(label: str) -> None:
            async with gate:
                entered.append(label)

        await gate.__aenter__()
        cancelled = asyncio.create_task(contend("cancelled"))
        await asyncio.sleep(0)
        survivor = asyncio.create_task(contend("survivor"))
        await asyncio.sleep(0)

        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled

        await gate.__aexit__()
        await asyncio.wait_for(survivor, timeout=1)

        async with gate:
            entered.append("reused")
        return entered

    assert asyncio.run(exercise()) == ["survivor", "reused"]


def test_cancelled_granted_waiter_returns_reserved_capacity() -> None:
    gate = LoopLocalGate(limit=1)

    async def exercise() -> None:
        async def contend() -> None:
            async with gate:
                raise AssertionError("cancelled contender unexpectedly entered")

        await gate.__aenter__()
        contender = asyncio.create_task(contend())
        await asyncio.sleep(0)

        # Releasing reserves capacity and schedules delivery. Cancelling before
        # the loop runs that callback must compensate the reservation exactly
        # once, regardless of callback/cancellation ordering.
        await gate.__aexit__()
        contender.cancel()
        with pytest.raises(asyncio.CancelledError):
            await contender

        async def reuse() -> None:
            async with gate:
                return

        await asyncio.wait_for(reuse(), timeout=1)

    asyncio.run(exercise())


def test_completed_contention_does_not_retain_closed_event_loops() -> None:
    gate = LoopLocalGate(limit=1)
    loop_refs = []

    async def exercise() -> None:
        async def contend() -> None:
            async with gate:
                await asyncio.sleep(0)

        await asyncio.gather(contend(), contend())

    for _ in range(10):
        loop = asyncio.new_event_loop()
        loop_refs.append(ref(loop))
        loop.run_until_complete(exercise())
        loop.close()

    del loop
    gc.collect()

    assert all(loop_ref() is None for loop_ref in loop_refs)


def test_abandoned_waiter_does_not_retain_a_closed_event_loop() -> None:
    gate = LoopLocalGate(limit=1)
    asyncio.run(gate.__aenter__())

    loop = asyncio.new_event_loop()
    # This test intentionally abandons a pending task to model an abrupt loop
    # shutdown. Suppress the loop's diagnostic; the assertions verify cleanup.
    loop.set_exception_handler(lambda *_: None)

    async def wait_for_gate() -> None:
        await gate.__aenter__()

    task = loop.create_task(wait_for_gate())
    loop.run_until_complete(asyncio.sleep(0))
    loop_ref = ref(loop)
    loop.close()

    del task
    del loop
    gc.collect()

    assert loop_ref() is None
    assert not gate._waiters

    asyncio.run(gate.__aexit__())


def test_tightening_is_global_across_independent_event_loops() -> None:
    registry = ModelResourceRegistry()
    gate = registry.gate("http://localhost:8003", 2)
    counter_guard = Lock()
    both_entered = Event()
    releases = [Event() for _ in range(2)]
    third_entered = Event()
    active_holders = 0

    async def hold(index: int) -> None:
        nonlocal active_holders
        async with gate:
            with counter_guard:
                active_holders += 1
                if active_holders == 2:
                    both_entered.set()
            while not releases[index].is_set():
                await asyncio.sleep(0.005)

    async def contend() -> None:
        async with gate:
            third_entered.set()

    with ThreadPoolExecutor(max_workers=3) as executor:
        holders = [
            executor.submit(asyncio.run, hold(index)) for index in range(2)
        ]
        assert both_entered.wait(timeout=1)

        same_gate = registry.gate("http://127.0.0.1:8003/v1", 1)
        third = executor.submit(asyncio.run, contend())
        assert third_entered.wait(timeout=0.05) is False

        releases[0].set()
        holders[0].result(timeout=1)
        assert third_entered.wait(timeout=0.05) is False

        releases[1].set()
        holders[1].result(timeout=1)
        assert third_entered.wait(timeout=1)
        third.result(timeout=1)

    assert same_gate is gate
    assert gate.limit == 1


def test_tightening_an_active_gate_waits_for_existing_leases() -> None:
    registry = ModelResourceRegistry()
    gate = registry.gate("http://localhost:8003", 2)

    async def exercise() -> None:
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()
        release_second = asyncio.Event()
        third_entered = asyncio.Event()

        async def hold(entered: asyncio.Event, release: asyncio.Event) -> None:
            async with gate:
                entered.set()
                await release.wait()

        first = asyncio.create_task(hold(first_entered, release_first))
        second = asyncio.create_task(hold(second_entered, release_second))
        await asyncio.gather(first_entered.wait(), second_entered.wait())

        same_gate = registry.gate("http://127.0.0.1:8003/", 1)
        assert same_gate is gate
        assert gate.limit == 1

        async def wait_for_gate() -> None:
            async with same_gate:
                third_entered.set()

        third = asyncio.create_task(wait_for_gate())
        await asyncio.sleep(0)
        assert third_entered.is_set() is False

        release_first.set()
        await first
        await asyncio.sleep(0)
        assert third_entered.is_set() is False

        release_second.set()
        await second
        await asyncio.wait_for(third_entered.wait(), timeout=1)
        await third

    asyncio.run(exercise())


def test_clear_current_loop_does_not_replace_an_active_gate_state() -> None:
    registry = ModelResourceRegistry()
    gate = registry.gate("http://localhost:8002", 1)

    async def exercise() -> None:
        contender_entered = asyncio.Event()

        async def contend() -> None:
            async with gate:
                contender_entered.set()

        async with gate:
            registry.clear_current_loop()
            contender = asyncio.create_task(contend())
            await asyncio.sleep(0)
            assert contender_entered.is_set() is False

        await asyncio.wait_for(contender_entered.wait(), timeout=1)
        await contender

    asyncio.run(exercise())


def test_clear_current_loop_preserves_notified_waiters() -> None:
    registry = ModelResourceRegistry()
    gate = registry.gate("http://localhost:8002", 1)

    async def exercise() -> None:
        old_entered = asyncio.Event()
        new_entered = asyncio.Event()
        release_old = asyncio.Event()
        release_new = asyncio.Event()

        async def contend(
            entered: asyncio.Event,
            release: asyncio.Event,
        ) -> None:
            async with gate:
                entered.set()
                await release.wait()

        await gate.__aenter__()
        old_contender = asyncio.create_task(contend(old_entered, release_old))
        await asyncio.sleep(0)

        # Releasing notifies the old waiter, but it cannot resume until this
        # coroutine yields. Clearing in this window must preserve its state.
        await gate.__aexit__()
        registry.clear_current_loop()
        new_contender = asyncio.create_task(contend(new_entered, release_new))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert old_entered.is_set() + new_entered.is_set() == 1
        if old_entered.is_set():
            release_old.set()
        else:
            release_new.set()
        await asyncio.wait_for(
            asyncio.gather(old_entered.wait(), new_entered.wait()),
            timeout=1,
        )
        release_old.set()
        release_new.set()
        await asyncio.gather(old_contender, new_contender)

    asyncio.run(exercise())


def test_independent_orchestrators_share_the_strictest_physical_gate() -> None:
    registry = ModelResourceRegistry()
    first = _orchestrator(registry.gate("http://localhost:8005/", 3))
    second = _orchestrator(registry.gate("http://[::1]:8005/v1", 1))

    assert first.model_gate is second.model_gate
    assert isinstance(first.model_gate, LoopLocalGate)
    assert first.model_gate.limit == 1

    async def exercise() -> int:
        active = 0
        maximum_active = 0

        async def invoke(orchestrator: AnalysisOrchestrator) -> None:
            nonlocal active, maximum_active
            assert orchestrator.model_gate is not None
            async with orchestrator.model_gate:
                active += 1
                maximum_active = max(maximum_active, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(
            *(invoke(first if index % 2 == 0 else second) for index in range(8))
        )
        return maximum_active

    assert asyncio.run(exercise()) == 1
