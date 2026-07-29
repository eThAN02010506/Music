import asyncio
import threading

import pytest

from music_insight.api.app import app
from music_insight.api.jobs import (
    AnalysisJobStore,
    JobCapacityError,
    JobState,
    snapshot_event,
)
from music_insight.schemas import AnalysisResult, DspResult


def _result() -> AnalysisResult:
    return AnalysisResult(
        summary="完成",
        lyrics=[],
        instruments=[],
        sound_events=[],
        emotion_timeline=[],
        inferred_atmosphere=[],
        themes=[],
        technical_metrics=DspResult(),
        evidence=[],
    )


def test_job_store_runs_work_and_exposes_result():
    async def exercise():
        store = AnalysisJobStore()

        async def work(update):
            await update("audio_analysis", 0.5, "正在聆听")
            return _result()

        created = store.create(work, owner_user_id="owner-a")
        for _ in range(20):
            await asyncio.sleep(0)
            snapshot = store.get(created.id, owner_user_id="owner-a")
            if snapshot and snapshot.state == JobState.COMPLETED:
                break
        return store, created.id, snapshot

    store, job_id, snapshot = asyncio.run(exercise())

    assert snapshot is not None
    assert snapshot.state == JobState.COMPLETED
    assert snapshot.progress == 1
    assert snapshot.result_url == f"/jobs/{job_id}/result"
    assert store.result(job_id, owner_user_id="owner-a").summary == "完成"
    assert store.list(owner_user_id="owner-a")[0].id == job_id
    assert [
        event.stage
        for event in store.events(job_id, owner_user_id="owner-a")
    ] == [
        "queued",
        "starting",
        "audio_analysis",
        "completed",
    ]
    assert "event: progress" in snapshot_event(snapshot)


def test_api_exposes_background_job_routes():
    paths = set(app.openapi()["paths"])

    assert "/jobs" in paths
    assert "/jobs/{job_id}" in paths
    assert "/jobs/{job_id}/events" in paths
    assert "/jobs/{job_id}/result" in paths
    assert "/jobs/{job_id}/cancel" in paths
    assert "/debug/state" in paths
    assert "/debug/report" in paths
    assert "/debug/tasks/{task_id}" in paths
    assert "/models/probe" in paths
    assert "/api/info" in paths


def test_observer_failure_does_not_change_successful_analysis_to_failed():
    async def exercise():
        store = AnalysisJobStore()

        def broken_observer(snapshot, result):
            raise OSError("history disk unavailable")

        async def work(update):
            return _result()

        created = store.create(
            work,
            observer=broken_observer,
            owner_user_id="owner-a",
        )
        for _ in range(20):
            await asyncio.sleep(0)
            snapshot = store.get(created.id, owner_user_id="owner-a")
            if snapshot and snapshot.state == JobState.COMPLETED:
                return store, snapshot
        raise AssertionError("job did not complete")

    store, snapshot = asyncio.run(exercise())

    assert snapshot.state == JobState.COMPLETED
    assert snapshot.persistence_error == "history disk unavailable"
    assert store.result(snapshot.id, owner_user_id="owner-a").summary == "完成"


def test_job_store_bounds_terminal_jobs_and_supports_removal():
    async def exercise():
        store = AnalysisJobStore(max_completed=2)
        ids = []

        async def work(update):
            return _result()

        for _ in range(3):
            created = store.create(work, owner_user_id="owner-a")
            ids.append(created.id)
            for _ in range(20):
                await asyncio.sleep(0)
                snapshot = store.get(created.id, owner_user_id="owner-a")
                if snapshot and snapshot.state == JobState.COMPLETED:
                    break
        return store, ids

    store, ids = asyncio.run(exercise())

    assert store.get(ids[0], owner_user_id="owner-a") is None
    assert [
        item.id for item in store.list(owner_user_id="owner-a")
    ] == list(reversed(ids[1:]))
    assert store.remove(ids[1], owner_user_id="owner-a") is True
    assert store.get(ids[1], owner_user_id="owner-a") is None


def test_job_store_hides_jobs_results_and_events_from_other_owners():
    async def exercise():
        store = AnalysisJobStore()

        async def work(update):
            return _result()

        created = store.create(work, owner_user_id="owner-a")
        for _ in range(20):
            await asyncio.sleep(0)
            snapshot = store.get(created.id, owner_user_id="owner-a")
            if snapshot and snapshot.state == JobState.COMPLETED:
                break
        return store, created.id

    store, job_id = asyncio.run(exercise())

    assert store.get(job_id, owner_user_id="owner-a") is not None
    assert store.get(job_id, owner_user_id="owner-b") is None
    assert store.result(job_id, owner_user_id="owner-b") is None
    assert store.events(job_id, owner_user_id="owner-b") == []
    assert store.cancel(job_id, owner_user_id="owner-b") is None
    assert store.remove(job_id, owner_user_id="owner-b") is False
    assert store.list(owner_user_id="owner-b") == []


