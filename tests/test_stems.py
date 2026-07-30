from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import wave

from fastapi.testclient import TestClient
import pytest

from music_insight.api.accounts import AccountStore
from music_insight.api.app import create_app
from music_insight.api.dependencies import get_account_store, get_history_store
from music_insight.api.history import HistoryStore
from music_insight.config import Settings
from music_insight.storage.assets import content_cache_key
from music_insight.stems import STEM_NAMES
from music_insight.stems.service import (
    StemSeparationError,
    StemSeparationService,
)


def _wav(path: Path, *, seconds: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\x00\x00\x00\x00" * int(8_000 * seconds))


class FakeSeparator:
    backend = "fake-demucs"

    def __init__(
        self,
        model: str,
        *,
        available: bool = True,
        duration_by_stem: dict[str, float] | None = None,
    ) -> None:
        self.model = model
        self.available = available
        self.duration_by_stem = duration_by_stem or {}
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def separate(self, _source: Path, output_root: Path) -> Path:
        self.calls += 1
        self.started.set()
        if self.block:
            await self.release.wait()
        generated = output_root / self.model
        generated.mkdir(parents=True)
        for stem in STEM_NAMES:
            _wav(
                generated / f"{stem}.wav",
                seconds=self.duration_by_stem.get(stem, 1.0),
            )
        return generated


def _service(
    tmp_path: Path,
    separator: FakeSeparator,
) -> StemSeparationService:
    return StemSeparationService(
        Settings(workspace_dir=tmp_path),
        separator=separator,
    )


def test_stem_service_generates_validates_and_reuses_content_cache(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.wav"
    _wav(source)
    separator = FakeSeparator("htdemucs")
    service = _service(tmp_path, separator)
    key = content_cache_key(source)

    async def exercise():
        first = await service.ensure(source=source, content_key=key)
        second = await service.ensure(source=source, content_key=key)
        status = await service.status(content_key=key)
        return first, second, status

    first, second, status = asyncio.run(exercise())

    assert separator.calls == 1
    assert first.state == second.state == status.state == "ready"
    assert set(first.paths) == set(STEM_NAMES)
    assert all(path.is_file() for path in first.paths.values())
    assert not list((tmp_path / "stems").glob(".tmp-*"))


def test_stem_service_serializes_same_content_across_callers(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.wav"
    _wav(source)
    separator = FakeSeparator("htdemucs")
    separator.block = True
    service = _service(tmp_path, separator)
    key = content_cache_key(source)

    async def exercise():
        first = asyncio.create_task(
            service.ensure(source=source, content_key=key)
        )
        await separator.started.wait()
        status = await service.status(content_key=key)
        second = asyncio.create_task(
            service.ensure(source=source, content_key=key)
        )
        await asyncio.sleep(0.05)
        separator.release.set()
        return status, await first, await second

    status, first, second = asyncio.run(exercise())

    assert status.state == "processing"
    assert first.state == second.state == "ready"
    assert separator.calls == 1


def test_stem_service_rejects_unsynchronized_outputs(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    _wav(source)
    separator = FakeSeparator(
        "htdemucs",
        duration_by_stem={"other": 0.5},
    )
    service = _service(tmp_path, separator)
    key = content_cache_key(source)

    with pytest.raises(StemSeparationError, match="时长"):
        asyncio.run(service.ensure(source=source, content_key=key))

    assert asyncio.run(service.status(content_key=key)).state == "missing"
    assert not list((tmp_path / "stems").glob(".tmp-*"))


def test_stem_service_reports_an_unavailable_backend(tmp_path: Path) -> None:
    separator = FakeSeparator("htdemucs", available=False)
    service = _service(tmp_path, separator)

    status = asyncio.run(service.status(content_key="a" * 20))

    assert status.state == "unavailable"
    with pytest.raises(StemSeparationError, match="未安装"):
        asyncio.run(
            service.ensure(
                source=tmp_path / "missing.wav",
                content_key="a" * 20,
            )
        )


def test_missing_stem_status_does_not_create_cache_files(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, FakeSeparator("htdemucs"))

    status = asyncio.run(service.status(content_key="a" * 20))

    assert status.state == "missing"
    assert not (tmp_path / "stems").exists()


def test_stem_api_is_owner_scoped_and_streams_generated_tracks(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite3"
    accounts = AccountStore(database)
    history = HistoryStore(database)
    separator = FakeSeparator("htdemucs")
    service = _service(tmp_path, separator)
    application = create_app()
    application.state.stem_service = service
    application.dependency_overrides[get_account_store] = lambda: accounts
    application.dependency_overrides[get_history_store] = lambda: history
    client = TestClient(application)

    registered = client.post(
        "/auth/register",
        json={"username": "stem-owner", "password": "safe password"},
    )
    assert registered.status_code == 201
    owner_id = registered.json()["id"]
    source = tmp_path / "source.wav"
    _wav(source)
    now = datetime.now(UTC)
    history.create(
        job_id="stem-song",
        title="Stem song",
        file_name="source.wav",
        language="en",
        state="completed",
        created_at=now,
        updated_at=now,
        audio_path=source,
        user_id=owner_id,
    )

    missing = client.get("/history/stem-song/stems")
    generated = client.post("/history/stem-song/stems")
    audio = client.get("/history/stem-song/stems/vocals")

    assert missing.status_code == 200
    assert missing.json()["status"] == "missing"
    assert generated.status_code == 200
    assert generated.json()["status"] == "ready"
    assert len(generated.json()["stems"]) == 4
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/wav")

    other = client.post(
        "/auth/register",
        json={"username": "stem-other", "password": "safe password"},
    )
    assert other.status_code == 201
    assert client.get("/history/stem-song/stems").status_code == 404
    assert client.get("/history/stem-song/stems/vocals").status_code == 404


def test_stem_cache_follows_existing_history_gc_lifecycle(
    tmp_path: Path,
) -> None:
    database = tmp_path / "history.sqlite3"
    accounts = AccountStore(database)
    history = HistoryStore(database)
    owner = accounts.register("stem-gc", "safe password")
    source = tmp_path / "uploads" / "source.wav"
    _wav(source)
    now = datetime.now(UTC)
    history.create(
        job_id="stem-gc-song",
        title="Stem GC",
        file_name="source.wav",
        language=None,
        state="completed",
        created_at=now,
        updated_at=now,
        audio_path=source,
        user_id=owner.id,
    )
    separator = FakeSeparator("htdemucs")
    service = _service(tmp_path, separator)
    key = content_cache_key(source)
    result = asyncio.run(service.ensure(source=source, content_key=key))
    cache_files = list((tmp_path / "stems" / f"v1-{key}").rglob("*"))
    old = (now - timedelta(days=2)).timestamp()
    for path in cache_files:
        if path.is_file():
            os.utime(path, (old, old))

    protected = history.garbage_collect_assets(
        min_age=timedelta(days=1),
        now=now,
    )
    assert all(path.exists() for path in result.paths.values())
    assert not set(result.paths.values()) & set(protected.removed_files)

    assert history.delete("stem-gc-song", user_id=owner.id)
    collected = history.garbage_collect_assets(
        min_age=timedelta(days=1),
        now=now,
    )
    assert set(result.paths.values()).issubset(set(collected.removed_files))
    assert not (tmp_path / "stems" / f"v1-{key}").exists()
