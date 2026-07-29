from __future__ import annotations

from datetime import UTC, datetime
from redis.asyncio import Redis
from redis.exceptions import RedisError

from music_insight.api.jobs import (
    JobCapacityError,
    JobEvent,
    JobSnapshot,
    JobState,
)
from music_insight.config import Settings
from music_insight.distributed.payloads import DistributedAnalysisPayload
from music_insight.schemas import AnalysisResult


_TERMINAL_STATES = {
    JobState.COMPLETED.value,
    JobState.FAILED.value,
    JobState.CANCELLED.value,
}


class DistributedJobCancelled(Exception):
    """Raised inside a worker when Redis contains a cancellation request."""


class DistributedJobUnavailable(RuntimeError):
    """Raised when the shared Redis job service cannot be reached."""


_CREATE_JOB = """
for _, id in ipairs(redis.call('SMEMBERS', KEYS[2])) do
  if redis.call('EXISTS', ARGV[12] .. id) == 0 then
    redis.call('SREM', KEYS[2], id)
  end
end
for _, id in ipairs(redis.call('SMEMBERS', KEYS[3])) do
  if redis.call('EXISTS', ARGV[12] .. id) == 0 then
    redis.call('SREM', KEYS[3], id)
  end
end
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 3
end
if redis.call('SCARD', KEYS[2]) >= tonumber(ARGV[1]) then
  return 1
end
if redis.call('SCARD', KEYS[3]) >= tonumber(ARGV[2]) then
  return 2
end
redis.call('HSET', KEYS[1],
  'owner', ARGV[3],
  'state', 'queued',
  'stage', 'queued',
  'progress', '0',
  'message', ARGV[4],
  'created_at', ARGV[5],
  'updated_at', ARGV[5],
  'revision', '0',
  'cancel_requested', '0',
  'payload', ARGV[6],
  'error', '',
  'persistence_error', '')
redis.call('SADD', KEYS[2], ARGV[7])
redis.call('SADD', KEYS[3], ARGV[7])
redis.call('ZADD', KEYS[4], ARGV[8], ARGV[7])
redis.call('EXPIRE', KEYS[1], ARGV[9])
redis.call('EXPIRE', KEYS[4], ARGV[9])
redis.call('RPUSH', KEYS[5], ARGV[10])
redis.call('LTRIM', KEYS[5], -tonumber(ARGV[11]), -1)
redis.call('EXPIRE', KEYS[5], ARGV[9])
return 0
"""


_CHECK_CAPACITY = """
for _, id in ipairs(redis.call('SMEMBERS', KEYS[1])) do
  if redis.call('EXISTS', ARGV[3] .. id) == 0 then
    redis.call('SREM', KEYS[1], id)
  end
end
for _, id in ipairs(redis.call('SMEMBERS', KEYS[2])) do
  if redis.call('EXISTS', ARGV[3] .. id) == 0 then
    redis.call('SREM', KEYS[2], id)
  end
end
if redis.call('SCARD', KEYS[1]) >= tonumber(ARGV[1]) then
  return 1
end
if redis.call('SCARD', KEYS[2]) >= tonumber(ARGV[2]) then
  return 2
end
return 0
"""


_UPDATE_PROGRESS = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return {-1, 0}
end
if redis.call('HGET', KEYS[1], 'owner') ~= ARGV[1] then
  return {-1, 0}
end
local state = redis.call('HGET', KEYS[1], 'state')
if state == 'completed' or state == 'failed' or state == 'cancelled' then
  return {3, tonumber(redis.call('HGET', KEYS[1], 'revision') or '0')}
end
if redis.call('HGET', KEYS[1], 'cancel_requested') == '1' then
  return {2, tonumber(redis.call('HGET', KEYS[1], 'revision') or '0')}
end
local current = tonumber(redis.call('HGET', KEYS[1], 'progress') or '0')
local requested = tonumber(ARGV[3])
if requested < current then
  requested = current
end
if requested > 0.99 then
  requested = 0.99
end
local revision = redis.call('HINCRBY', KEYS[1], 'revision', 1)
redis.call('HSET', KEYS[1],
  'state', 'running',
  'stage', ARGV[2],
  'progress', tostring(requested),
  'message', ARGV[4],
  'updated_at', ARGV[5])
