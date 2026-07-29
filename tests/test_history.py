from datetime import UTC, datetime, timedelta
import os
import sqlite3

import pytest

from music_insight.api.accounts import AccountStore
from music_insight.api.database import LATEST_SCHEMA_VERSION
from music_insight.api.history import HistoryEntryNotFoundError, HistoryStore
from music_insight.pipeline.preprocess import Preprocessor
from music_insight.schemas import (
    AnalysisResult,
    DspResult,
    Evidence,
    EvidenceType,
    LyricsSegment,
    TimeSpan,
)
from music_insight.storage.assets import content_cache_key


def _result() -> AnalysisResult:
    metrics = DspResult(
        bpm=76.0,
        bpm_confidence=0.4,
        evidence=[
            Evidence(
                id="dsp.metrics",
                source="test",
                kind=EvidenceType.COMPUTED,
                text="duration",
                metadata={"duration_s": 180.0},
            )
        ],
    )
    return AnalysisResult(
        summary="本地缓存结果",
        lyrics=[],
        instruments=["钢琴"],
        sound_events=[],
        emotion_timeline=[],
        inferred_atmosphere=[],
        themes=["测试"],
        technical_metrics=metrics,
        evidence=[],
    )


def _register_user(database_path, username: str) -> str:
    return AccountStore(database_path).register(
        username,
        "safe password",
    ).id


def test_history_persists_renames_and_deletes(tmp_path):
    database_path = tmp_path / "history.sqlite3"
    store = HistoryStore(database_path)
    user_id = _register_user(database_path, "user-a")
    audio = tmp_path / "uploads" / "song.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"RIFF-test")
    now = datetime.now(UTC)

    store.create(
        job_id="job-1",
        title="song",
        file_name="song.wav",
        language="zh",
        state="queued",
        created_at=now,
        updated_at=now,
        audio_path=audio,
        model_source="network",
        model_location="http://192.168.1.97:8004",
        user_id=user_id,
    )
    store.update(
        "job-1",
        state="completed",
        updated_at=now,
        result=_result(),
        user_id=user_id,
    )

    listed = store.list(user_id=user_id)
    assert len(listed) == 1
    assert listed[0].bpm == 76.0
    assert listed[0].duration_s == 180.0
    assert listed[0].instruments == ["钢琴"]
    assert listed[0].model_source == "network"
    assert listed[0].model_location == "http://192.168.1.97:8004"

    renamed = store.rename("job-1", "新的名称", user_id=user_id)
    assert renamed is not None
    assert renamed.title == "新的名称"
    assert renamed.result is not None
    assert renamed.audio_url == "/history/job-1/audio"

    reloaded = HistoryStore(tmp_path / "history.sqlite3").get(
        "job-1",
        user_id=user_id,
    )
    assert reloaded is not None
    assert reloaded.result is not None
    assert reloaded.result.summary == "本地缓存结果"

    revised = store.update_lyrics(
        "job-1",
        [
            LyricsSegment(
                text="人工校对歌词",
                span=TimeSpan(start_s=12.0, end_s=16.0),
            )
        ],
        user_id=user_id,
    )
    assert revised is not None
    assert revised.revision_count == 1
    assert revised.result is not None
    assert revised.result.lyrics[0].text == "人工校对歌词"
    assert revised.result.evidence[-1].source == "用户校对"
    revisions = store.revisions("job-1", user_id=user_id)
    assert len(revisions) == 1
    assert revisions[0].lyrics == []

    assert store.delete("job-1", user_id=user_id) is True
    assert store.get("job-1", user_id=user_id) is None
    assert store.revisions("job-1", user_id=user_id) == []
    assert not audio.exists()


def test_history_marks_interrupted_jobs_failed(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path)
    user_id = _register_user(path, "user-a")
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF-test")
    now = datetime.now(UTC)
    store.create(
        job_id="running-job",
        title="running",
        file_name="song.wav",
        language=None,
        state="running",
        created_at=now,
        updated_at=now,
        audio_path=audio,
        user_id=user_id,
    )

    restarted = HistoryStore(path)
    before_recovery = restarted.get("running-job", user_id=user_id)
    recovered_count = restarted.recover_interrupted_jobs()
    restored = restarted.get("running-job", user_id=user_id)

    assert before_recovery is not None
    assert before_recovery.state == "running"
    assert recovered_count == 1
    assert restored is not None
    assert restored.state == "failed"
    assert restored.error == "服务重启，未完成的任务已中断。"


