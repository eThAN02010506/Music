from __future__ import annotations

import asyncio

import pytest

from music_insight.api.app import app
from music_insight.api.dependencies import _account_store, _history_store
from music_insight.api.jobs import AnalysisJobStore
from music_insight.api.services.auth import AuthRateLimiter
from music_insight.config import get_settings


@pytest.fixture(autouse=True)
def isolate_runtime_workspace(tmp_path, monkeypatch):
    """Keep every test away from the developer's real workspace and database."""

    workspace = tmp_path / "runtime-workspace"
    monkeypatch.setenv("MUSIC_INSIGHT_WORKSPACE_DIR", str(workspace))
    get_settings.cache_clear()
    _account_store.cache_clear()
    _history_store.cache_clear()
    app.dependency_overrides.clear()
    app.state.jobs = AnalysisJobStore()
    app.state.auth_rate_limiter = AuthRateLimiter()
    app.state.registration_lock = asyncio.Lock()
    yield
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    _account_store.cache_clear()
    _history_store.cache_clear()
