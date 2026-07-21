import asyncio
import json

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from music_insight.adapters.dsp import BasicDspAdapter
from music_insight.adapters.qwen_omni_unified import QwenOmniUnifiedAdapter
from music_insight.api.jobs import AnalysisJobStore, JobSnapshot, JobState, snapshot_event
from music_insight.config import Settings, get_settings
from music_insight.pipeline.orchestrator import AnalysisOrchestrator
from music_insight.pipeline.preprocess import Preprocessor
from music_insight.reporting.markdown import render_markdown_report
from music_insight.schemas import AnalysisResult
from music_insight.storage.local import LocalAudioStore, UploadTooLargeError

app = FastAPI(title="Music Insight", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:5174",
        "http://localhost:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
jobs = AnalysisJobStore()


def get_orchestrator(settings: Settings = Depends(get_settings)) -> AnalysisOrchestrator:
    return AnalysisOrchestrator(
        unified=QwenOmniUnifiedAdapter(
            endpoint=settings.omni_endpoint,
            completions_path=settings.omni_completions_path,
            models_path=settings.omni_models_path,
            model=settings.omni_model,
            chunk_seconds=settings.omni_chunk_seconds,
        ),
        dsp=BasicDspAdapter(),
        preprocessor=Preprocessor(
            workspace_dir=settings.workspace_dir,
        ),
    )


@app.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {
        "status": "ok",
        "model_endpoint": settings.omni_endpoint,
        "mode": "local-api",
    }


@app.get("/")
async def root() -> dict[str, object]:
    return {
        "name": "Music Insight",
        "status": "ok",
        "endpoints": {
            "health": "/health",
            "analyze": "POST /analyze",
            "markdown": "POST /analyze/markdown",
            "jobs": "POST /jobs",
            "docs": "/docs",
        },
        "pipeline": {
            "unified_model": "Qwen3-Omni 8004",
            "acoustic_metrics": "librosa (local)",
            "strategy": "30-second audio chunks + same-model text fusion",
        },
    }


async def _save_asset(
    file: UploadFile,
    language: str | None,
    settings: Settings,
):
    if not file.content_type or not file.content_type.startswith("audio/"):
        raise HTTPException(status_code=415, detail="Only audio uploads are supported.")
    if language not in {None, "zh", "en"}:
        raise HTTPException(status_code=422, detail="Unsupported language hint.")
    store = LocalAudioStore(settings.workspace_dir / "uploads")
    try:
        asset = await store.save_upload(
            file,
            max_bytes=settings.max_upload_mb * 1024 * 1024,
        )
    except UploadTooLargeError as exc:
        raise HTTPException(
            status_code=413, detail="Upload exceeds configured size limit."
        ) from exc
    return asset.model_copy(update={"language_hint": language})


@app.post("/jobs", response_model=JobSnapshot, status_code=202)
async def create_job(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    settings: Settings = Depends(get_settings),
) -> JobSnapshot:
    asset = await _save_asset(file, language, settings)
    orchestrator = get_orchestrator(settings)

    async def work(update):
        return await orchestrator.analyze(asset, progress=update)

    return jobs.create(work)


def _job_or_404(job_id: str) -> JobSnapshot:
    snapshot = jobs.get(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Analysis job not found.")
    return snapshot


@app.get("/jobs/{job_id}", response_model=JobSnapshot)
async def get_job(job_id: str) -> JobSnapshot:
    return _job_or_404(job_id)


@app.get("/jobs/{job_id}/result", response_model=AnalysisResult)
async def get_job_result(job_id: str) -> AnalysisResult:
    snapshot = _job_or_404(job_id)
    if snapshot.state != JobState.COMPLETED:
        raise HTTPException(status_code=409, detail="Analysis is not complete.")
    result = jobs.result(job_id)
    if result is None:
        raise HTTPException(status_code=500, detail="Completed job has no result.")
    return result


@app.post("/jobs/{job_id}/cancel", response_model=JobSnapshot)
async def cancel_job(job_id: str) -> JobSnapshot:
    snapshot = jobs.cancel(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Analysis job not found.")
    return snapshot


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    initial = _job_or_404(job_id)

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
            current = jobs.get(job_id)
            if current is None:
                yield f"event: error\ndata: {json.dumps({'detail': 'job removed'})}\n\n"
                break
            snapshot = current

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/analyze", response_model=AnalysisResult)
async def analyze(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    settings: Settings = Depends(get_settings),
    orchestrator: AnalysisOrchestrator = Depends(get_orchestrator),
) -> AnalysisResult:
    asset = await _save_asset(file, language, settings)

    return await orchestrator.analyze(asset)


@app.post("/analyze/markdown")
async def analyze_markdown(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    settings: Settings = Depends(get_settings),
    orchestrator: AnalysisOrchestrator = Depends(get_orchestrator),
) -> dict[str, str]:
    result = await analyze(
        file=file,
        language=language,
        settings=settings,
        orchestrator=orchestrator,
    )
    return {"markdown": render_markdown_report(result)}
