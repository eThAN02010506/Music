import asyncio
from datetime import UTC, datetime, timedelta
import io
import os
import threading
import wave

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

from music_insight.api.app import create_app
from music_insight.api.history import HistoryStore
from music_insight.api.jobs import AnalysisJobStore, JobState
from music_insight.api.services.analysis import submit_analysis_job
from music_insight.api.services.auth import AuthRateLimiter
from music_insight.config import Settings, get_settings
from music_insight.schemas import AnalysisResult, DspResult
from music_insight.storage.assets import AssetCleanupReport


class _HistoryStub:
    def __init__(self, *, fail_create: bool = False) -> None:
        self.fail_create = fail_create
        self.created = []
        self.updated = []
        self.deleted = []

    def create(self, **values):
        if self.fail_create:
            raise OSError("database unavailable")
        self.created.append(values)

    def update(self, job_id, **values):
        self.updated.append((job_id, values))

    def delete(self, history_id, *, user_id):
        self.deleted.append((history_id, user_id))
        return bool(self.created)


class _BlockingCreateHistoryStub(_HistoryStub):
    def __init__(self) -> None:
        super().__init__()
        self.create_started = threading.Event()
        self.release_create = threading.Event()

    def create(self, **values):
        self.create_started.set()
        if not self.release_create.wait(timeout=2):
            raise TimeoutError("test did not release history.create")
        super().create(**values)


class _BlockingTerminalHistoryStub(_HistoryStub):
    def __init__(self) -> None:
        super().__init__()
        self.terminal_started = threading.Event()
        self.release_terminal = threading.Event()

    def update(self, job_id, **values):
        if values["state"] == "completed":
            self.terminal_started.set()
            if not self.release_terminal.wait(timeout=2):
                raise TimeoutError("test did not release terminal update")
        super().update(job_id, **values)


class _OrchestratorStub:
    async def analyze(self, asset, progress):
        for value in (0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16):
            await progress("audio_analysis", value, "working")
        return AnalysisResult(
            summary="done",
            lyrics=[],
            instruments=[],
            sound_events=[],
            emotion_timeline=[],
            inferred_atmosphere=[],
            themes=[],
            technical_metrics=DspResult(),
            evidence=[],
        )


class _BrokenJobStore:
    def ensure_capacity(self, *args, **kwargs):
        return None

    def create(self, *args, **kwargs):
        raise RuntimeError("job store unavailable")

    def cancel(self, *args, **kwargs):
        raise AssertionError("No job should exist.")

    def remove(self, *args, **kwargs):
        raise AssertionError("No job should exist.")


