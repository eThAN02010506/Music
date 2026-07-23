from __future__ import annotations

import asyncio


class ModelResourceRegistry:
    """Process-local concurrency gates keyed by model service location."""

    def __init__(self) -> None:
        self._gates: dict[tuple[str, int], asyncio.Semaphore] = {}

    def gate(self, location: str, limit: int = 1) -> asyncio.Semaphore:
        normalized_limit = max(1, int(limit))
        key = (location.rstrip("/"), normalized_limit)
        gate = self._gates.get(key)
        if gate is None:
            gate = asyncio.Semaphore(normalized_limit)
            self._gates[key] = gate
        return gate


model_resources = ModelResourceRegistry()
