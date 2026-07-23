import asyncio

from music_insight.api.app import app
from music_insight.api.jobs import AnalysisJobStore, JobState, snapshot_event
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

        created = store.create(work)
        for _ in range(20):
            await asyncio.sleep(0)
            snapshot = store.get(created.id)
            if snapshot and snapshot.state == JobState.COMPLETED:
                break
        return store, created.id, snapshot

    store, job_id, snapshot = asyncio.run(exercise())

    assert snapshot is not None
    assert snapshot.state == JobState.COMPLETED
    assert snapshot.progress == 1
    assert snapshot.result_url == f"/jobs/{job_id}/result"
    assert store.result(job_id).summary == "完成"
    assert store.list()[0].id == job_id
    assert [event.stage for event in store.events(job_id)] == [
        "queued",
        "starting",
        "audio_analysis",
        "completed",
    ]
    assert "event: progress" in snapshot_event(snapshot)


def test_api_exposes_background_job_routes():
    paths = {route.path for route in app.routes}

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

        created = store.create(work, observer=broken_observer)
        for _ in range(20):
            await asyncio.sleep(0)
            snapshot = store.get(created.id)
            if snapshot and snapshot.state == JobState.COMPLETED:
                return store, snapshot
        raise AssertionError("job did not complete")

    store, snapshot = asyncio.run(exercise())

    assert snapshot.state == JobState.COMPLETED
    assert snapshot.persistence_error == "history disk unavailable"
    assert store.result(snapshot.id).summary == "完成"


def test_job_store_bounds_terminal_jobs_and_supports_removal():
    async def exercise():
        store = AnalysisJobStore(max_completed=2)
        ids = []

        async def work(update):
            return _result()

        for _ in range(3):
            created = store.create(work)
            ids.append(created.id)
            for _ in range(20):
                await asyncio.sleep(0)
                snapshot = store.get(created.id)
                if snapshot and snapshot.state == JobState.COMPLETED:
                    break
        return store, ids

    store, ids = asyncio.run(exercise())

    assert store.get(ids[0]) is None
    assert [item.id for item in store.list()] == list(reversed(ids[1:]))
    assert store.remove(ids[1]) is True
    assert store.get(ids[1]) is None
