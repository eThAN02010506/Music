from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
import httpcore
import httpx
from pydantic import BaseModel, Field
from fastapi.responses import StreamingResponse

from music_insight.api.accounts import UserPublic
from music_insight.api.dependencies import (
    get_current_user,
    get_history_store,
    get_job_store,
    get_orchestrator,
)
from music_insight.api.history import HistoryStore
from music_insight.api.job_access import (
    cancel_job as cancel_job_in_store,
    get_job as get_job_from_store,
    get_result,
)
from music_insight.api.jobs import (
    AnalysisJobStore,
    JobSnapshot,
    JobState,
    snapshot_event,
)
from music_insight.api.services.analysis import (
    analyze_upload,
    analyze_upload_markdown,
    submit_analysis_job,
)
from music_insight.api.services.remote_audio import (
    RemoteAudioError,
    download_remote_audio,
    remote_audio_http_exception,
)
from music_insight.config import Settings, get_settings
from music_insight.schemas import AnalysisResult


router = APIRouter(tags=["analysis"])


class RemoteAudioJobRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    language: str | None = None
    model_source: str = "network"
    model_endpoint: str | None = None
    local_model_path: str | None = None


@router.post("/jobs", response_model=JobSnapshot, status_code=202)
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    model_source: str = Form(default="network"),
    model_endpoint: str | None = Form(default=None),
    local_model_path: str | None = Form(default=None),
    settings: Settings = Depends(get_settings),
    history: HistoryStore = Depends(get_history_store),
    jobs: AnalysisJobStore = Depends(get_job_store),
    user: UserPublic = Depends(get_current_user),
) -> JobSnapshot:
    if model_source not in {"network", "local"}:
        raise HTTPException(status_code=422, detail="Unsupported model source.")
    orchestrator = get_orchestrator(
        settings,
        model_source=model_source,
        model_endpoint=model_endpoint,
        local_model_path=local_model_path,
        local_server=request.app.state.local_server,
    )
    # Reserve bounded intake capacity before copying/probing the upload. The
    # durable background job has its own longer-lived queue capacity.
    async with request.app.state.direct_work_limiter.lease(user.id):
        return await submit_analysis_job(
            file=file,
            language=language,
            model_source=model_source,
            model_endpoint=model_endpoint,
            local_model_path=local_model_path,
            settings=settings,
            history=history,
            jobs=jobs,
            user_id=user.id,
            orchestrator=orchestrator,
            task_queue=getattr(request.app.state, "task_queue", None),
        )


@router.post("/jobs/from-url", response_model=JobSnapshot, status_code=202)
async def create_job_from_url(
    payload: RemoteAudioJobRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    history: HistoryStore = Depends(get_history_store),
    jobs: AnalysisJobStore = Depends(get_job_store),
    user: UserPublic = Depends(get_current_user),
) -> JobSnapshot:
    if payload.model_source not in {"network", "local"}:
        raise HTTPException(status_code=422, detail="Unsupported model source.")
    if payload.language not in {None, "zh", "en"}:
        raise HTTPException(status_code=422, detail="Unsupported language hint.")
    orchestrator = get_orchestrator(
        settings,
        model_source=payload.model_source,
        model_endpoint=payload.model_endpoint,
        local_model_path=payload.local_model_path,
        local_server=request.app.state.local_server,
    )
    async with request.app.state.direct_work_limiter.lease(user.id):
        try:
            async with asyncio.timeout(
                settings.remote_audio_timeout_seconds
            ):
                upload = await download_remote_audio(
                    payload.url,
                    max_bytes=settings.max_upload_mb * 1024 * 1024,
                    timeout_seconds=settings.remote_audio_timeout_seconds,
                    max_redirects=settings.remote_audio_max_redirects,
                )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail="远程音频下载超过总时间限制。",
            ) from exc
        except RemoteAudioError as exc:
            raise remote_audio_http_exception(exc) from exc
        except (
            httpcore.NetworkError,
            httpcore.TimeoutException,
            httpx.HTTPError,
        ) as exc:
            raise HTTPException(
                status_code=502,
                detail="无法安全读取远程音频。",
            ) from exc
        try:
            return await submit_analysis_job(
                file=upload,
                language=payload.language,
                model_source=payload.model_source,
                model_endpoint=payload.model_endpoint,
                local_model_path=payload.local_model_path,
                settings=settings,
                history=history,
                jobs=jobs,
                user_id=user.id,
                orchestrator=orchestrator,
                task_queue=getattr(request.app.state, "task_queue", None),
            )
        finally:
            await upload.close()