def _wav_bytes(seconds: float = 1.0, sample_rate: int = 8_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return buffer.getvalue()


def _upload() -> UploadFile:
    return UploadFile(
        file=io.BytesIO(_wav_bytes()),
        filename="song.wav",
        headers=Headers({"content-type": "audio/wav"}),
    )


def _request(username_host: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/login",
            "headers": [],
            "client": (username_host, 1234),
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


def test_create_app_preserves_public_route_contract():
    application = create_app()
    paths = set(application.openapi()["paths"])

    assert {
        "/auth/register",
        "/auth/login",
        "/auth/logout",
        "/auth/me",
        "/health",
        "/runtime-config",
        "/models/probe",
        "/jobs",
        "/jobs/{job_id}",
        "/jobs/{job_id}/events",
        "/jobs/{job_id}/result",
        "/history",
        "/history/{history_id}",
        "/history/{history_id}/lyrics/retry",
        "/history/{history_id}/singing/score",
        "/singing/compare",
        "/singing/attempts",
        "/singing/attempts/{attempt_id}",
        "/leaderboard",
        "/analyze",
        "/analyze/markdown",
    } <= paths


def test_health_is_public_but_runtime_model_config_requires_login():
    application = create_app()
    client = TestClient(application)

    health = client.get("/health")
    anonymous_config = client.get("/runtime-config")
    registered = client.post(
        "/auth/register",
        json={"username": "runtime-user", "password": "safe password"},
    )
    authenticated_config = client.get("/runtime-config")

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "mode": "local-api"}
    assert "model_endpoint" not in health.json()
    assert "local_model_root" not in health.json()
    assert anonymous_config.status_code == 401
    assert registered.status_code == 201
    assert authenticated_config.status_code == 200
    assert {
        "model_endpoint",
        "local_model_root",
        "local_runner_available",
        "job_backend",
    } == set(authenticated_config.json())


def test_auth_rate_limit_uses_account_normalization_and_expires_keys():
    now = [10.0]
    limiter = AuthRateLimiter(
        max_attempts=2,
        window_seconds=60,
        max_keys=2,
        clock=lambda: now[0],
    )
    request = _request()

    limiter.check(request, "Ａlice")
    limiter.check(request, "alice")
    with pytest.raises(HTTPException) as rate_error:
        limiter.check(request, "ALICE")
    assert rate_error.value.status_code == 429

    limiter.check(request, "bob")
    with pytest.raises(HTTPException) as capacity_error:
        limiter.check(request, "charlie")
    assert capacity_error.value.status_code == 429
    assert limiter.tracked_keys == 2

    now[0] += 61
    limiter.check(request, "charlie")
    assert limiter.tracked_keys == 1


@pytest.mark.parametrize(
    "history,jobs",
    [
        (_HistoryStub(fail_create=True), AnalysisJobStore()),
        (_HistoryStub(), _BrokenJobStore()),
    ],
)
def test_job_submission_failure_removes_upload_and_partial_state(
    tmp_path,
    history,
    jobs,
):
    async def exercise():
        with pytest.raises(HTTPException) as error:
            await submit_analysis_job(
                file=_upload(),
                language="en",
                model_source="network",
                model_endpoint=None,
                local_model_path=None,
                settings=Settings(workspace_dir=tmp_path),
                history=history,
                jobs=jobs,
                user_id="user-1",
                orchestrator=_OrchestratorStub(),
            )
        return error.value

    error = asyncio.run(exercise())

    assert error.status_code == 500
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]
    if isinstance(jobs, AnalysisJobStore):
        assert jobs.list(owner_user_id="user-1") == []


def test_job_observer_throttles_progress_and_always_persists_terminal_state(
    tmp_path,
):
    history = _HistoryStub()
    jobs = AnalysisJobStore()

    async def exercise():
        created = await submit_analysis_job(
            file=_upload(),
            language="en",
            model_source="network",
            model_endpoint=None,
            local_model_path=None,
            settings=Settings(workspace_dir=tmp_path),
            history=history,
            jobs=jobs,
            user_id="user-1",
            orchestrator=_OrchestratorStub(),
        )
        for _ in range(100):
            snapshot = jobs.get(created.id, owner_user_id="user-1")
            terminal_persisted = (
                history.updated
                and history.updated[-1][1]["state"] == "completed"
            )
            if (
                snapshot
                and snapshot.state == JobState.COMPLETED
                and terminal_persisted
            ):
                return snapshot
            await asyncio.sleep(0)
        raise AssertionError("job did not complete")

    snapshot = asyncio.run(exercise())

    assert snapshot.state == JobState.COMPLETED
    assert history.updated[-1][1]["state"] == "completed"
    assert len(history.updated) < 7


