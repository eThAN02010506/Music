from __future__ import annotations

import asyncio
import time

import fakeredis.aioredis
import pytest

from music_insight.api.capacity import CapacityLimitError
from music_insight.api.app import create_app
from music_insight.distributed.capacity import RedisCapacityLimiter
from music_insight.distributed.jobs import RedisAnalysisJobStore


def _limiter(
    client,
    *,
    max_active: int = 2,
    per_owner: int | None = 1,
    ttl: int = 60,
) -> RedisCapacityLimiter:
    return RedisCapacityLimiter(
        client,
        key_prefix="test-music",
        max_active=max_active,
        max_active_per_owner=per_owner,
        lease_ttl_seconds=ttl,
        label="测试",
    )


def test_redis_capacity_is_shared_across_limiter_instances() -> None:
    async def exercise() -> tuple[CapacityLimitError, CapacityLimitError]:
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        first = _limiter(client, max_active=2, per_owner=1)
        second = _limiter(client, max_active=2, per_owner=1)

        async with first.lease("alice"):
            with pytest.raises(CapacityLimitError) as owner_error:
                async with second.lease("alice"):
                    pass
            async with second.lease("bob"):
                with pytest.raises(CapacityLimitError) as global_error:
                    async with first.lease("charlie"):
                        pass
        async with second.lease("charlie"):
            pass
        await client.aclose()
        return owner_error.value, global_error.value

    owner_error, global_error = asyncio.run(exercise())

    assert owner_error.global_limit is False
    assert global_error.global_limit is True


def test_expired_redis_capacity_lease_is_reclaimed() -> None:
    async def exercise() -> None:
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = _limiter(client, max_active=1, per_owner=1)
        token = "abandoned"
        await client.zadd(limiter._leases_key, {token: time.time() * 1000 - 1})
        await client.hset(limiter._owners_key, token, "alice")
        await client.hset(limiter._weights_key, token, 1)

        async with limiter.lease("bob"):
            assert await client.zcard(limiter._leases_key) == 1
            assert await client.hexists(limiter._owners_key, token) == 0

        assert await client.exists(limiter._leases_key) == 0
        await client.aclose()

    asyncio.run(exercise())


def test_redis_capacity_heartbeat_extends_a_live_lease() -> None:
    async def exercise() -> tuple[float, float]:
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = _limiter(client, max_active=1, per_owner=1, ttl=1)
        async with limiter.lease("alice") as _:
            tokens = await client.zrange(limiter._leases_key, 0, -1)
            assert len(tokens) == 1
            before = float(
                await client.zscore(limiter._leases_key, tokens[0])
            )
            await asyncio.sleep(0.4)
            after = float(
                await client.zscore(limiter._leases_key, tokens[0])
            )
        await client.aclose()
        return before, after

    before, after = asyncio.run(exercise())
    assert after > before


def test_redis_capacity_release_survives_request_cancellation() -> None:
    async def exercise() -> None:
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = _limiter(client, max_active=1, per_owner=1)
        entered = asyncio.Event()
        blocker = asyncio.Event()

        async def hold() -> None:
            async with limiter.lease("alice"):
                entered.set()
                await blocker.wait()

        task = asyncio.create_task(hold())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with limiter.lease("bob"):
            pass
        await client.aclose()

    asyncio.run(exercise())


def test_redis_job_backend_selects_the_global_direct_work_limiter() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    jobs = RedisAnalysisJobStore(
        client,
        key_prefix="test-app",
        max_active=2,
        max_active_per_owner=1,
        ttl_seconds=3600,
    )

    application = create_app(job_store=jobs)  # type: ignore[arg-type]

    assert isinstance(
        application.state.direct_work_limiter,
        RedisCapacityLimiter,
    )
    asyncio.run(client.aclose())


def test_redis_capacity_release_failure_does_not_mask_completed_work(
    monkeypatch,
) -> None:
    async def exercise() -> None:
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = _limiter(client, max_active=1, per_owner=1)

        async def failed_release(_token: str) -> None:
            raise ConnectionError("temporary Redis outage")

        monkeypatch.setattr(limiter, "_release", failed_release)
        async with limiter.lease("alice"):
            pass
        await client.aclose()

    asyncio.run(exercise())