def test_history_owner_scopes_all_reads_and_mutations(tmp_path):
    database_path = tmp_path / "history.sqlite3"
    store = HistoryStore(database_path)
    user_a = _register_user(database_path, "user-a")
    user_b = _register_user(database_path, "user-b")
    now = datetime.now(UTC)

    def create(job_id: str, user_id: str | None) -> None:
        audio = tmp_path / "uploads" / f"{job_id}.wav"
        audio.parent.mkdir(exist_ok=True)
        audio.write_bytes(b"RIFF-test")
        if user_id is None:
            store.create_legacy_unowned_for_migration(
                job_id=job_id,
                title=job_id,
                file_name=audio.name,
                language="zh",
                state="completed",
                created_at=now,
                updated_at=now,
                audio_path=audio,
            )
        else:
            store.create(
                job_id=job_id,
                title=job_id,
                file_name=audio.name,
                language="zh",
                state="completed",
                created_at=now,
                updated_at=now,
                audio_path=audio,
                user_id=user_id,
            )
            store.update(
                job_id,
                state="completed",
                updated_at=now,
                result=_result(),
                user_id=user_id,
            )

    create("owned-by-a", user_a)
    create("owned-by-b", user_b)
    create("legacy", None)

    assert [item.id for item in store.list(user_id=user_a)] == ["owned-by-a"]
    assert [item.id for item in store.list(user_id=user_b)] == ["owned-by-b"]
    assert {item.id for item in store.list_all_for_maintenance()} == {
        "owned-by-a",
        "owned-by-b",
        "legacy",
    }
    assert store.get("owned-by-b", user_id=user_a) is None
    assert store.audio_path("owned-by-b", user_id=user_a) is None

    with pytest.raises(HistoryEntryNotFoundError):
        store.update(
            "owned-by-b",
            state="failed",
            updated_at=now,
            error="must not leak",
            user_id=user_a,
        )
    still_owned_by_b = store.get("owned-by-b", user_id=user_b)
    assert still_owned_by_b is not None
    assert still_owned_by_b.state == "completed"
    assert still_owned_by_b.error is None

    assert store.rename("owned-by-b", "stolen", user_id=user_a) is None
    assert (
        store.update_lyrics(
            "owned-by-b",
            [
                LyricsSegment(
                    text="不应写入",
                    span=TimeSpan(start_s=0.0, end_s=1.0),
                )
            ],
            user_id=user_a,
        )
        is None
    )
    assert store.revisions("owned-by-b", user_id=user_a) == []

    revised = store.update_lyrics(
        "owned-by-b",
        [
            LyricsSegment(
                text="本人校对",
                span=TimeSpan(start_s=0.0, end_s=1.0),
            )
        ],
        user_id=user_b,
    )
    assert revised is not None
    assert len(store.revisions("owned-by-b", user_id=user_b)) == 1
    assert store.revisions("owned-by-b", user_id=user_a) == []

    assert store.delete("owned-by-b", user_id=user_a) is False
    assert store.get("owned-by-b", user_id=user_b) is not None
    assert (tmp_path / "uploads" / "owned-by-b.wav").exists()

    assert store.claim_legacy(user_a) == 1
    assert store.claim_legacy(user_b) == 0
    assert {item.id for item in store.list(user_id=user_a)} == {
        "owned-by-a",
        "legacy",
    }
    assert [item.id for item in store.list(user_id=user_b)] == ["owned-by-b"]

    assert store.delete("owned-by-b", user_id=user_b) is True
    assert store.revisions("owned-by-b", user_id=user_b) == []
    assert not (tmp_path / "uploads" / "owned-by-b.wav").exists()


