from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import inspect
import os
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from starlette.concurrency import run_in_threadpool

from music_insight import __version__
from music_insight.api.body_limit import RequestBodyLimitMiddleware
from music_insight.api.capacity import CapacityLimitError, CapacityLimiter
from music_insight.api.dependencies import (
    get_account_store,
    get_current_user,
    get_history_store,
    get_job_store,
    get_orchestrator,
    validate_private_model_endpoint,
)
from music_insight.api.jobs import AnalysisJobStore
from music_insight.api.orchestrator_factory import create_local_server
from music_insight.api.routers import auth, debug, history, jobs as job_routes
from music_insight.api.routers import singing, stems, system, teaching
from music_insight.api.services.auth import AuthRateLimiter
from music_insight.config import get_settings
from music_insight.distributed.jobs import (
    DistributedJobUnavailable,
    RedisAnalysisJobStore,
)
from music_insight.distributed.capacity import RedisCapacityLimiter
from music_insight.distributed.reconcile import reconcile_terminal_history
from music_insight.pipeline.resources import model_resources
from music_insight.stems import StemSeparationService


DEFAULT_WEB_ORIGINS = {
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:5174",
    "http://localhost:5174",
}


def configured_web_origins() -> set[str]:
    configured = {
        origin.strip().rstrip("/")
        for origin in os.getenv("MUSIC_INSIGHT_WEB_ORIGINS", "").split(",")
        if origin.strip()
    }
    return DEFAULT_WEB_ORIGINS | configured


ALLOWED_WEB_ORIGINS = configured_web_origins()


@asynccontextmanager
async def _lifespan(api: FastAPI):
    """Recover durable state before serving and settle owned resources on exit."""

    api.state.asset_gc_report = None
    api.state.asset_gc_error = None
    settings = get_settings()
    api.state.settings = settings
    history_store = await run_in_threadpool(get_history_store, settings)
    if getattr(api.state, "teaching_repository", None) is None:
        from music_insight.api.teaching import TeachingStore

        api.state.teaching_repository = await run_in_threadpool(
            TeachingStore,
            history_store.database_path,
        )
    api.state.recovered_teaching = {
        "understanding_maps": 0,
        "music_messages": 0,
    }
    api.state.recovered_teaching_error = None
    recover_teaching = getattr(
        api.state.teaching_repository,
        "recover_pending",
        None,
    )
    if callable(recover_teaching):
        try:
            # A grace period prevents a newly starting API worker from
            # invalidating work still owned by another healthy worker.
            recovery_options = (
                {
                    "before": datetime.now(UTC) - timedelta(minutes=30),
                }
                if "before" in inspect.signature(recover_teaching).parameters
                else {}
            )
            api.state.recovered_teaching = await run_in_threadpool(
                recover_teaching,
                **recovery_options,
            )
        except Exception as exc:
            api.state.recovered_teaching_error = (
                str(exc).strip() or exc.__class__.__name__
            )[:1000]
    active_job_ids: set[str] = set()
    terminal_reconciler: asyncio.Task[None] | None = None
    if isinstance(api.state.jobs, RedisAnalysisJobStore):
        await api.state.jobs.initialize()
        active_job_ids = await api.state.jobs.active_job_ids()
        active_job_ids.update(
            await api.state.jobs.pending_terminal_job_ids(500)
        )
    api.state.recovered_interrupted_jobs = await run_in_threadpool(
        history_store.recover_interrupted_jobs,
        active_job_ids=active_job_ids,
    )
    if isinstance(api.state.jobs, RedisAnalysisJobStore):
        terminal_reconciler = asyncio.create_task(
            reconcile_terminal_history(
                api.state.jobs,
                history_store,
                stale_after_seconds=settings.celery_soft_time_limit_seconds,
            )
        )

    try:
        report = await run_in_threadpool(
            history_store.garbage_collect_assets,
            min_age=timedelta(hours=settings.asset_gc_grace_hours),
        )
        api.state.asset_gc_report = {
            "removed_count": report.removed_count,
            "reclaimed_bytes": report.reclaimed_bytes,
            "grace_hours": settings.asset_gc_grace_hours,
        }
    except Exception as exc:
        # Cleanup is maintenance, not a prerequisite for serving requests.
        api.state.asset_gc_error = (
            str(exc).strip() or exc.__class__.__name__
        )[:1000]
    try:
        yield
    finally:
        if terminal_reconciler is not None:
            terminal_reconciler.cancel()
            await asyncio.gather(
                terminal_reconciler,
                return_exceptions=True,
            )
        if api.state.task_queue is not None:
            await run_in_threadpool(api.state.task_queue.close)
        await api.state.jobs.shutdown()
        await api.state.local_server.aclose()
        model_resources.clear_current_loop()