redis.call('RPUSH', KEYS[2], ARGV[6])
redis.call('LTRIM', KEYS[2], -tonumber(ARGV[7]), -1)
redis.call('EXPIRE', KEYS[1], ARGV[8])
redis.call('EXPIRE', KEYS[2], ARGV[8])
return {0, revision}
"""


_FINISH_JOB = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return {-1, 0}
end
if redis.call('HGET', KEYS[1], 'owner') ~= ARGV[1] then
  return {-1, 0}
end
local current = redis.call('HGET', KEYS[1], 'state')
if current == 'completed' or current == 'failed' or current == 'cancelled' then
  return {3, tonumber(redis.call('HGET', KEYS[1], 'revision') or '0')}
end
local requested = ARGV[2]
if redis.call('HGET', KEYS[1], 'cancel_requested') == '1' then
  requested = 'cancelled'
end
local stage = requested
local progress = redis.call('HGET', KEYS[1], 'progress') or '0'
local message = ARGV[3]
local result = ARGV[4]
local error = ARGV[5]
if requested == 'completed' then
  progress = '1'
  message = '分析完成'
elseif requested == 'cancelled' then
  message = '任务已取消'
  result = ''
  error = ''
else
  message = '分析失败'
  result = ''
end
local revision = redis.call('HINCRBY', KEYS[1], 'revision', 1)
redis.call('HSET', KEYS[1],
  'state', requested,
  'stage', stage,
  'progress', progress,
  'message', message,
  'updated_at', ARGV[6],
  'result', result,
  'error', error,
    'persistence_error', ARGV[7])
redis.call('SREM', KEYS[2], ARGV[8])
redis.call('SREM', KEYS[3], ARGV[8])
redis.call('ZADD', KEYS[5], ARGV[12], ARGV[8])
redis.call('EXPIRE', KEYS[1], ARGV[11])
redis.call('EXPIRE', KEYS[4], ARGV[11])
return {0, revision, requested}
"""


_REQUEST_CANCEL = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return {-1, 0}
end
if redis.call('HGET', KEYS[1], 'owner') ~= ARGV[1] then
  return {-1, 0}
end
local state = redis.call('HGET', KEYS[1], 'state')
if state == 'completed' or state == 'failed' or state == 'cancelled' then
  return {3, tonumber(redis.call('HGET', KEYS[1], 'revision') or '0')}
end
redis.call('HSET', KEYS[1], 'cancel_requested', '1')
local revision = redis.call('HINCRBY', KEYS[1], 'revision', 1)
if state == 'queued' then
  redis.call('HSET', KEYS[1],
    'state', 'cancelled',
    'stage', 'cancelled',
    'message', '任务已取消',
    'updated_at', ARGV[2],
    'error', '',
    'result', '')
  redis.call('SREM', KEYS[2], ARGV[3])
  redis.call('SREM', KEYS[3], ARGV[3])
  redis.call('ZADD', KEYS[4], ARGV[5], ARGV[3])
else
  redis.call('HSET', KEYS[1],
    'stage', 'cancelling',
    'message', '正在取消任务',
    'updated_at', ARGV[2])
end
redis.call('EXPIRE', KEYS[1], ARGV[4])
return {0, revision}
"""


_REMOVE_JOB = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
if redis.call('HGET', KEYS[1], 'owner') ~= ARGV[1] then
  return 0
end
local state = redis.call('HGET', KEYS[1], 'state')
if state ~= 'completed' and state ~= 'failed' and state ~= 'cancelled' then
  return -1
end
redis.call('DEL', KEYS[1], KEYS[2])
redis.call('ZREM', KEYS[3], ARGV[2])
redis.call('SREM', KEYS[4], ARGV[2])
redis.call('SREM', KEYS[5], ARGV[2])
redis.call('ZREM', KEYS[6], ARGV[2])
return 1
"""