def test_history_migrates_existing_database_with_nullable_owner(tmp_path):
    database_path = tmp_path / "history.sqlite3"
    audio = tmp_path / "uploads" / "legacy.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"legacy")
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE analyses (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                file_name TEXT NOT NULL,
                language TEXT,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                audio_path TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                model_source TEXT NOT NULL DEFAULT 'network',
                model_location TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO analyses (
                id, title, file_name, language, state, created_at,
                updated_at, audio_path, result_json, error,
                model_source, model_location
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-job",
                "legacy",
                audio.name,
                "zh",
                "completed",
                now,
                now,
                str(audio),
                _result().model_dump_json(),
                None,
                "network",
                "http://example.invalid",
            ),
        )

    store = HistoryStore(database_path)
    migrated = store.get_for_maintenance("legacy-job")

    with store._connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(analyses)").fetchall()
        }
        indexes = {
            row["name"]
            for row in connection.execute("PRAGMA index_list(analyses)").fetchall()
        }
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        foreign_keys_enabled = connection.execute(
            "PRAGMA foreign_keys"
        ).fetchone()[0]
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert "user_id" in columns
    assert {"summary_text", "duration_s", "lyrics_count", "bpm"} <= columns
    assert "idx_analyses_user_created" in indexes
    assert {
        "users",
        "sessions",
        "singing_attempts",
        "analysis_assets",
    } <= tables
    assert foreign_keys_enabled == 1
    assert schema_version == LATEST_SCHEMA_VERSION
    assert migrated is not None
    assert migrated.summary == "本地缓存结果"
    assert migrated.duration_s == 180.0
    assert migrated.instruments == ["钢琴"]
    assert migrated.bpm == 76.0
    backup_path = tmp_path / "history.pre-v0.sqlite3.bak"
    assert backup_path.is_file()
    with sqlite3.connect(backup_path) as backup:
        backup_columns = {
            row[1] for row in backup.execute("PRAGMA table_info(analyses)")
        }
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 0
    assert "user_id" not in backup_columns


def test_history_business_reads_fail_closed_without_an_owner(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3")

    with pytest.raises(TypeError):
        store.list()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        store.get("missing")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="user_id is required"):
        store.list(user_id="")