def test_cancelled_submission_waits_for_create_then_compensates_everything(
    tmp_path,
):
    history = _BlockingCreateHistoryStub()
    jobs = AnalysisJobStore()

    async def exercise():
        submission = asyncio.create_task(
            submit_analysis_job(
                file=_upload(),
                language="en",
                model_source="network",
                model_endpoint=None,
                local_model_path=None,
                settings=Settings(workspace_dir=tmp_path),
                history=history,
                jobs=jobs,
                user_id="user-1",
                orchestrator=_OrchestratorStub(),
            )
        )
        for _ in range(100):
            if history.create_started.is_set():
                break
            await asyncio.sleep(0)
        assert history.create_started.is_set()

        submission.cancel()
        await asyncio.sleep(0)
        assert not submission.done()
        history.release_create.set()
        with pytest.raises(asyncio.CancelledError):
            await submission

    asyncio.run(exercise())

    assert len(history.created) == 1
    assert history.deleted == [(history.created[0]["job_id"], "user-1")]
    assert jobs.list(owner_user_id="user-1") == []
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_completed_job_is_not_visible_before_history_terminal_update(
    tmp_path,
):
    history = _BlockingTerminalHistoryStub()
    jobs = AnalysisJobStore()

    async def exercise():
        created = await submit_analysis_job(
            file=_upload(),
            language="en",
            model_source="network",
            model_endpoint=None,
            local_model_path=None,
            settings=Settings(workspace_dir=tmp_path),
            history=history,
            jobs=jobs,
            user_id="user-1",
            orchestrator=_OrchestratorStub(),
        )
        for _ in range(200):
            if history.terminal_started.is_set():
                break
            await asyncio.sleep(0)
        assert history.terminal_started.is_set()

        while_persisting = jobs.get(
            created.id,
            owner_user_id="user-1",
        )
        persisted_state = (
            history.updated[-1][1]["state"]
            if history.updated
            else history.created[-1]["state"]
        )
        history.release_terminal.set()
        completed = await jobs.wait(
            created.id,
            owner_user_id="user-1",
        )
        return while_persisting, persisted_state, completed

    while_persisting, persisted_state, completed = asyncio.run(exercise())

    assert while_persisting is not None
    assert while_persisting.state == JobState.RUNNING
    assert persisted_state != "completed"
    assert completed is not None
    assert completed.state == JobState.COMPLETED
    assert history.updated[-1][1]["state"] == "completed"


def test_app_lifespan_runs_grace_period_asset_gc(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    temporary = workspace / "users" / "user-1" / "temporary"
    temporary.mkdir(parents=True)
    old_upload = temporary / "old.wav"
    old_upload.write_bytes(b"old")
    fresh_upload = temporary / "fresh.wav"
    fresh_upload.write_bytes(b"fresh")
    old_timestamp = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(old_upload, (old_timestamp, old_timestamp))
    monkeypatch.setenv("MUSIC_INSIGHT_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("MUSIC_INSIGHT_ASSET_GC_GRACE_HOURS", "24")
    get_settings.cache_clear()
    application = create_app()

    with TestClient(application) as client:
        assert client.get("/health").status_code == 200

    assert not old_upload.exists()
    assert fresh_upload.exists()
    assert application.state.asset_gc_error is None
    assert application.state.asset_gc_report == {
        "removed_count": 1,
        "reclaimed_bytes": 3,
        "grace_hours": 24.0,
    }


def test_app_startup_waits_for_asset_gc_before_serving(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MUSIC_INSIGHT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    get_settings.cache_clear()
    gc_started = threading.Event()
    release_gc = threading.Event()

    def blocked_gc(self, **kwargs):
        gc_started.set()
        if not release_gc.wait(timeout=5):
            raise TimeoutError("test did not release asset GC")
        return AssetCleanupReport((), 0)

    monkeypatch.setattr(HistoryStore, "garbage_collect_assets", blocked_gc)
    application = create_app()
    service_ready = threading.Event()
    responses = []

    def serve():
        with TestClient(application) as client:
            service_ready.set()
            responses.append(client.get("/health"))

    thread = threading.Thread(target=serve)
    thread.start()
    assert gc_started.wait(timeout=1)
    assert not service_ready.is_set()
    release_gc.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert service_ready.is_set()
    assert responses[0].status_code == 200
    assert application.state.asset_gc_error is None


def test_app_lifespan_reports_gc_failure_without_blocking_service(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MUSIC_INSIGHT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    get_settings.cache_clear()

    def broken_gc(self, **kwargs):
        raise OSError("cleanup unavailable")

    monkeypatch.setattr(HistoryStore, "garbage_collect_assets", broken_gc)
    application = create_app()

    with TestClient(application) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert application.state.asset_gc_report is None
    assert application.state.asset_gc_error == "cleanup unavailable"