class RedisAnalysisJobStore:
    """Cross-process job state, ownership, capacity and progress in Redis."""

    is_distributed = True

    def __init__(
        self,
        client: Redis,
        *,
        key_prefix: str,
        max_active: int,
        max_active_per_owner: int,
        ttl_seconds: int,
        max_events: int = 200,
    ) -> None:
        self.client = client
        self._tag = "{" + key_prefix + "}"
        self.max_active = max(1, int(max_active))
        self.max_active_per_owner = max(1, int(max_active_per_owner))
        self.ttl_seconds = max(3600, int(ttl_seconds))
        self.max_events = max(10, int(max_events))

    @classmethod
    def from_settings(cls, settings: Settings) -> RedisAnalysisJobStore:
        client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            health_check_interval=30,
            socket_connect_timeout=5,
            socket_timeout=10,
        )
        return cls(
            client,
            key_prefix=settings.redis_key_prefix,
            max_active=settings.max_active_jobs,
            max_active_per_owner=settings.max_active_jobs_per_user,
            ttl_seconds=settings.redis_job_ttl_seconds,
        )

    async def initialize(self) -> None:
        try:
            await self.client.ping()
        except RedisError as exc:
            raise DistributedJobUnavailable(
                "Redis job backend is unavailable."
            ) from exc

    async def shutdown(self) -> None:
        await self.client.aclose()

    async def ensure_capacity(self, owner_user_id: str) -> None:
        owner = self._require_owner(owner_user_id)
        try:
            code = await self.client.eval(
                _CHECK_CAPACITY,
                2,
                self._active_key(),
                self._owner_active_key(owner),
                self.max_active,
                self.max_active_per_owner,
                self._job_key(""),
            )
        except RedisError as exc:
            raise DistributedJobUnavailable(
                "Redis job backend is unavailable."
            ) from exc
        if int(code) == 1:
            raise JobCapacityError(
                "分析队列已满，请等待现有任务完成后重试。",
                global_limit=True,
            )
        if int(code) == 2:
            raise JobCapacityError(
                "你的待处理分析已达到上限，请等待或取消现有任务。"
            )

    async def create_distributed(
        self,
        payload: DistributedAnalysisPayload,
    ) -> JobSnapshot:
        owner = self._require_owner(payload.owner_user_id)
        job_id = payload.job_id
        now = datetime.now(UTC)
        queued = JobEvent(
            state=JobState.QUEUED,
            stage="queued",
            progress=0,
            message="任务已加入 Redis 队列",
            timestamp=now,
        )
        keys = self._job_keys(job_id, owner)
        try:
            code = await self.client.eval(
                _CREATE_JOB,
                len(keys),
                *keys,
                self.max_active,
                self.max_active_per_owner,
                owner,
                queued.message,
                now.isoformat(),
                payload.model_dump_json(),
                job_id,
                now.timestamp(),
                self.ttl_seconds,
                queued.model_dump_json(),
                self.max_events,
                self._job_key(""),
            )
        except RedisError as exc:
            raise DistributedJobUnavailable(
                "Redis job backend is unavailable."
            ) from exc
        if int(code) == 1:
            raise JobCapacityError(
                "分析队列已满，请等待现有任务完成后重试。",
                global_limit=True,
            )
        if int(code) == 2:
            raise JobCapacityError(
                "你的待处理分析已达到上限，请等待或取消现有任务。"
            )
        if int(code) != 0:
            raise RuntimeError("Distributed job identifier already exists.")
        snapshot = await self.get(job_id, owner_user_id=owner)
        if snapshot is None:
            raise DistributedJobUnavailable(
                "Redis accepted the job but did not return its state."
            )
        return snapshot

    async def get(
        self,
        job_id: str,
        *,
        owner_user_id: str,
    ) -> JobSnapshot | None:
        owner = self._require_owner(owner_user_id)
        values = await self._read_job(job_id)
        if not values or values.get("owner") != owner:
            return None
        return self._snapshot(job_id, values)

    async def get_for_maintenance(self, job_id: str) -> JobSnapshot | None:
        values = await self._read_job(job_id)
        return self._snapshot(job_id, values) if values else None

    async def result(
        self,
        job_id: str,
        *,
        owner_user_id: str,
    ) -> AnalysisResult | None:
        owner = self._require_owner(owner_user_id)
        values = await self._read_job(job_id)
        if not values or values.get("owner") != owner:
            return None
        payload = values.get("result", "")
        if not payload:
            return None
        return AnalysisResult.model_validate_json(payload)

    async def list(
        self,
        *,
        owner_user_id: str,
        limit: int = 50,
    ) -> list[JobSnapshot]:
        owner = self._require_owner(owner_user_id)
        bounded = max(1, min(int(limit), 500))
        ids = await self.client.zrevrange(
            self._owner_jobs_key(owner),
            0,
            bounded * 2 - 1,
        )
        if not ids:
            return []
        pipe = self.client.pipeline(transaction=False)
        for job_id in ids:
            pipe.hgetall(self._job_key(job_id))
        rows = await pipe.execute()
        snapshots: list[JobSnapshot] = []
        stale: list[str] = []
        for job_id, values in zip(ids, rows, strict=True):
            if not values:
                stale.append(job_id)
                continue
            if values.get("owner") == owner:
                snapshots.append(self._snapshot(job_id, values))
                if len(snapshots) == bounded:
                    break
        if stale:
            await self.client.zrem(self._owner_jobs_key(owner), *stale)
        return snapshots

    async def list_all_for_maintenance(
        self,
        *,
        limit: int = 50,
    ) -> list[JobSnapshot]:
        bounded = max(1, min(int(limit), 500))
        ids = list(await self.client.smembers(self._active_key()))
        snapshots: list[JobSnapshot] = []
        for job_id in ids[:bounded]:
            snapshot = await self.get_for_maintenance(job_id)
            if snapshot is not None:
                snapshots.append(snapshot)
        snapshots.sort(key=lambda item: item.updated_at, reverse=True)
        return snapshots

    async def active_job_ids(self) -> set[str]:
        return set(await self.client.smembers(self._active_key()))

    async def events(
        self,
        job_id: str,
        *,
        owner_user_id: str,
    ) -> list[JobEvent]:
        owner = self._require_owner(owner_user_id)
        if await self.client.hget(self._job_key(job_id), "owner") != owner:
            return []
        payloads = await self.client.lrange(self._events_key(job_id), 0, -1)
        events: list[JobEvent] = []
        for payload in payloads:
            try:
                events.append(JobEvent.model_validate_json(payload))
            except ValueError:
                continue
        return events

    async def remove(
        self,
        job_id: str,
        *,
        owner_user_id: str,
    ) -> bool:
        owner = self._require_owner(owner_user_id)
        result = await self.client.eval(
            _REMOVE_JOB,
            6,
            self._job_key(job_id),
            self._events_key(job_id),
            self._owner_jobs_key(owner),
            self._active_key(),
            self._owner_active_key(owner),
            self._terminal_key(),
            owner,
            job_id,
        )
        return int(result) == 1

    async def cancel(
        self,
        job_id: str,
        *,
        owner_user_id: str,
    ) -> JobSnapshot | None:
        owner = self._require_owner(owner_user_id)
        now = datetime.now(UTC)
        result = await self.client.eval(
            _REQUEST_CANCEL,
            4,
            self._job_key(job_id),
            self._active_key(),
            self._owner_active_key(owner),
            self._terminal_key(),
            owner,
            now.isoformat(),
            job_id,
            self.ttl_seconds,
            now.timestamp(),
        )
        if int(result[0]) == -1:
            return None
        snapshot = await self.get(job_id, owner_user_id=owner)
        if int(result[0]) == 0 and snapshot is not None:
            event = JobEvent(
                state=snapshot.state,
                stage=snapshot.stage,
                progress=snapshot.progress,
                message=snapshot.message,
                timestamp=snapshot.updated_at,
                error=snapshot.error,
            )
            pipe = self.client.pipeline(transaction=False)
            pipe.rpush(self._events_key(job_id), event.model_dump_json())
            pipe.ltrim(self._events_key(job_id), -self.max_events, -1)
            pipe.expire(self._events_key(job_id), self.ttl_seconds)
            await pipe.execute()
        return snapshot

    async def cancel_and_wait(
        self,
        job_id: str,
        *,
        owner_user_id: str,
    ) -> JobSnapshot | None:
        # Running work is cooperatively cancelled at its next progress
        # checkpoint. Do not block an HTTP worker while an external model call
        # is in flight.
        return await self.cancel(job_id, owner_user_id=owner_user_id)

    async def mark_running(
        self,
        job_id: str,
        owner_user_id: str,
    ) -> bool:
        return await self.update_progress(
            job_id,
            owner_user_id,
            "starting",
            0.02,
            "正在启动分布式分析",
        )

    async def update_progress(
        self,
        job_id: str,
        owner_user_id: str,
        stage: str,
        progress: float,
        message: str,
    ) -> bool:
        owner = self._require_owner(owner_user_id)
        now = datetime.now(UTC)
        event = JobEvent(
            state=JobState.RUNNING,
            stage=stage,
            progress=max(0, min(float(progress), 0.99)),
            message=message,
            timestamp=now,
        )
        result = await self.client.eval(
            _UPDATE_PROGRESS,
            2,
            self._job_key(job_id),
            self._events_key(job_id),
            owner,
            stage[:100],
            event.progress,
            message[:1000],
            now.isoformat(),
            event.model_dump_json(),
            self.max_events,
            self.ttl_seconds,
        )
        code = int(result[0])
        if code == 2:
            raise DistributedJobCancelled
        if code == -1:
            raise LookupError(f"Distributed job not found: {job_id}")
        return code == 0

    async def finish(
        self,
        job_id: str,
        owner_user_id: str,
        state: JobState,
        *,
        result: AnalysisResult | None = None,
        error: str | None = None,
        persistence_error: str | None = None,
    ) -> JobSnapshot | None:
        if state not in {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }:
            raise ValueError("finish requires a terminal state")
        owner = self._require_owner(owner_user_id)
        now = datetime.now(UTC)
        requested_message = {
            JobState.COMPLETED: "分析完成",
            JobState.FAILED: "分析失败",
            JobState.CANCELLED: "任务已取消",
        }[state]
        response = await self.client.eval(
            _FINISH_JOB,
            5,
            self._job_key(job_id),
            self._active_key(),
            self._owner_active_key(owner),
            self._events_key(job_id),
            self._terminal_key(),
            owner,
            state.value,
            requested_message,
            result.model_dump_json() if result is not None else "",
            (error or "")[:1000],
            now.isoformat(),
            (persistence_error or "")[:1000],
            job_id,
            "",
            self.max_events,
            self.ttl_seconds,
            now.timestamp(),
        )
        if int(response[0]) == -1:
            return None
        snapshot = await self.get(job_id, owner_user_id=owner)
        if int(response[0]) == 0 and snapshot is not None:
            event = JobEvent(
                state=snapshot.state,
                stage=snapshot.stage,
                progress=snapshot.progress,
                message=snapshot.message,
                timestamp=snapshot.updated_at,
                error=snapshot.error,
            )
            pipe = self.client.pipeline(transaction=False)
            pipe.rpush(self._events_key(job_id), event.model_dump_json())
            pipe.ltrim(self._events_key(job_id), -self.max_events, -1)
            pipe.expire(self._events_key(job_id), self.ttl_seconds)
            await pipe.execute()
        return snapshot

    async def payload(
        self,
        job_id: str,
    ) -> DistributedAnalysisPayload | None:
        value = await self.client.hget(self._job_key(job_id), "payload")
        if not value:
            return None
        return DistributedAnalysisPayload.model_validate_json(value)

    async def pending_terminal_job_ids(self, limit: int = 50) -> list[str]:
        return list(
            await self.client.zrange(
                self._terminal_key(),
                0,
                max(0, min(int(limit), 500) - 1),
            )
        )

    async def acknowledge_terminal(self, job_id: str) -> None:
        await self.client.zrem(self._terminal_key(), job_id)

    async def _read_job(self, job_id: str) -> dict[str, str]:
        try:
            return await self.client.hgetall(self._job_key(job_id))
        except RedisError as exc:
            raise DistributedJobUnavailable(
                "Redis job backend is unavailable."
            ) from exc

    def _snapshot(self, job_id: str, values: dict[str, str]) -> JobSnapshot:
        state = JobState(values["state"])
        return JobSnapshot(
            id=job_id,
            state=state,
            stage=values.get("stage", state.value),
            progress=float(values.get("progress", 0)),
            message=values.get("message", ""),
            created_at=datetime.fromisoformat(values["created_at"]),
            updated_at=datetime.fromisoformat(values["updated_at"]),
            result_url=(
                f"/jobs/{job_id}/result"
                if state == JobState.COMPLETED
                else None
            ),
            error=values.get("error") or None,
            persistence_error=values.get("persistence_error") or None,
            revision=int(values.get("revision", 0)),
        )

    def _job_keys(self, job_id: str, owner: str) -> tuple[str, ...]:
        return (
            self._job_key(job_id),
            self._active_key(),
            self._owner_active_key(owner),
            self._owner_jobs_key(owner),
            self._events_key(job_id),
        )

    def _job_key(self, job_id: str) -> str:
        return f"{self._tag}:job:{job_id}"

    def _events_key(self, job_id: str) -> str:
        return f"{self._tag}:job:{job_id}:events"

    def _active_key(self) -> str:
        return f"{self._tag}:jobs:active"

    def _owner_active_key(self, owner: str) -> str:
        return f"{self._tag}:owner:{owner}:active"

    def _owner_jobs_key(self, owner: str) -> str:
        return f"{self._tag}:owner:{owner}:jobs"

    def _terminal_key(self) -> str:
        return f"{self._tag}:jobs:terminal-pending"

    @staticmethod
    def _require_owner(owner_user_id: str) -> str:
        if not isinstance(owner_user_id, str) or not owner_user_id.strip():
            raise ValueError("owner_user_id is required for job access")
        return owner_user_id
