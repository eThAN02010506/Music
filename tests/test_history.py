from datetime import UTC, datetime

from music_insight.api.history import HistoryStore
from music_insight.schemas import AnalysisResult, DspResult, Evidence, EvidenceType


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


def test_history_persists_renames_and_deletes(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3")
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
    )
    store.update(
        "job-1",
        state="completed",
        updated_at=now,
        result=_result(),
    )

    listed = store.list()
    assert len(listed) == 1
    assert listed[0].bpm == 76.0
    assert listed[0].duration_s == 180.0
    assert listed[0].instruments == ["钢琴"]
    assert listed[0].model_source == "network"
    assert listed[0].model_location == "http://192.168.1.97:8004"

    renamed = store.rename("job-1", "新的名称")
    assert renamed is not None
    assert renamed.title == "新的名称"
    assert renamed.result is not None
    assert renamed.audio_url == "/history/job-1/audio"

    reloaded = HistoryStore(tmp_path / "history.sqlite3").get("job-1")
    assert reloaded is not None
    assert reloaded.result is not None
    assert reloaded.result.summary == "本地缓存结果"

    assert store.delete("job-1") is True
    assert store.get("job-1") is None
    assert not audio.exists()


def test_history_marks_interrupted_jobs_failed(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path)
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
    )

    restored = HistoryStore(path).get("running-job")

    assert restored is not None
    assert restored.state == "failed"
    assert restored.error == "服务重启，未完成的任务已中断。"
