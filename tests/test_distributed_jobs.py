from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import fakeredis.aioredis
import pytest

from music_insight.api.accounts import AccountStore
from music_insight.api.jobs import JobCapacityError, JobState
from music_insight.api.history import HistoryStore
from music_insight.config import Settings
from music_insight.distributed.celery_app import (
    TASK_NAME,
    create_celery_app,
)
from music_insight.distributed.jobs import (
    DistributedJobCancelled,
    RedisAnalysisJobStore,
)
from music_insight.distributed.payloads import DistributedAnalysisPayload
from music_insight.distributed.worker import _validated_worker_asset
from music_insight.distributed.reconcile import (
    reconcile_terminal_history_once,
)
from music_insight.schemas import AnalysisResult, AudioAsset, DspResult


def _result(summary: str = "完成") -> AnalysisResult:
    return AnalysisResult(
        summary=summary,
        lyrics=[],
        instruments=[],
        sound_events=[],
        emotion_timeline=[],
        inferred_atmosphere=[],
        themes=[],
        technical_metrics=DspResult(),
        evidence=[],
    )


def _payload(job_id: str, owner: str = "owner-a") -> DistributedAnalysisPayload:
    return DistributedAnalysisPayload(
        job_id=job_id,
        owner_user_id=owner,
        asset=AudioAsset(
            path=Path("/shared/users") / owner / "uploads" / "song.wav",
            media_type="audio/wav",
            size_bytes=10,
            language_hint="en",
            max_duration_s=1200,
        ),
        model_source="network",
        model_endpoint="http://192.168.1.97:8004",
    )


def _store(*, max_active: int = 8, per_owner: int = 3):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return RedisAnalysisJobStore(
        client,
        key_prefix="test-music",
        max_active=max_active,
        max_active_per_owner=per_owner,
        ttl_seconds=3600,
    )


def test_redis_job_store_preserves_owner_progress_events_and_result():
    async def exercise():
        store = _store()
        created = await store.create_distributed(_payload("job-1"))
        hidden = await store.get("job-1", owner_user_id="owner-b")
        assert await store.mark_running("job-1", "owner-a") is True
        await store.update_progress(
            "job-1",
            "owner-a",
            "audio_analysis",
            0.5,
            "正在聆听",
        )
        completed = await store.finish(
            "job-1",
            "owner-a",
            JobState.COMPLETED,
            result=_result(),
        )
        result = await store.result("job-1", owner_user_id="owner-a")
        events = await store.events("job-1", owner_user_id="owner-a")
        active = await store.active_job_ids()
        await store.shutdown()
        return created, hidden, completed, result, events, active

    created, hidden, completed, result, events, active = asyncio.run(exercise())

    assert created.state == JobState.QUEUED
    assert hidden is None
    assert completed is not None
    assert completed.state == JobState.COMPLETED
    assert completed.result_url == "/jobs/job-1/result"
    assert result is not None and result.summary == "完成"
    assert [event.stage for event in events] == [
        "queued",
        "starting",
        "audio_analysis",
        "completed",
    ]
    assert active == set()


def test_redis_job_capacity_is_atomic_across_concurrent_submitters():
    async def exercise():
        store = _store(max_active=3, per_owner=3)

        async def create(index: int):
            try:
                return await store.create_distributed(
                    _payload(f"job-{index}", owner=f"owner-{index}")
                )
            except JobCapacityError as exc:
                return exc

        results = await asyncio.gather(*(create(index) for index in range(12)))
        active = await store.active_job_ids()
        await store.shutdown()
        return results, active

    results, active = asyncio.run(exercise())

    accepted = [item for item in results if not isinstance(item, Exception)]
    rejected = [item for item in results if isinstance(item, JobCapacityError)]
    assert len(accepted) == 3
    assert len(rejected) == 9
    assert all(item.global_limit for item in rejected)
    assert len(active) == 3


def test_expired_job_members_cannot_permanently_leak_capacity():
    async def exercise():
        store = _store(max_active=1, per_owner=1)
        await store.client.sadd(store._active_key(), "expired-job")
        await store.client.sadd(
            store._owner_active_key("owner-a"),
            "expired-job",
        )
        await store.ensure_capacity("owner-a")
        created = await store.create_distributed(_payload("replacement"))
        active = await store.active_job_ids()
        await store.shutdown()
        return created, active

    created, active = asyncio.run(exercise())

    assert created.id == "replacement"
    assert active == {"replacement"}


def test_redis_job_cancel_releases_capacity_and_worker_observes_flag():
    async def exercise():
        store = _store(max_active=1, per_owner=1)
        await store.create_distributed(_payload("queued"))
        cancelled = await store.cancel("queued", owner_user_id="owner-a")
        replacement = await store.create_distributed(_payload("replacement"))
        await store.mark_running("replacement", "owner-a")
        await store.cancel("replacement", owner_user_id="owner-a")
        with pytest.raises(DistributedJobCancelled):
            await store.update_progress(
                "replacement",
                "owner-a",
                "dsp",
                0.3,
                "DSP",
            )
        terminal = await store.finish(
            "replacement",
            "owner-a",
            JobState.COMPLETED,
            result=_result(),
        )
        await store.shutdown()
        return cancelled, replacement, terminal

    cancelled, replacement, terminal = asyncio.run(exercise())

    assert cancelled is not None and cancelled.state == JobState.CANCELLED
    assert replacement.state == JobState.QUEUED
    assert terminal is not None and terminal.state == JobState.CANCELLED
    assert terminal.result_url is None