def create_app(
    *,
    job_store: AnalysisJobStore | None = None,
    auth_rate_limiter: AuthRateLimiter | None = None,
) -> FastAPI:
    """Build an isolated Music Insight API instance.

    Runtime-only state lives on the application instance, which keeps tests
    and future multi-process adapters from sharing accidental module globals.
    """

    api = FastAPI(
        title="Music Insight",
        version=__version__,
        lifespan=_lifespan,
    )
    allowed_origins = configured_web_origins()
    settings = get_settings()
    if job_store is not None:
        api.state.jobs = job_store
    elif settings.job_backend == "redis":
        api.state.jobs = RedisAnalysisJobStore.from_settings(settings)
    else:
        api.state.jobs = AnalysisJobStore(
            max_active=settings.max_active_jobs,
            max_active_per_owner=settings.max_active_jobs_per_user,
        )
    if isinstance(api.state.jobs, RedisAnalysisJobStore):
        from music_insight.distributed.celery_app import create_celery_app

        api.state.task_queue = create_celery_app(settings)
    else:
        api.state.task_queue = None
    api.state.local_server = create_local_server(settings)
    api.state.local_compute_gate = model_resources.gate(
        "music-insight://local-dsp",
        settings.dsp_max_concurrency,
    )
    if isinstance(api.state.jobs, RedisAnalysisJobStore):
        api.state.direct_work_limiter = RedisCapacityLimiter(
            api.state.jobs.client,
            key_prefix=settings.redis_key_prefix,
            max_active=settings.max_direct_work,
            max_active_per_owner=settings.max_direct_work_per_user,
            lease_ttl_seconds=settings.direct_work_lease_ttl_seconds,
            label="直接分析",
        )
    else:
        api.state.direct_work_limiter = CapacityLimiter(
            max_active=settings.max_direct_work,
            max_active_per_owner=settings.max_direct_work_per_user,
            label="直接分析",
        )
    api.state.auth_kdf_limiter = CapacityLimiter(
        max_active=settings.auth_kdf_max_concurrency,
        label="认证计算",
    )
    api.state.stem_service = StemSeparationService(settings)
    api.state.stem_compute_gate = asyncio.Semaphore(
        settings.stem_max_concurrency
    )
    api.state.auth_rate_limiter = auth_rate_limiter or AuthRateLimiter()
    api.state.registration_lock = asyncio.Lock()
    api.state.allowed_web_origins = frozenset(allowed_origins)
    api.state.teaching_repository = None
    api.state.teaching_model = None
    api.state.teaching_relisten_provider = None
    api.state.recovered_teaching = None
    api.state.recovered_teaching_error = None

    # Install the receive guard before the decorator middleware. Starlette
    # prepends each added middleware, so the final order below becomes:
    # CORS -> origin validation -> body/admission guard -> request parser.
    api.add_middleware(
        RequestBodyLimitMiddleware,
        max_upload_bytes=settings.max_upload_mb * 1024 * 1024,
        max_upload_units=settings.max_upload_units,
    )

    @api.middleware("http")
    async def verify_browser_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site", "").casefold()
            if fetch_site == "cross-site":
                return Response("Cross-site request not allowed.", status_code=403)
            if origin and origin not in api.state.allowed_web_origins:
                return Response("Origin not allowed.", status_code=403)
            if not origin:
                referer = request.headers.get("referer")
                if referer:
                    parsed = urlsplit(referer)
                    referer_origin = (
                        f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
                    )
                    if referer_origin not in api.state.allowed_web_origins:
                        return Response(
                            "Referrer not allowed.",
                            status_code=403,
                        )
        response = await call_next(request)
        # CSP is scoped to the Debug Console page (which uses an inline
        # script). The /docs Swagger UI loads its bundle from the FastAPI CDN,
        # so a self-only script-src would break it.
        if request.url.path == "/":
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "connect-src 'self'",
            )
        if request.url.path not in {
            "/",
            "/api/info",
            "/health",
            "/docs",
            "/openapi.json",
        }:
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @api.exception_handler(CapacityLimitError)
    async def capacity_limit_error(
        _request: Request,
        exc: CapacityLimitError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503 if exc.global_limit else 429,
            content={"detail": str(exc)},
            headers={"Retry-After": "1"},
        )

    @api.exception_handler(DistributedJobUnavailable)
    async def distributed_job_unavailable(
        _request: Request,
        exc: DistributedJobUnavailable,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc)},
            headers={"Retry-After": "2"},
        )

    @api.exception_handler(RedisError)
    async def redis_error(
        _request: Request,
        _exc: RedisError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "Redis job backend is unavailable."},
            headers={"Retry-After": "2"},
        )

    # Keep CORS outermost so early 413/503 responses remain visible to a
    # configured cross-origin browser frontend.
    api.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(allowed_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for grouped_router in (
        auth.router,
        system.router,
        debug.router,
        history.router,
        stems.router,
        teaching.router,
        singing.router,
        job_routes.router,
    ):
        api.include_router(grouped_router)
    return api


app = create_app()

# Compatibility aliases for callers that imported these from the original
# monolithic module. New code should depend on request.app.state or the
# dependency functions above.
jobs: AnalysisJobStore = app.state.jobs


__all__ = [
    "ALLOWED_WEB_ORIGINS",
    "app",
    "configured_web_origins",
    "create_app",
    "get_account_store",
    "get_current_user",
    "get_history_store",
    "get_job_store",
    "get_orchestrator",
    "jobs",
    "validate_private_model_endpoint",
]