def test_history_list_uses_materialized_summary_without_parsing_result(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "history.sqlite3"
    store = HistoryStore(database_path)
    user_id = _register_user(database_path, "user-a")
    audio = tmp_path / "uploads" / "song.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"RIFF-summary")
    now = datetime.now(UTC)
    store.create(
        job_id="summary-job",
        title="summary",
        file_name=audio.name,
        language="zh",
        state="queued",
        created_at=now,
        updated_at=now,
        audio_path=audio,
        user_id=user_id,
    )
    store.update(
        "summary-job",
        state="completed",
        updated_at=now,
        result=_result(),
        user_id=user_id,
    )

    def unexpected_parse(payload):
        raise AssertionError("summary listing parsed a complete AnalysisResult")

    monkeypatch.setattr(
        HistoryStore,
        "_result",
        staticmethod(unexpected_parse),
    )

    summary = store.list(user_id=user_id)[0]

    assert summary.summary == "本地缓存结果"
    assert summary.duration_s == 180.0
    assert summary.lyrics_count == 0
    assert summary.instruments == ["钢琴"]
    assert summary.bpm == 76.0


def test_non_completed_update_clears_result_projection_and_derived_assets(
    tmp_path,
):
    database_path = tmp_path / "history.sqlite3"
    store = HistoryStore(database_path)
    user_id = _register_user(database_path, "user-a")
    now = datetime.now(UTC)
    source = tmp_path / "uploads" / "song.wav"
    source.parent.mkdir()
    source.write_bytes(b"source-audio")
    derived = tmp_path / "normalized" / "v2-derived" / "omni_input.wav"
    derived.parent.mkdir(parents=True)
    derived.write_bytes(b"derived-audio")
    completed_result = _result().model_copy(
        update={
            "evidence": [
                Evidence(
                    id="preprocess.omni.wav",
                    source="test",
                    kind=EvidenceType.COMPUTED,
                    text="cached",
                    metadata={"cached_path": str(derived)},
                )
            ]
        }
    )
    store.create(
        job_id="cleared-result",
        title="song",
        file_name=source.name,
        language="zh",
        state="queued",
        created_at=now,
        updated_at=now,
        audio_path=source,
        user_id=user_id,
    )
    store.update(
        "cleared-result",
        state="completed",
        updated_at=now,
        result=completed_result,
        user_id=user_id,
    )

    store.update(
        "cleared-result",
        state="failed",
        updated_at=now + timedelta(seconds=1),
        result=None,
        error="retry failed",
        user_id=user_id,
    )

    detail = store.get("cleared-result", user_id=user_id)
    assert detail is not None
    assert detail.state == "failed"
    assert detail.error == "retry failed"
    assert detail.result is None
    assert detail.summary is None
    assert detail.duration_s is None
    assert detail.lyrics_count == 0
    assert detail.instruments == []
    assert detail.bpm is None
    with store._connect() as connection:
        row = connection.execute(
            """
            SELECT result_json, summary_text, duration_s, lyrics_count,
                   instruments_json, bpm
            FROM analyses
            WHERE id = ?
            """,
            ("cleared-result",),
        ).fetchone()
        assets = connection.execute(
            """
            SELECT path, kind
            FROM analysis_assets
            WHERE analysis_id = ?
            ORDER BY kind, path
            """,
            ("cleared-result",),
        ).fetchall()

    assert row is not None
    assert row["result_json"] is None
    assert row["summary_text"] is None
    assert row["duration_s"] is None
    assert row["lyrics_count"] == 0
    assert row["instruments_json"] == "[]"
    assert row["bpm"] is None
    assert [(asset["path"], asset["kind"]) for asset in assets] == [
        (str(source), "source")
    ]


def test_history_delete_removes_sources_and_defers_derived_cache_to_gc(
    tmp_path,
):
    database_path = tmp_path / "history.sqlite3"
    store = HistoryStore(database_path)
    user_a = _register_user(database_path, "user-a")
    user_b = _register_user(database_path, "user-b")
    now = datetime.now(UTC)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    first_audio = uploads / "first.wav"
    second_audio = uploads / "second.wav"
    first_audio.write_bytes(b"same-audio-content")
    second_audio.write_bytes(b"same-audio-content")
    cache_key = content_cache_key(first_audio)
    normalized = tmp_path / "normalized" / cache_key / "omni_input.wav"
    normalized.parent.mkdir(parents=True)
    normalized.write_bytes(b"normalized")
    cached_result = _result().model_copy(
        update={
            "evidence": [
                Evidence(
                    id="preprocess.omni.wav",
                    source="test",
                    kind=EvidenceType.COMPUTED,
                    text="cached",
                    metadata={"cached_path": str(normalized)},
                )
            ]
        }
    )

    for job_id, audio, owner in (
        ("first", first_audio, user_a),
        ("second", second_audio, user_b),
    ):
        store.create(
            job_id=job_id,
            title=job_id,
            file_name=audio.name,
            language=None,
            state="queued",
            created_at=now,
            updated_at=now,
            audio_path=audio,
            user_id=owner,
        )
        store.update(
            job_id,
            state="completed",
            updated_at=now,
            result=cached_result,
            user_id=owner,
        )

    assert store.delete("first", user_id=user_a) is True
    assert not first_audio.exists()
    assert second_audio.exists()
    assert normalized.exists()

    assert store.delete("second", user_id=user_b) is True
    assert not second_audio.exists()
    assert normalized.exists()

    old_timestamp = (now - timedelta(days=2)).timestamp()
    os.utime(normalized, (old_timestamp, old_timestamp))
    report = store.garbage_collect_assets(
        min_age=timedelta(days=1),
        now=now,
    )

    assert report.removed_files == (normalized.resolve(),)
    assert not normalized.exists()
    assert not normalized.parent.exists()


def test_asset_gc_respects_references_cache_keys_and_grace_period(tmp_path):
    database_path = tmp_path / "history.sqlite3"
    store = HistoryStore(database_path)
    user_id = _register_user(database_path, "user-a")
    now = datetime.now(UTC)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    referenced = uploads / "referenced.wav"
    referenced.write_bytes(b"referenced-content")
    orphan = uploads / "orphan.wav"
    orphan.write_bytes(b"orphan")
    cache_key = content_cache_key(referenced)
    protected_cache = tmp_path / "normalized" / cache_key / "omni_input.wav"
    protected_cache.parent.mkdir(parents=True)
    protected_cache.write_bytes(b"keep")
    orphan_cache = tmp_path / "normalized" / ("f" * 20) / "omni_input.wav"
    orphan_cache.parent.mkdir(parents=True)
    orphan_cache.write_bytes(b"remove")
    fresh_orphan = uploads / "fresh.wav"
    fresh_orphan.write_bytes(b"fresh")
    old_timestamp = (now - timedelta(days=2)).timestamp()
    for path in (orphan, protected_cache, orphan_cache):
        os.utime(path, (old_timestamp, old_timestamp))

    store.create(
        job_id="referenced",
        title="referenced",
        file_name=referenced.name,
        language=None,
        state="completed",
        created_at=now,
        updated_at=now,
        audio_path=referenced,
        user_id=user_id,
    )

    report = store.garbage_collect_assets(
        min_age=timedelta(days=1),
        now=now,
    )

    assert referenced.exists()
    assert protected_cache.exists()
    assert fresh_orphan.exists()
    assert not orphan.exists()
    assert not orphan_cache.exists()
    assert report.removed_count == 2
    assert report.reclaimed_bytes == len(b"orphan") + len(b"remove")


def test_v5_source_content_key_protects_current_preprocessor_cache(tmp_path):
    database_path = tmp_path / "history.sqlite3"
    store = HistoryStore(database_path)
    user_id = _register_user(database_path, "user-a")
    now = datetime.now(UTC)
    source = tmp_path / "uploads" / "source.wav"
    source.parent.mkdir()
    source.write_bytes(b"current-preprocessor-source")
    content_key = content_cache_key(source)
    normalized = (
        tmp_path
        / "normalized"
        / f"{Preprocessor.CACHE_FORMAT_VERSION}-{content_key}"
        / "omni_input.wav"
    )
    normalized.parent.mkdir(parents=True)
    normalized.write_bytes(b"normalized")
    old_timestamp = (now - timedelta(days=2)).timestamp()
    os.utime(normalized, (old_timestamp, old_timestamp))

    store.create(
        job_id="current-cache",
        title="source",
        file_name=source.name,
        language=None,
        state="completed",
        created_at=now,
        updated_at=now,
        audio_path=source,
        user_id=user_id,
    )

    with store._connect() as connection:
        schema_version = connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0]
        source_row = connection.execute(
            """
            SELECT kind, content_key
            FROM analysis_assets
            WHERE analysis_id = ? AND path = ?
            """,
            ("current-cache", str(source)),
        ).fetchone()

    report = store.garbage_collect_assets(
        min_age=timedelta(days=1),
        now=now,
    )

    assert schema_version == LATEST_SCHEMA_VERSION
    assert source_row is not None
    assert source_row["kind"] == "source"
    assert source_row["content_key"] == content_key
    assert normalized.exists()
    assert normalized.resolve() not in report.removed_files


def test_asset_gc_cleans_only_old_user_temporary_uploads(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3")
    now = datetime.now(UTC)
    temporary = tmp_path / "users" / "user-a" / "temporary"
    temporary.mkdir(parents=True)
    old_file = temporary / "old.wav"
    old_file.write_bytes(b"old")
    fresh_file = temporary / "fresh.wav"
    fresh_file.write_bytes(b"fresh")
    old_timestamp = (now - timedelta(days=2)).timestamp()
    os.utime(old_file, (old_timestamp, old_timestamp))

    report = store.garbage_collect_assets(
        min_age=timedelta(days=1),
        now=now,
    )

    assert report.removed_files == (old_file.resolve(),)
    assert not old_file.exists()
    assert fresh_file.exists()
