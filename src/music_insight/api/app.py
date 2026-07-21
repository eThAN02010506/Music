import asyncio
from functools import lru_cache
import json
from pathlib import Path
import shutil

from fastapi import Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse

from music_insight.adapters.dsp import BasicDspAdapter
from music_insight.adapters.local_omni import (
    LocalModelConfigurationError,
    LocalOmniServer,
    ManagedLocalOmniAdapter,
)
from music_insight.adapters.qwen_omni_unified import QwenOmniUnifiedAdapter
from music_insight.api.history import (
    HistoryDetail,
    HistoryRename,
    HistoryStore,
    HistorySummary,
)
from music_insight.api.debug import DEBUG_PAGE, debug_state, diagnostic_report, task_detail
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


@lru_cache
def _history_store(database_path: str) -> HistoryStore:
    return HistoryStore(Path(database_path))


def get_history_store(settings: Settings = Depends(get_settings)) -> HistoryStore:
    return _history_store(str(settings.workspace_dir / "history.sqlite3"))


@lru_cache
def _local_server(root: str, endpoint: str, executable: str) -> LocalOmniServer:
    return LocalOmniServer(root=Path(root), endpoint=endpoint, executable=executable)


def get_orchestrator(
    settings: Settings = Depends(get_settings),
    *,
    model_source: str = "network",
    model_endpoint: str | None = None,
    local_model_path: str | None = None,
) -> AnalysisOrchestrator:
    if model_source == "local":
        if not local_model_path:
            raise HTTPException(status_code=422, detail="Local model path is required.")
        server = _local_server(
            str(settings.local_model_root),
            settings.local_omni_endpoint,
            settings.local_llama_server,
        )
        try:
            server.resolve(local_model_path)
        except LocalModelConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if shutil.which(settings.local_llama_server) is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"未找到 {settings.local_llama_server}。请先安装支持该 Qwen Omni "
                    "GGUF 的 llama.cpp，或填写本机 OpenAI 兼容接口地址。"
                ),
            )
        unified = ManagedLocalOmniAdapter(
            server=server,
            model_path=local_model_path,
            completions_path=settings.omni_completions_path,
            models_path=settings.omni_models_path,
            model=settings.omni_model,
            chunk_seconds=settings.omni_chunk_seconds,
        )
    else:
        from urllib.parse import urlsplit

        endpoint = (model_endpoint or settings.omni_endpoint).strip().rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HTTPException(status_code=422, detail="Model endpoint must use http or https.")
        unified = QwenOmniUnifiedAdapter(
            endpoint=endpoint,
            completions_path=settings.omni_completions_path,
            models_path=settings.omni_models_path,
            model=settings.omni_model,
            chunk_seconds=settings.omni_chunk_seconds,
        )
    return AnalysisOrchestrator(
        unified=unified,
        dsp=BasicDspAdapter(),
        preprocessor=Preprocessor(
            workspace_dir=settings.workspace_dir,
        ),
    )


@app.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict[str, str | bool]:
    return {
        "status": "ok",
        "model_endpoint": settings.omni_endpoint,
        "mode": "local-api",
        "local_model_root": str(settings.local_model_root.resolve()),
        "local_runner_available": shutil.which(settings.local_llama_server) is not None,
    }


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(DEBUG_PAGE)


@app.get("/api/info")
async def api_info() -> dict[str, object]:
    return {
        "name": "Music Insight",
        "status": "ok",
        "endpoints": {
            "health": "/health",
            "analyze": "POST /analyze",
            "markdown": "POST /analyze/markdown",
            "jobs": "POST /jobs",
            "history": "GET /history",
            "docs": "/docs",
        },
        "pipeline": {
            "unified_model": "Qwen3-Omni 8004",
            "acoustic_metrics": "librosa (local)",
            "strategy": "30-second audio chunks + same-model text fusion",
        },
    }


