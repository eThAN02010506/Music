from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from music_insight.api.accounts import AccountStore, UserPublic
from music_insight.api.history import HistoryStore
from music_insight.api.jobs import AnalysisJobStore
from music_insight.api.orchestrator_factory import (
    build_orchestrator,
    validate_private_model_endpoint,
)
from music_insight.adapters.local_omni import LocalOmniServer
from music_insight.api.session import SESSION_COOKIE, SESSION_MAX_AGE_SECONDS
from music_insight.config import Settings, get_settings
from music_insight.pipeline.orchestrator import AnalysisOrchestrator


__all__ = [
    "SESSION_COOKIE",
    "SESSION_MAX_AGE_SECONDS",
    "get_account_store",
    "get_current_user",
    "get_history_store",
    "get_job_store",
    "get_orchestrator",
    "validate_private_model_endpoint",
]


@lru_cache
def _history_store(
    database_path: str,
    shared_audio_dir: str | None = None,
) -> HistoryStore:
    source_roots = (
        (Path(shared_audio_dir),)
        if shared_audio_dir is not None
        else ()
    )
    return HistoryStore(
        Path(database_path),
        source_roots=source_roots,
    )


def get_history_store(settings: Settings = Depends(get_settings)) -> HistoryStore:
    return _history_store(
        str(settings.workspace_dir / "history.sqlite3"),
        (
            str(settings.shared_audio_dir)
            if settings.job_backend == "redis"
            and settings.shared_audio_dir is not None
            else None
        ),
    )


@lru_cache
def _account_store(database_path: str) -> AccountStore:
    return AccountStore(Path(database_path))


def get_account_store(settings: Settings = Depends(get_settings)) -> AccountStore:
    return _account_store(str(settings.workspace_dir / "history.sqlite3"))


def get_job_store(request: Request) -> AnalysisJobStore:
    return request.app.state.jobs


async def get_current_user(
    request: Request,
    accounts: AccountStore = Depends(get_account_store),
) -> UserPublic:
    token = request.cookies.get(SESSION_COOKIE, "")
    user = await run_in_threadpool(accounts.user_for_token, token)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录。")
    return user


def get_orchestrator(
    settings: Settings = Depends(get_settings),
    *,
    model_source: str = "network",
    model_endpoint: str | None = None,
    local_model_path: str | None = None,
    local_server: LocalOmniServer | None = None,
) -> AnalysisOrchestrator:
    return build_orchestrator(
        settings,
        model_source=model_source,
        model_endpoint=model_endpoint,
        local_model_path=local_model_path,
        local_server=local_server,
    )