def test_celery_redis_configuration_is_json_only_and_late_acknowledged(tmp_path):
    settings = Settings(
        workspace_dir=tmp_path,
        job_backend="redis",
        shared_audio_dir=tmp_path / "shared-audio",
        redis_url="redis://127.0.0.1:6379/7",
    )
    app = create_celery_app(settings)

    assert app.conf.broker_url == settings.redis_url
    assert app.conf.result_backend == settings.redis_url
    assert app.conf.accept_content == ["json"]
    assert app.conf.task_serializer == "json"
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_routes[TASK_NAME]["queue"] == settings.celery_queue_name


def test_distributed_worker_rejects_cross_owner_or_changed_audio(tmp_path):
    workspace = tmp_path / "shared"
    valid = workspace / "users" / "owner-a" / "uploads" / "song.wav"
    valid.parent.mkdir(parents=True)
    valid.write_bytes(b"audio")
    settings = Settings(
        workspace_dir=tmp_path / "worker-cache",
        job_backend="redis",
        shared_audio_dir=workspace,
    )
    payload = _payload("job").model_copy(
        update={
            "asset": _payload("job").asset.model_copy(
                update={"path": valid, "size_bytes": len(b"audio")}
            )
        }
    )

    validated = _validated_worker_asset(payload, settings)
    assert validated.path == valid.resolve()

    outside = tmp_path / "other.wav"
    outside.write_bytes(b"audio")
    with pytest.raises(ValueError, match="outside"):
        _validated_worker_asset(
            payload.model_copy(
                update={
                    "asset": payload.asset.model_copy(update={"path": outside})
                }
            ),
            settings,
        )

    valid.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        _validated_worker_asset(payload, settings)


def test_redis_mode_requires_an_absolute_shared_audio_path():
    with pytest.raises(ValueError, match="shared_audio_dir"):
        Settings(
            job_backend="redis",
            shared_audio_dir=Path("relative-shared-audio"),
        )


def test_api_reconciles_worker_terminal_result_into_history(tmp_path):
    async def exercise():
        store = _store()
        source = tmp_path / "song.wav"
        source.write_bytes(b"audio")
        database_path = tmp_path / "history.sqlite3"
        owner = AccountStore(database_path).register(
            "distributed-user",
            "safe test password",
        )
        payload = _payload("job-1", owner.id).model_copy(
            update={
                "asset": _payload("job-1", owner.id).asset.model_copy(
                    update={
                        "path": source,
                        "size_bytes": source.stat().st_size,
                    }
                )
            }
        )
        created = await store.create_distributed(payload)
        history = HistoryStore(database_path)
        history.create(
            job_id=created.id,
            title="song",
            file_name="song.wav",
            language="en",
            state=created.state.value,
            created_at=created.created_at,
            updated_at=created.updated_at,
            audio_path=source,
            model_source="network",
            model_location="http://192.168.1.97:8004",
            user_id=payload.owner_user_id,
        )
        await store.mark_running(created.id, payload.owner_user_id)
        await store.finish(
            created.id,
            payload.owner_user_id,
            JobState.COMPLETED,
            result=_result("distributed"),
        )
        before = history.get(created.id, user_id=payload.owner_user_id)
        count = await reconcile_terminal_history_once(store, history)
        after = history.get(created.id, user_id=payload.owner_user_id)
        pending = await store.pending_terminal_job_ids()
        await store.shutdown()
        return before, count, after, pending

    before, count, after, pending = asyncio.run(exercise())

    assert before is not None and before.state == "queued"
    assert count == 1
    assert after is not None and after.state == "completed"
    assert after.result is not None and after.result.summary == "distributed"
    assert pending == []


def test_history_deletion_removes_source_from_dedicated_shared_audio_root(
    tmp_path,
):
    database_path = tmp_path / "api" / "history.sqlite3"
    shared = tmp_path / "shared-audio"
    source = shared / "users" / "placeholder" / "uploads" / "song.wav"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"audio")
    accounts = AccountStore(database_path)
    owner = accounts.register("shared-owner", "safe test password")
    owner_source = (
        shared / "users" / owner.id / "uploads" / "song.wav"
    )
    owner_source.parent.mkdir(parents=True)
    source.replace(owner_source)
    history = HistoryStore(database_path, source_roots=(shared,))
    now = datetime.now(UTC)
    history.create(
        job_id="shared-job",
        title="song",
        file_name="song.wav",
        language=None,
        state="cancelled",
        created_at=now,
        updated_at=now,
        audio_path=owner_source,
        user_id=owner.id,
    )

    assert history.delete("shared-job", user_id=owner.id) is True
    assert not owner_source.exists()