@app.get("/debug/state")
async def get_debug_state(
    settings: Settings = Depends(get_settings),
    history: HistoryStore = Depends(get_history_store),
) -> dict[str, object]:
    return debug_state(jobs, history, settings)


@app.get("/debug/report", response_class=PlainTextResponse)
async def get_debug_report(
    settings: Settings = Depends(get_settings),
    history: HistoryStore = Depends(get_history_store),
) -> PlainTextResponse:
    report = diagnostic_report(debug_state(jobs, history, settings))
    return PlainTextResponse(
        report,
        headers={
            "Content-Disposition": "attachment; filename=music-insight-debug.json"
        },
    )


@app.get("/debug/tasks/{task_id}")
async def get_debug_task(
    task_id: str,
    history: HistoryStore = Depends(get_history_store),
) -> dict[str, object]:
    detail = task_detail(task_id, jobs, history)
    if detail is None:
        raise HTTPException(status_code=404, detail="Debug task not found.")
    return detail


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
    model_source: str = Form(default="network"),
    model_endpoint: str | None = Form(default=None),
    local_model_path: str | None = Form(default=None),
    settings: Settings = Depends(get_settings),
) -> JobSnapshot:
    file_name = file.filename or "audio"
    if model_source not in {"network", "local"}:
        raise HTTPException(status_code=422, detail="Unsupported model source.")
    orchestrator = get_orchestrator(
        settings,
        model_source=model_source,
        model_endpoint=model_endpoint,
        local_model_path=local_model_path,
    )
    asset = await _save_asset(file, language, settings)
    history = get_history_store(settings)

    async def work(update):
        return await orchestrator.analyze(asset, progress=update)

    def observe(snapshot: JobSnapshot, result: AnalysisResult | None) -> None:
        history.update(
            snapshot.id,
            state=snapshot.state.value,
            updated_at=snapshot.updated_at,
            result=result if snapshot.state == JobState.COMPLETED else None,
            error=snapshot.error,
        )

    snapshot = jobs.create(work, observer=observe)
    history.create(
        job_id=snapshot.id,
        title=Path(file_name).stem,
        file_name=file_name,
        language=language,
        state=snapshot.state.value,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        audio_path=asset.path,
        model_source=model_source,
        model_location=(
            local_model_path if model_source == "local"
            else (model_endpoint or settings.omni_endpoint)
        ),
    )
    return snapshot


@app.get("/history", response_model=list[HistorySummary])
async def list_history(
    limit: int = 100,
    history: HistoryStore = Depends(get_history_store),
) -> list[HistorySummary]:
    return history.list(limit=limit)


@app.get("/history/{history_id}", response_model=HistoryDetail)
async def get_history(
    history_id: str,
    history: HistoryStore = Depends(get_history_store),
) -> HistoryDetail:
    entry = history.get(history_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Analysis history not found.")
    return entry


@app.patch("/history/{history_id}", response_model=HistoryDetail)
async def rename_history(
    history_id: str,
    payload: HistoryRename,
    history: HistoryStore = Depends(get_history_store),
) -> HistoryDetail:
    entry = history.rename(history_id, payload.title)
    if entry is None:
        raise HTTPException(status_code=404, detail="Analysis history not found.")
    return entry


@app.delete("/history/{history_id}", status_code=204)
async def delete_history(
    history_id: str,
    history: HistoryStore = Depends(get_history_store),
) -> Response:
    snapshot = jobs.get(history_id)
    if snapshot and snapshot.state in {JobState.QUEUED, JobState.RUNNING}:
        raise HTTPException(status_code=409, detail="Cancel the running job first.")
    if not history.delete(history_id):
        raise HTTPException(status_code=404, detail="Analysis history not found.")
    return Response(status_code=204)


@app.get("/history/{history_id}/audio")
async def get_history_audio(
    history_id: str,
    history: HistoryStore = Depends(get_history_store),
) -> FileResponse:
    path = history.audio_path(history_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Cached audio not found.")
    return FileResponse(path, filename=path.name)


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
