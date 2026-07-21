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
    assert "event: progress" in snapshot_event(snapshot)


def test_api_exposes_background_job_routes():
    paths = {route.path for route in app.routes}

    assert "/jobs" in paths
    assert "/jobs/{job_id}" in paths
    assert "/jobs/{job_id}/events" in paths
    assert "/jobs/{job_id}/result" in paths
    assert "/jobs/{job_id}/cancel" in paths
