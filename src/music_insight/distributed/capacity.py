from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
import logging
import time
from types import TracebackType
from uuid import uuid4

from redis.asyncio import Redis

from music_insight.api.capacity import CapacityLimitError


logger = logging.getLogger(__name__)


_ACQUIRE_LEASE = """
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
for _, token in ipairs(expired) do
  redis.call('ZREM', KEYS[1], token)
  redis.call('HDEL', KEYS[2], token)
  redis.call('HDEL', KEYS[3], token)
end

local global_active = 0
local owner_active = 0
for _, token in ipairs(redis.call('ZRANGE', KEYS[1], 0, -1)) do
  local weight = tonumber(redis.call('HGET', KEYS[3], token) or '0')
  global_active = global_active + weight
  if redis.call('HGET', KEYS[2], token) == ARGV[3] then
    owner_active = owner_active + weight
  end
end

local requested = tonumber(ARGV[4])
if global_active + requested > tonumber(ARGV[5]) then
  return 1
end
if ARGV[6] ~= '' and owner_active + requested > tonumber(ARGV[6]) then
  return 2
end

redis.call('ZADD', KEYS[1], ARGV[2], ARGV[7])
redis.call('HSET', KEYS[2], ARGV[7], ARGV[3])
redis.call('HSET', KEYS[3], ARGV[7], ARGV[4])
redis.call('PEXPIRE', KEYS[1], ARGV[8])
redis.call('PEXPIRE', KEYS[2], ARGV[8])
redis.call('PEXPIRE', KEYS[3], ARGV[8])
return 0
"""


_REFRESH_LEASE = """
if redis.call('ZSCORE', KEYS[1], ARGV[1]) == false then
  return 0
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[3])
redis.call('PEXPIRE', KEYS[2], ARGV[3])
redis.call('PEXPIRE', KEYS[3], ARGV[3])
return 1
"""


_RELEASE_LEASE = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
if redis.call('ZCARD', KEYS[1]) == 0 then
  redis.call('DEL', KEYS[1], KEYS[2], KEYS[3])
end
return 1
"""


class RedisCapacityLimiter:
    """Non-queuing, crash-recoverable capacity shared by API processes.

    Each active request owns a short-lived Redis lease. A heartbeat extends
    the lease while work is running; an API crash therefore releases capacity
    automatically without relying on a process-local ``finally`` block.
    """

    is_distributed = True

    def __init__(
        self,
        client: Redis,
        *,
        key_prefix: str,
        max_active: int,
        max_active_per_owner: int | None = None,
        lease_ttl_seconds: int = 3600,
        label: str = "请求",
    ) -> None:
        if max_active < 1:
            raise ValueError("max_active must be positive")
        if max_active_per_owner is not None and max_active_per_owner < 1:
            raise ValueError("max_active_per_owner must be positive")
        if lease_ttl_seconds < 1:
            raise ValueError("lease_ttl_seconds must be positive")
        self.client = client
        self.max_active = int(max_active)
        self.max_active_per_owner = (
            int(max_active_per_owner)
            if max_active_per_owner is not None
            else None
        )
        self.lease_ttl_seconds = int(lease_ttl_seconds)
        self.label = label
        tag = "{" + key_prefix + "}"
        self._leases_key = f"{tag}:direct-work:leases"
        self._owners_key = f"{tag}:direct-work:owners"
        self._weights_key = f"{tag}:direct-work:weights"

    def lease(
        self,
        owner: str | None = None,
        *,
        weight: int = 1,
    ) -> "_RedisCapacityLease":
        normalized_weight = int(weight)
        if normalized_weight < 1:
            raise ValueError("capacity lease weight must be positive")
        return _RedisCapacityLease(
            self,
            owner.strip() if owner else None,
            normalized_weight,
        )

    async def _acquire(
        self,
        token: str,
        owner: str | None,
        weight: int,
    ) -> None:
        now_ms = int(time.time() * 1000)
        expiry_ms = now_ms + self.lease_ttl_seconds * 1000
        key_ttl_ms = self.lease_ttl_seconds * 2000
        code = int(
            await self.client.eval(
                _ACQUIRE_LEASE,
                3,
                self._leases_key,
                self._owners_key,
                self._weights_key,
                now_ms,
                expiry_ms,
                owner or "",
                weight,
                self.max_active,
                self.max_active_per_owner or "",
                token,
                key_ttl_ms,
            )
        )
        if code == 1:
            raise CapacityLimitError(
                f"{self.label}并发已达系统上限，请稍后再试。",
                global_limit=True,
            )
        if code == 2:
            raise CapacityLimitError(
                f"{self.label}并发已达当前用户上限，请稍后再试。",
                global_limit=False,
            )
        if code != 0:
            raise RuntimeError("Redis capacity lease returned an invalid state")

    async def _refresh(self, token: str) -> bool:
        expiry_ms = int(time.time() * 1000) + self.lease_ttl_seconds * 1000
        key_ttl_ms = self.lease_ttl_seconds * 2000
        refreshed = await self.client.eval(
            _REFRESH_LEASE,
            3,
            self._leases_key,
            self._owners_key,
            self._weights_key,
            token,
            expiry_ms,
            key_ttl_ms,
        )
        return bool(int(refreshed))

    async def _release(self, token: str) -> None:
        await self.client.eval(
            _RELEASE_LEASE,
            3,
            self._leases_key,
            self._owners_key,
            self._weights_key,
            token,
        )

    @property
    def heartbeat_seconds(self) -> float:
        return max(0.25, min(30.0, self.lease_ttl_seconds / 3))


class _RedisCapacityLease(AbstractAsyncContextManager[None]):
    def __init__(
        self,
        limiter: RedisCapacityLimiter,
        owner: str | None,
        weight: int,
    ) -> None:
        self._limiter = limiter
        self._owner = owner
        self._weight = weight
        self._token = uuid4().hex
        self._acquired = False
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> None:
        if self._acquired:
            raise RuntimeError("capacity lease cannot be entered twice")
        await self._limiter._acquire(
            self._token,
            self._owner,
            self._weight,
        )
        self._acquired = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat())

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not self._acquired:
            return
        self._acquired = False
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(
                self._heartbeat_task,
                return_exceptions=True,
            )
            self._heartbeat_task = None
        try:
            await self._settled_release()
        except Exception:
            # The lease already has a server-side expiry. A transient Redis
            # failure while releasing must not turn completed user work into
            # an HTTP 500; capacity is recovered by TTL in the worst case.
            logger.warning(
                "Unable to release Redis capacity lease; awaiting TTL expiry",
                exc_info=True,
            )

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self._limiter.heartbeat_seconds)
            try:
                refreshed = await self._limiter._refresh(self._token)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Redis outages already prevent new acquisitions. Keep
                # retrying so a recovered Redis can refresh this live lease.
                continue
            if not refreshed:
                return

    async def _settled_release(self) -> None:
        release = asyncio.create_task(self._limiter._release(self._token))
        cancellation: asyncio.CancelledError | None = None
        while not release.done():
            try:
                await asyncio.shield(release)
            except asyncio.CancelledError as exc:
                cancellation = exc
                continue
        release.result()
        if cancellation is not None:
            raise cancellation