async def _job_or_404(
    jobs: AnalysisJobStore,
    job_id: str,
    user_id: str,
) -> JobSnapshot:
    snapshot = await get_job_from_store(
        jobs,
        job_id,
        owner_user_id=user_id,
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Analysis job not found.")
    return snapshot


@router.get("/jobs/{job_id}", response_model=JobSnapshot)
async def get_job(
    job_id: str,
    jobs: AnalysisJobStore = Depends(get_job_store),
    user: UserPublic = Depends(get_current_user),
) -> JobSnapshot:
    return await _job_or_404(jobs, job_id, user.id)


@router.get("/jobs/{job_id}/result", response_model=AnalysisResult)
async def get_job_result(
    job_id: str,
    jobs: AnalysisJobStore = Depends(get_job_store),
    user: UserPublic = Depends(get_current_user),
) -> AnalysisResult:
    snapshot = await _job_or_404(jobs, job_id, user.id)
    if snapshot.state != JobState.COMPLETED:
        raise HTTPException(status_code=409, detail="Analysis is not complete.")
    result = await get_result(jobs, job_id, owner_user_id=user.id)
    if result is None:
        raise HTTPException(status_code=500, detail="Completed job has no result.")
    return result


@router.post("/jobs/{job_id}/cancel", response_model=JobSnapshot)
async def cancel_job(
    job_id: str,
    jobs: AnalysisJobStore = Depends(get_job_store),
    user: UserPublic = Depends(get_current_user),
) -> JobSnapshot:
    await _job_or_404(jobs, job_id, user.id)
    snapshot = await cancel_job_in_store(
        jobs,
        job_id,
        owner_user_id=user.id,
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Analysis job not found.")
    return snapshot


@router.get("/jobs/{job_id}/events")
async def job_events(
    job_id: str,
    jobs: AnalysisJobStore = Depends(get_job_store),
    user: UserPublic = Depends(get_current_user),
) -> StreamingResponse:
    initial = await _job_or_404(jobs, job_id, user.id)
    owner_user_id = user.id

    async def events():
        last_revision = -1
        snapshot = initial
        while True:
            if snapshot.revision != last_revision:
                yield snapshot_event(snapshot)
                last_revision = snapshot.revision
            if snapshot.state in {
                JobState.COMPLETED,
                JobState.FAILED,
                JobState.CANCELLED,
            }:
                break
            await asyncio.sleep(0.5)
            current = await get_job_from_store(
                jobs,
                job_id,
                owner_user_id=owner_user_id,
            )
            if current is None:
                yield (
                    "event: error\ndata: "
                    f"{json.dumps({'detail': 'job removed'})}\n\n"
                )
                break
            snapshot = current

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/analyze", response_model=AnalysisResult)
async def analyze(
    request: Request,
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    settings: Settings = Depends(get_settings),
    user: UserPublic = Depends(get_current_user),
) -> AnalysisResult:
    orchestrator = get_orchestrator(
        settings,
        local_server=request.app.state.local_server,
    )
    async with request.app.state.direct_work_limiter.lease(user.id):
        return await analyze_upload(
            file=file,
            language=language,
            settings=settings,
            user_id=user.id,
            orchestrator=orchestrator,
        )


@router.post("/analyze/markdown")
async def analyze_markdown(
    request: Request,
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    settings: Settings = Depends(get_settings),
    user: UserPublic = Depends(get_current_user),
) -> dict[str, str]:
    orchestrator = get_orchestrator(
        settings,
        local_server=request.app.state.local_server,
    )
    async with request.app.state.direct_work_limiter.lease(user.id):
        markdown = await analyze_upload_markdown(
            file=file,
            language=language,
            settings=settings,
            user_id=user.id,
            orchestrator=orchestrator,
        )
    return {"markdown": markdown}
