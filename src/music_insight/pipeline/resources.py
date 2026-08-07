from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import ipaddress
from threading import Lock
from typing import Literal
from urllib.parse import urlsplit
from weakref import ReferenceType, ref


_WaiterState = Literal["waiting", "granted", "entered", "cancelled"]


@dataclass(eq=False, slots=True, weakref_slot=True)
class _GateWaiter:
    state: _WaiterState = "waiting"
    loop_ref: ReferenceType[asyncio.AbstractEventLoop] | None = None
    future_ref: ReferenceType[asyncio.Future[None]] | None = None


class LoopLocalGate:
    """One process-local FIFO limit usable from independent event loops."""

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._guard = Lock()
        self._active = 0
        self._waiters: deque[_GateWaiter] = deque()

    @property
    def limit(self) -> int:
        with self._guard:
            return self._limit

    def tighten(self, limit: int) -> int:
        """Keep the strictest limit without replacing an in-use gate."""

        normalized_limit = max(1, int(limit))
        with self._guard:
            self._limit = min(self._limit, normalized_limit)
            return self._limit

    def _new_waiter(self) -> tuple[_GateWaiter, asyncio.Future[None]]:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        waiter = _GateWaiter()
        gate_ref = ref(self)
        waiter_ref = ref(waiter)

        def abandon_if_unreferenced(_) -> None:
            gate = gate_ref()
            abandoned = waiter_ref()
            if gate is not None and abandoned is not None:
                gate._abandon(abandoned)

        waiter.loop_ref = ref(loop, abandon_if_unreferenced)
        waiter.future_ref = ref(future, abandon_if_unreferenced)
        return waiter, future

    @staticmethod
    def _waiter_targets(
        waiter: _GateWaiter,
    ) -> tuple[
        asyncio.AbstractEventLoop | None,
        asyncio.Future[None] | None,
    ]:
        loop = waiter.loop_ref() if waiter.loop_ref is not None else None
        future = (
            waiter.future_ref() if waiter.future_ref is not None else None
        )
        return loop, future

    def _collect_grants_locked(self) -> list[_GateWaiter]:
        grants: list[_GateWaiter] = []
        while self._active < self._limit and self._waiters:
            waiter = self._waiters.popleft()
            if waiter.state != "waiting":
                continue
            loop, future = self._waiter_targets(waiter)
            if loop is None or future is None or loop.is_closed():
                waiter.state = "cancelled"
                continue
            waiter.state = "granted"
            self._active += 1
            grants.append(waiter)
        return grants

    def _revoke_grant(self, waiter: _GateWaiter) -> list[_GateWaiter]:
        with self._guard:
            if waiter.state != "granted":
                return []
            waiter.state = "cancelled"
            self._active -= 1
            return self._collect_grants_locked()

    def _schedule_grants(self, grants: list[_GateWaiter]) -> None:
        pending = deque(grants)
        while pending:
            waiter = pending.popleft()
            loop, future = self._waiter_targets(waiter)
            if loop is None or future is None or loop.is_closed():
                pending.extend(self._revoke_grant(waiter))
                continue
            try:
                loop.call_soon_threadsafe(self._deliver_grant, waiter)
            except RuntimeError:
                pending.extend(self._revoke_grant(waiter))

    def _deliver_grant(self, waiter: _GateWaiter) -> None:
        _, future = self._waiter_targets(waiter)
        if future is None or future.done():
            self._schedule_grants(self._revoke_grant(waiter))
            return
        deliver = False
        with self._guard:
            # Deliver under the lock so a concurrent revoke/cancel cannot
            # observe a granted waiter whose future was never resolved.
            if waiter.state == "granted":
                deliver = True
        if deliver:
            try:
                future.set_result(None)
            except (asyncio.InvalidStateError, RuntimeError):
                # The waiter task was cancelled between scheduling and delivery;
                # revoke its slot so capacity is not leaked.
                self._schedule_grants(self._revoke_grant(waiter))

    def _abandon(self, waiter: _GateWaiter) -> None:
        grants: list[_GateWaiter] = []
        with self._guard:
            if waiter.state == "waiting":
                waiter.state = "cancelled"
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
                grants = self._collect_grants_locked()
            elif waiter.state == "granted":
                waiter.state = "cancelled"
                self._active -= 1
                grants = self._collect_grants_locked()
        self._schedule_grants(grants)

    async def __aenter__(self) -> None:
        waiter, future = self._new_waiter()
        grants: list[_GateWaiter] = []
        entered_immediately = False
        with self._guard:
            if self._active < self._limit and not self._waiters:
                self._active += 1
                waiter.state = "entered"
                entered_immediately = True
            else:
                self._waiters.append(waiter)
                grants = self._collect_grants_locked()
        self._schedule_grants(grants)
        if entered_immediately:
            return

        try:
            await future
        except BaseException:
            self._abandon(waiter)
            raise

        with self._guard:
            if waiter.state != "granted":
                raise RuntimeError("model resource gate grant was revoked")
            waiter.state = "entered"

    async def __aexit__(self, *_) -> None:
        with self._guard:
            if self._active <= 0:
                raise RuntimeError("model resource gate released without acquire")
            self._active -= 1
            grants = self._collect_grants_locked()
        self._schedule_grants(grants)

    def clear_current_loop(self) -> None:
        """Compatibility no-op: the gate retains no event-loop-owned state."""


def _resource_key(location: str) -> str:
    """Return a conservative process-resource identity for one service origin."""

    cleaned = str(location).strip()
    if not cleaned:
        return ""
    parsed = urlsplit(cleaned)
    hostname = parsed.hostname
    if not parsed.scheme or hostname is None:
        return cleaned.rstrip("/")

    scheme = parsed.scheme.casefold()
    host = hostname.rstrip(".").casefold()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None

    if host == "localhost" or host.endswith(".localhost"):
        canonical_host = "loopback"
    elif address is not None and (
        address.is_loopback or address.is_unspecified
    ):
        canonical_host = "loopback"
    elif address is not None:
        canonical_host = address.compressed
    else:
        canonical_host = host

    try:
        port = parsed.port
    except ValueError:
        return cleaned.rstrip("/")
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)

    if ":" in canonical_host:
        canonical_host = f"[{canonical_host}]"
    port_suffix = f":{port}" if port is not None else ""
    return f"{scheme}://{canonical_host}{port_suffix}"


class ModelResourceRegistry:
    """Process-local concurrency gates keyed by model service location."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._gates: dict[str, LoopLocalGate] = {}

    def gate(self, location: str, limit: int = 1) -> LoopLocalGate:
        normalized_limit = max(1, int(limit))
        key = _resource_key(location)
        with self._guard:
            gate = self._gates.get(key)
            if gate is None:
                gate = LoopLocalGate(normalized_limit)
                self._gates[key] = gate
            else:
                gate.tighten(normalized_limit)
            return gate

    def clear_current_loop(self) -> None:
        with self._guard:
            gates = tuple(self._gates.values())
        for gate in gates:
            gate.clear_current_loop()


model_resources = ModelResourceRegistry()
