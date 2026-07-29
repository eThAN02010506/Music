from __future__ import annotations

import asyncio
from threading import Lock
from weakref import WeakKeyDictionary


class LoopLocalGate:
    """One logical resource limit backed by a semaphore per event loop."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._guard = Lock()
        self._semaphores: WeakKeyDictionary[
            asyncio.AbstractEventLoop,
            asyncio.Semaphore,
        ] = WeakKeyDictionary()

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        with self._guard:
            semaphore = self._semaphores.get(loop)
            if semaphore is None:
                semaphore = asyncio.Semaphore(self.limit)
                self._semaphores[loop] = semaphore
            return semaphore

    async def __aenter__(self) -> None:
        await self._semaphore().acquire()

    async def __aexit__(self, *_) -> None:
        self._semaphore().release()

    def clear_current_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        with self._guard:
            self._semaphores.pop(loop, None)


class ModelResourceRegistry:
    """Process-local concurrency gates keyed by model service location."""

    def __init__(self) -> None:
        self._gates: dict[tuple[str, int], LoopLocalGate] = {}

    def gate(self, location: str, limit: int = 1) -> LoopLocalGate:
        normalized_limit = max(1, int(limit))
        key = (location.rstrip("/"), normalized_limit)
        gate = self._gates.get(key)
        if gate is None:
            gate = LoopLocalGate(normalized_limit)
            self._gates[key] = gate
        return gate

    def clear_current_loop(self) -> None:
        for gate in self._gates.values():
            gate.clear_current_loop()


model_resources = ModelResourceRegistry()