def test_job_store_business_access_fails_closed_without_owner():
    store = AnalysisJobStore()

    try:
        store.list()  # type: ignore[call-arg]
    except TypeError:
        pass
    else:
        raise AssertionError("unscoped list access must be impossible")

    try:
        store.get("missing")  # type: ignore[call-arg]
    except TypeError:
        pass
    else:
        raise AssertionError("unscoped get access must be impossible")


def test_cancel_waits_for_terminal_observer_before_removal():
    async def exercise():
        store = AnalysisJobStore()
        blocker = asyncio.Event()
        observed_states = []

        async def work(update):
            await blocker.wait()
            return _result()

        async def observer(snapshot, result):
            observed_states.append(snapshot.state)

        created = store.create(
            work,
            observer=observer,
            owner_user_id="owner-a",
        )
        requested = store.cancel(created.id, owner_user_id="owner-a")
        removed_early = store.remove(created.id, owner_user_id="owner-a")
        cancelled = await store.cancel_and_wait(
            created.id,
            owner_user_id="owner-a",
        )
        removed = store.remove(created.id, owner_user_id="owner-a")
        return requested, cancelled, removed_early, removed, store, observed_states

    requested, cancelled, removed_early, removed, store, observed_states = (
        asyncio.run(exercise())
    )

    assert requested is not None
    assert requested.state in {JobState.QUEUED, JobState.RUNNING}
    assert cancelled is not None
    assert cancelled.state == JobState.CANCELLED
    assert cancelled.stage == "cancelled"
    assert removed_early is False
    assert removed is True
    assert store.list(owner_user_id="owner-a") == []
    assert observed_states[-1] == JobState.CANCELLED


def test_cancelled_persistence_cannot_be_overwritten_by_older_running_update():
    async def exercise():
        store = AnalysisJobStore()
        persistence_started = threading.Event()
        release_persistence = threading.Event()
        persisted_states = []

        async def work(update):
            await asyncio.Event().wait()
            return _result()

        async def observer(snapshot, result):
            if snapshot.state == JobState.RUNNING and not persistence_started.is_set():
                def blocked_write():
                    persistence_started.set()
                    release_persistence.wait(timeout=2)
                    persisted_states.append(snapshot.state)

                await asyncio.to_thread(blocked_write)
            else:
                persisted_states.append(snapshot.state)

        created = store.create(
            work,
            observer=observer,
            owner_user_id="owner-a",
        )
        for _ in range(100):
            if persistence_started.is_set():
                break
            await asyncio.sleep(0)
        assert persistence_started.is_set()

        cancel_task = asyncio.create_task(
            store.cancel_and_wait(
                created.id,
                owner_user_id="owner-a",
            )
        )
        await asyncio.sleep(0)
        while_persisting = store.get(
            created.id,
            owner_user_id="owner-a",
        )
        release_persistence.set()
        cancelled = await cancel_task
        return cancelled, while_persisting, persisted_states

    cancelled, while_persisting, persisted_states = asyncio.run(exercise())

    assert cancelled is not None
    assert cancelled.state == JobState.CANCELLED
    assert while_persisting is not None
    assert while_persisting.state == JobState.RUNNING
    assert persisted_states[-1] == JobState.CANCELLED


def test_terminal_state_is_hidden_until_observer_finishes():
    async def exercise():
        store = AnalysisJobStore()
        terminal_observer_started = asyncio.Event()
        release_terminal_observer = asyncio.Event()

        async def work(update):
            return _result()

        async def observer(snapshot, result):
            if snapshot.state == JobState.COMPLETED:
                terminal_observer_started.set()
                await release_terminal_observer.wait()

        created = store.create(
            work,
            observer=observer,
            owner_user_id="owner-a",
        )
        await asyncio.wait_for(terminal_observer_started.wait(), timeout=1)
        while_persisting = store.get(
            created.id,
            owner_user_id="owner-a",
        )
        unpublished_result = store.result(
            created.id,
            owner_user_id="owner-a",
        )
        release_terminal_observer.set()
        completed = await store.wait(
            created.id,
            owner_user_id="owner-a",
        )
        return while_persisting, unpublished_result, completed

    while_persisting, unpublished_result, completed = asyncio.run(exercise())

    assert while_persisting is not None
    assert while_persisting.state == JobState.RUNNING
    assert unpublished_result is None
    assert completed is not None
    assert completed.state == JobState.COMPLETED


