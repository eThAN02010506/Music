from __future__ import annotations

from collections import Counter
from contextlib import AbstractAsyncContextManager
from threading import Lock
from types import TracebackType


class CapacityLimitError(RuntimeError):
    """Raised when an immediate, process-local work limit is exhausted."""

    def __init__(self, detail: str, *, global_limit: bool) -> None:
        super().__init__(detail)
        self.global_limit = global_limit


class CapacityLimiter:
    """Cancellation-safe, non-queuing capacity leases.

    The counter uses a regular lock because acquiring a lease must be
    immediate: expensive requests are rejected instead of accumulating an
    unbounded queue. This is also safe when tests use more than one event loop.
    """

    def __init__(
        self,
        *,
        max_active: int,
        max_active_per_owner: int | None = None,
        label: str = "请求",
    ) -> None:
        if max_active < 1:
            raise ValueError("max_active must be positive")
        if max_active_per_owner is not None and max_active_per_owner < 1:
            raise ValueError("max_active_per_owner must be positive")
        self.max_active = int(max_active)
        self.max_active_per_owner = (
            int(max_active_per_owner)
            if max_active_per_owner is not None
            else None
        )
        self.label = label
        self._guard = Lock()
        self._active = 0
        self._active_by_owner: Counter[str] = Counter()

    def lease(
        self,
        owner: str | None = None,
        *,
        weight: int = 1,
    ) -> "_CapacityLease":
        normalized_weight = int(weight)
        if normalized_weight < 1:
            raise ValueError("capacity lease weight must be positive")
        return _CapacityLease(
            self,
            owner.strip() if owner else None,
            normalized_weight,
        )

    def _acquire(self, owner: str | None, weight: int) -> None:
        with self._guard:
            if self._active + weight > self.max_active:
                raise CapacityLimitError(
                    f"{self.label}并发已达系统上限，请稍后再试。",
                    global_limit=True,
                )
            if (
                owner is not None
                and self.max_active_per_owner is not None
                and self._active_by_owner[owner] + weight
                > self.max_active_per_owner
            ):
                raise CapacityLimitError(
                    f"{self.label}并发已达当前用户上限，请稍后再试。",
                    global_limit=False,
                )
            self._active += weight
            if owner is not None:
                self._active_by_owner[owner] += weight

    def _release(self, owner: str | None, weight: int) -> None:
        with self._guard:
            if self._active < weight:
                raise RuntimeError("capacity lease released without acquire")
            self._active -= weight
            if owner is not None:
                remaining = self._active_by_owner[owner] - weight
                if remaining > 0:
                    self._active_by_owner[owner] = remaining
                else:
                    self._active_by_owner.pop(owner, None)

    @property
    def active(self) -> int:
        with self._guard:
            return self._active


class _CapacityLease(AbstractAsyncContextManager[None]):
    def __init__(
        self,
        limiter: CapacityLimiter,
        owner: str | None,
        weight: int,
    ) -> None:
        self._limiter = limiter
        self._owner = owner
        self._weight = weight
        self._acquired = False

    async def __aenter__(self) -> None:
        if self._acquired:
            raise RuntimeError("capacity lease cannot be entered twice")
        self._limiter._acquire(self._owner, self._weight)
        self._acquired = True

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._acquired:
            self._acquired = False
            self._limiter._release(self._owner, self._weight)