def test_completed_terminal_observer_wins_over_late_cancellation():
    async def exercise():
        store = AnalysisJobStore()
        terminal_observer_started = asyncio.Event()
        release_terminal_observer = asyncio.Event()
        observed_states = []

        async def work(update):
            return _result()

        async def observer(snapshot, result):
            observed_states.append(snapshot.state)
            if snapshot.state == JobState.COMPLETED:
                assert result is not None
                terminal_observer_started.set()
                await release_terminal_observer.wait()

        created = store.create(
            work,
            observer=observer,
            owner_user_id="owner-a",
        )
        await asyncio.wait_for(terminal_observer_started.wait(), timeout=1)
        cancel_task = asyncio.create_task(
            store.cancel_and_wait(
                created.id,
                owner_user_id="owner-a",
            )
        )
        await asyncio.sleep(0)
        while_persisting = store.get(
            created.id,
            owner_user_id="owner-a",
        )
        release_terminal_observer.set()
        completed = await asyncio.wait_for(cancel_task, timeout=1)
        events = store.events(created.id, owner_user_id="owner-a")
        result = store.result(created.id, owner_user_id="owner-a")
        return (
            while_persisting,
            completed,
            observed_states,
            events,
            result,
        )

    (
        while_persisting,
        completed,
        observed_states,
        events,
        result,
    ) = asyncio.run(exercise())

    assert while_persisting is not None
    assert while_persisting.state == JobState.RUNNING
    assert completed is not None
    assert completed.state == JobState.COMPLETED
    assert result is not None
    assert result.summary == "完成"
    assert observed_states[-1] == JobState.COMPLETED
    assert JobState.CANCELLED not in observed_states
    assert events[-1].state == JobState.COMPLETED
    assert all(event.state != JobState.CANCELLED for event in events)


def test_cancel_wins_when_work_swallows_cancelled_error():
    async def exercise():
        store = AnalysisJobStore()
        work_started = asyncio.Event()

        async def work(update):
            work_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return _result()

        created = store.create(work, owner_user_id="owner-a")
        await asyncio.wait_for(work_started.wait(), timeout=1)
        return await store.cancel_and_wait(
            created.id,
            owner_user_id="owner-a",
        ), store.result(created.id, owner_user_id="owner-a")

    cancelled, result = asyncio.run(exercise())

    assert cancelled is not None
    assert cancelled.state == JobState.CANCELLED
    assert result is None


def test_job_store_enforces_owner_and_global_active_capacity():
    async def exercise():
        store = AnalysisJobStore(
            max_active=2,
            max_active_per_owner=1,
        )
        started = asyncio.Event()

        async def work(update):
            started.set()
            await asyncio.Event().wait()

        first = store.create(work, owner_user_id="owner-a")
        await asyncio.wait_for(started.wait(), timeout=1)

        with pytest.raises(JobCapacityError) as owner_error:
            store.create(work, owner_user_id="owner-a")
        second = store.create(work, owner_user_id="owner-b")
        with pytest.raises(JobCapacityError) as global_error:
            store.create(work, owner_user_id="owner-c")

        await store.shutdown()
        return store, first.id, second.id, owner_error.value, global_error.value

    store, first_id, second_id, owner_error, global_error = asyncio.run(
        exercise()
    )

    assert owner_error.global_limit is False
    assert global_error.global_limit is True
    assert store.get(first_id, owner_user_id="owner-a").state == JobState.CANCELLED
    assert store.get(second_id, owner_user_id="owner-b").state == JobState.CANCELLED


def test_job_store_shutdown_settles_queued_and_running_work():
    async def exercise():
        store = AnalysisJobStore(max_active=4, max_active_per_owner=4)
        blocker = asyncio.Event()

        async def work(update):
            await blocker.wait()

        jobs = [
            store.create(work, owner_user_id="owner-a")
            for _ in range(3)
        ]
        await asyncio.sleep(0)
        await store.shutdown()
        return [
            store.get(job.id, owner_user_id="owner-a")
            for job in jobs
        ]

    snapshots = asyncio.run(exercise())

    assert all(snapshot is not None for snapshot in snapshots)
    assert {snapshot.state for snapshot in snapshots} == {JobState.CANCELLED}
