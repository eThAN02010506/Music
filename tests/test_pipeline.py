import asyncio
import io
from unittest.mock import patch
import wave

import numpy as np
import pytest
from starlette.datastructures import Headers, UploadFile

from music_insight.adapters.dsp import BasicDspAdapter
from music_insight.adapters.openai_compat_utils import parse_json_object
from music_insight.adapters.qwen_omni_unified import QwenOmniUnifiedAdapter
from music_insight.api.app import app
from music_insight.audio import slice_wav
from music_insight.pipeline.orchestrator import AnalysisOrchestrator
from music_insight.pipeline.preprocess import Preprocessor
from music_insight.schemas import (
    AsrResult,
    AudioAsset,
    AudioSceneResult,
    DspResult,
    Evidence,
    EvidenceType,
    LiteraryResult,
    LyricsSegment,
    TimeSpan,
    UnifiedAudioResult,
)
from music_insight.storage.local import LocalAudioStore, UploadTooLargeError


class FakeUnifiedAdapter:
    async def analyze(self, asset, dsp, progress=None):
        return UnifiedAudioResult(
            asr=AsrResult(
                model="test Qwen Omni",
                lyrics=[LyricsSegment(text="夜空中闪烁的星")],
                evidence=[
                    Evidence(
                        id="omni.transcript",
                        source="test Qwen Omni",
                        kind=EvidenceType.INFERRED,
                        text="夜空中闪烁的星",
                    )
                ],
            ),
            scene=AudioSceneResult(
                model="test Qwen Omni",
                instruments=["钢琴"],
                themes=["夜空"],
                narrative="钢琴与平稳节奏构成安静声景。",
                evidence=[],
            ),
            literary=LiteraryResult(
                model="test Qwen Omni",
                themes=["夜空", "希望"],
                narrative="歌词、钢琴和节拍共同形成逐渐明亮的情绪走向。",
                evidence=[],
            ),
        )


class FailingUnifiedAdapter:
    async def analyze(self, asset, dsp, progress=None):
        raise RuntimeError("unified test failure")


class FakeRecoveringOmni(QwenOmniUnifiedAdapter):
    async def _model(self):
        return "test-model"

    async def _analyze_chunk(self, **kwargs):
        return {
            "lyrics": [],
            "instruments": ["钢琴"],
            "sound_events": [],
            "emotion_timeline": [],
            "themes": [],
            "narrative": "钢琴持续演奏。",
        }

    async def _recover_missing(self, **kwargs):
        return {
            "lyrics": [{"text": "hello", "start_s": 0, "end_s": 1}],
            "emotion_timeline": [
                {"text": "轻柔", "start_s": 0, "end_s": 1, "confidence": 0.7}
            ],
        }

    async def _synthesize_report(self, **kwargs):
        return "最终报告。", ["主题"], [], []


class FakeEmotionMissingOmni(QwenOmniUnifiedAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.recovery_calls = 0

    async def _model(self):
        return "test-model"

    async def _analyze_chunk(self, **kwargs):
        return {
            "lyrics": [{"text": "hello", "start_s": 0, "end_s": 1}],
            "instruments": [],
            "sound_events": [],
            "emotion_timeline": [],
            "themes": [],
            "narrative": "voice",
        }

    async def _recover_missing(self, **kwargs):
        self.recovery_calls += 1
        return {"lyrics": [], "emotion_timeline": []}

    async def _synthesize_report(self, **kwargs):
        return "final", [], [], []


class FakeQualityRetryOmni(QwenOmniUnifiedAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.quality_recovery_calls = 0

    async def _model(self):
        return "test-model"

    async def _analyze_chunk(self, **kwargs):
        return {
            "lyrics": [
                {
                    "text": "等到放晴的那天也许我会比较好一点",
                    "start_s": 0,
                    "end_s": 0.2,
                }
            ],
            "instruments": [],
            "sound_events": [],
            "emotion_timeline": [],
            "themes": [],
            "narrative": "voice",
        }

    async def _recover_lyrics_quality(self, **kwargs):
        self.quality_recovery_calls += 1
        return {
            "lyrics": [
                {
                    "text": "等到放晴那天",
                    "start_s": 0.2,
                    "end_s": 1.8,
                }
            ]
        }

    async def _synthesize_report(self, **kwargs):
        return "final", [], [], []


class FakeJsonRetryOmni(QwenOmniUnifiedAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.requests = []

    async def _chat(self, request, timeout):
        self.requests.append(request)
        if len(self.requests) == 1:
            return '{"lyrics": [{"text": "broken"} "themes": []}'
        return '{"lyrics": [], "themes": []}'


class FakeDspAdapter:
    async def analyze(self, asset):
        return DspResult(
            bpm=120.0,
            key="A minor",
            evidence=[
                Evidence(
                    id="dsp.metrics",
                    source="librosa DSP",
                    kind=EvidenceType.COMPUTED,
                    text="BPM 120；A minor",
                )
            ],
        )


def _asset(path, language="zh"):
    return AudioAsset(
        path=path,
        media_type="audio/wav",
        size_bytes=path.stat().st_size,
        language_hint=language,
    )


def _write_test_audio(path, seconds=4.0, sample_rate=22_050):
    time = np.arange(int(seconds * sample_rate)) / sample_rate
    signal = 0.2 * np.sin(2 * np.pi * 440 * time)
    for beat in np.arange(0, seconds, 0.5):
        start = int(beat * sample_rate)
        signal[start : start + 200] += np.hanning(200) * 0.8
    pcm = (np.clip(signal, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())


def test_orchestrator_uses_only_unified_model(tmp_path):
    audio = tmp_path / "song.wav"
    _write_test_audio(audio)
    orchestrator = AnalysisOrchestrator(
        unified=FakeUnifiedAdapter(),
        dsp=FakeDspAdapter(),
    )

    result = asyncio.run(orchestrator.analyze(_asset(audio)))

    assert result.lyrics[0].text == "夜空中闪烁的星"
    assert result.instruments == ["钢琴"]
    assert result.summary.startswith("歌词、钢琴和节拍")
    assert result.themes == ["夜空", "希望"]
    assert result.warnings == []


def test_orchestrator_degrades_when_unified_model_fails(tmp_path):
    audio = tmp_path / "song.wav"
    _write_test_audio(audio)
    orchestrator = AnalysisOrchestrator(
        unified=FailingUnifiedAdapter(),
        dsp=FakeDspAdapter(),
    )

    result = asyncio.run(orchestrator.analyze(_asset(audio)))

    assert result.lyrics == []
    assert result.technical_metrics.bpm == 120.0
    assert any(item.id == "omni.error" for item in result.evidence)
    assert any("unified test failure" in warning for warning in result.warnings)


def test_chinese_language_gate_rejects_english_transcript():
    result = AsrResult(
        model="test Qwen Omni",
        lyrics=[LyricsSegment(text="Let him sing")],
        evidence=[],
    )

    checked = AnalysisOrchestrator._apply_language_gate(result, "zh", "omni")

    assert checked.lyrics == []
    assert checked.evidence[0].id == "omni.transcript.rejected"


def test_real_dsp_returns_metrics_and_energy(tmp_path):
    audio = tmp_path / "clicks.wav"
    _write_test_audio(audio, seconds=6.0)

    result = asyncio.run(BasicDspAdapter().analyze(_asset(audio, language=None)))

    assert result.bpm is not None
    assert result.bpm_confidence is not None
    assert 0 <= result.bpm_confidence <= 1
    assert 100 <= result.bpm <= 140
    assert result.key
    assert result.key_confidence is not None
    assert 0 <= result.key_confidence <= 1
    assert result.energy_curve
    assert result.evidence[0].confidence != 1.0


def test_dsp_prefers_supported_half_time_candidate():
    onset = np.ones(64, dtype=np.float32)

    with patch.object(
        BasicDspAdapter,
        "_pulse_support",
        side_effect=[0.7428, 0.7299],
    ):
        bpm, candidates, ambiguous, ratio = BasicDspAdapter._resolve_tempo_octave(
            151.999,
            onset,
            22_050,
        )

    assert bpm == pytest.approx(75.9995)
    assert candidates == [76.0, 152.0]
    assert ambiguous is True
    assert ratio == pytest.approx(0.983)


def test_dsp_keeps_fast_tempo_without_half_time_support():
    onset = np.ones(64, dtype=np.float32)

    with patch.object(
        BasicDspAdapter,
        "_pulse_support",
        side_effect=[0.8, 0.5],
    ):
        bpm, candidates, ambiguous, ratio = BasicDspAdapter._resolve_tempo_octave(
            160.0,
            onset,
            22_050,
        )

    assert bpm == 160.0
    assert candidates == [160.0]
    assert ambiguous is False
    assert ratio == pytest.approx(0.625)


def test_preprocessor_creates_omni_wav(tmp_path):
    audio = tmp_path / "source.wav"
    _write_test_audio(audio)

    prepared = asyncio.run(
        Preprocessor(workspace_dir=tmp_path / "cache").prepare(_asset(audio))
    )

    assert prepared.scene.path.suffix == ".wav"
    assert prepared.scene.path.exists()
    assert any(item.id == "preprocess.omni.wav" for item in prepared.evidence)


def test_chat_parser_handles_fenced_json():
    parsed = parse_json_object(
        '```json\n{"themes":["希望"],"narrative":"情绪上升。"}\n```'
    )

    assert parsed["themes"] == ["希望"]


def test_chat_parser_repairs_single_quotes_and_unquoted_keys():
    assert parse_json_object("{'lyrics': [], 'themes': ['hope']}")["themes"] == [
        "hope"
    ]
    assert parse_json_object('{lyrics: [], "themes": ["hope"]}')["lyrics"] == []


def test_time_span_rejects_reverse_range():
    with pytest.raises(ValueError):
        TimeSpan(start_s=2.0, end_s=1.0)


def test_unified_adapter_chunks_wav(tmp_path):
    audio = tmp_path / "long.wav"
    _write_test_audio(audio, seconds=12.0, sample_rate=16_000)
    adapter = QwenOmniUnifiedAdapter(
        endpoint="http://127.0.0.1:9999",
        model="test-model",
        chunk_seconds=5.0,
    )

    chunks = list(adapter._wav_chunks(audio))

    assert len(chunks) == 3
    assert chunks[0][1:] == (0.0, 5.0)
    assert chunks[-1][1:] == (10.0, 12.0)


def test_slice_wav_returns_requested_excerpt(tmp_path):
    audio = tmp_path / "source.wav"
    _write_test_audio(audio, seconds=4.0, sample_rate=16_000)

    excerpt, duration = slice_wav(audio, 1.25, 3.0)
    with wave.open(io.BytesIO(excerpt), "rb") as source:
        frames = source.getnframes()
        sample_rate = source.getframerate()

    assert duration == pytest.approx(1.75)
    assert frames / sample_rate == pytest.approx(1.75)


def test_chunk_parser_offsets_and_bounds_timestamps():
    adapter = QwenOmniUnifiedAdapter(
        endpoint="http://127.0.0.1:9999", model="test-model"
    )
    parsed = adapter._parse_chunk(
        {
            "lyrics": [{"text": "hello", "start_s": 1, "end_s": 2}],
            "sound_events": [
                {"description": "鼓声", "start_time": 20, "end_time": 35},
                {"description": "越界幻觉", "start_time": 40, "end_time": 42},
            ],
            "emotion_timeline": [],
            "instruments": ["鼓"],
            "themes": ["推进"],
            "narrative": "节奏推进。",
        },
        index=2,
        chunk_start=30.0,
        chunk_end=60.0,
    )

    assert parsed["lyrics"][0].span.start_s == 31.0
    assert len(parsed["sound_events"]) == 1
    assert parsed["sound_events"][0].span.end_s == 60.0


def test_chunk_parser_splits_multiline_lyrics_and_distributes_span():
    adapter = QwenOmniUnifiedAdapter(
        endpoint="http://127.0.0.1:9999", model="test-model"
    )
    parsed = adapter._parse_chunk(
        {
            "lyrics": [{"text": "first line\nsecond line"}],
            "sound_events": [],
            "emotion_timeline": [],
            "instruments": [],
            "themes": [],
            "narrative": "",
        },
        index=1,
        chunk_start=0.0,
        chunk_end=10.0,
    )

    assert [item.text for item in parsed["lyrics"]] == ["first line", "second line"]
    assert parsed["lyrics"][0].span == TimeSpan(start_s=0.0, end_s=5.0)
    assert parsed["lyrics"][1].span == TimeSpan(start_s=5.0, end_s=10.0)


def test_chunk_parser_rejects_prompt_placeholders():
    adapter = QwenOmniUnifiedAdapter(
        endpoint="http://127.0.0.1:9999", model="test-model"
    )
    parsed = adapter._parse_chunk(
        {
            "lyrics": [{"text": "原文"}],
            "instruments": ["声源"],
            "sound_events": [{"text": "事件及依据"}],
            "emotion_timeline": [{"text": "情绪及依据"}],
            "themes": ["声音支持的主题"],
            "narrative": "两至四句局部声音描述",
        },
        index=1,
        chunk_start=0.0,
        chunk_end=20.0,
    )

    assert parsed["lyrics"] == []
    assert parsed["instruments"] == []
    assert parsed["sound_events"] == []
    assert parsed["emotions"] == []
    assert parsed["themes"] == []
    assert parsed["narrative"] == ""


def test_adapter_normalizes_and_deduplicates_labels():
    adapter = QwenOmniUnifiedAdapter(
        endpoint="http://127.0.0.1:9999", model="test-model"
    )
    values = adapter._strings(
        ["electricguitar", " electricguitar ", "piano"], limit=10
    )

    assert adapter._deduplicate(values, 10) == ["electric guitar", "piano"]


def test_adapter_uses_strict_structured_output_schemas():
    adapter = QwenOmniUnifiedAdapter(
        endpoint="http://127.0.0.1:9999", model="test-model"
    )

    assert adapter._chunk_response_format()["type"] == "json_schema"
    assert adapter._recovery_response_format()["json_schema"]["strict"] is True
    assert adapter._final_response_format()["json_schema"]["strict"] is True
    final_schema = adapter._final_response_format()["json_schema"]["schema"]
    assert "inferred_atmosphere" in final_schema["required"]
    lyric_recovery = adapter._recovery_response_format(["lyrics"])
    assert set(lyric_recovery["json_schema"]["schema"]["properties"]) == {
        "lyrics"
    }


def test_unified_adapter_recovers_missing_lyrics(tmp_path):
    audio = tmp_path / "recover.wav"
    _write_test_audio(audio, seconds=2.0, sample_rate=16_000)
    adapter = FakeRecoveringOmni(
        endpoint="http://127.0.0.1:9999", model="test-model"
    )

    result = asyncio.run(adapter.analyze(_asset(audio, language="en"), DspResult()))

    assert result.asr.lyrics[0].text == "hello"
    assert result.scene.emotion_timeline == []
    assert any(item.id.endswith(".recovery") for item in result.scene.evidence)


def test_unified_adapter_reports_chunk_and_synthesis_progress(tmp_path):
    audio = tmp_path / "progress.wav"
    _write_test_audio(audio, seconds=6.0, sample_rate=16_000)
    adapter = FakeRecoveringOmni(
        endpoint="http://127.0.0.1:9999",
        model="test-model",
        chunk_seconds=5,
    )
    updates = []

    async def progress(stage, value, message):
        updates.append((stage, value, message))

    asyncio.run(
        adapter.analyze(
            _asset(audio, language="en"),
            DspResult(),
            progress=progress,
        )
    )

    assert any("1/2" in message for _, _, message in updates)
    assert any("2/2" in message for _, _, message in updates)
    assert [stage for stage, _, _ in updates][-1] == "model_synthesis"
    assert updates[-1][1] == 1.0


def test_orchestrators_share_model_concurrency_gate(tmp_path):
    class CountingUnified(FakeUnifiedAdapter):
        def __init__(self):
            self.active = 0
            self.maximum_active = 0

        async def analyze(self, asset, dsp, progress=None):
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return await super().analyze(asset, dsp, progress)

    async def exercise():
        first_audio = tmp_path / "first.wav"
        second_audio = tmp_path / "second.wav"
        _write_test_audio(first_audio, seconds=1.0)
        _write_test_audio(second_audio, seconds=1.5)
        unified = CountingUnified()
        gate = asyncio.Semaphore(1)
        first = AnalysisOrchestrator(
            unified=unified,
            dsp=FakeDspAdapter(),
            preprocessor=Preprocessor(tmp_path / "first-work"),
            model_gate=gate,
        )
        second = AnalysisOrchestrator(
            unified=unified,
            dsp=FakeDspAdapter(),
            preprocessor=Preprocessor(tmp_path / "second-work"),
            model_gate=gate,
        )
        await asyncio.gather(
            first.analyze(_asset(first_audio)),
            second.analyze(_asset(second_audio)),
        )
        return unified.maximum_active

    assert asyncio.run(exercise()) == 1


def test_unified_adapter_does_not_relisten_for_emotion_only(tmp_path):
    audio = tmp_path / "emotion-optional.wav"
    _write_test_audio(audio, seconds=2.0, sample_rate=16_000)
    adapter = FakeEmotionMissingOmni(
        endpoint="http://127.0.0.1:9999", model="test-model"
    )

    result = asyncio.run(adapter.analyze(_asset(audio, language="en"), DspResult()))

    assert result.asr.lyrics[0].text == "hello"
    assert result.scene.emotion_timeline == []
    assert adapter.recovery_calls == 0


def test_lyrics_quality_filter_rejects_density_overlap_and_near_duplicates():
    adapter = QwenOmniUnifiedAdapter(
        endpoint="http://127.0.0.1:9999", model="test-model"
    )
    reliable = LyricsSegment(
        text="等到放晴那天",
        span=TimeSpan(start_s=0.0, end_s=2.0),
    )
    values = [
        reliable,
        LyricsSegment(
            text="另一句歌词",
            span=TimeSpan(start_s=1.0, end_s=3.0),
        ),
        LyricsSegment(
            text="这一整句歌词不可能在瞬间唱完",
            span=TimeSpan(start_s=2.0, end_s=2.2),
        ),
        LyricsSegment(
            text="等到放晴那天",
            span=TimeSpan(start_s=2.1, end_s=4.0),
        ),
    ]

    kept, issues = adapter._filter_lyrics_quality(values)

    assert kept == [reliable]
    assert any("重叠" in issue for issue in issues)
    assert any("密度" in issue for issue in issues)
    assert any("重复" in issue for issue in issues)


def test_unified_adapter_relistens_only_bad_lyrics_chunk(tmp_path):
    audio = tmp_path / "quality-retry.wav"
    _write_test_audio(audio, seconds=2.0, sample_rate=16_000)
    adapter = FakeQualityRetryOmni(
        endpoint="http://127.0.0.1:9999", model="test-model"
    )

    result = asyncio.run(adapter.analyze(_asset(audio), DspResult()))

    assert adapter.quality_recovery_calls == 1
    assert [item.text for item in result.asr.lyrics] == ["等到放晴那天"]
    retry = next(
        item for item in result.scene.evidence if item.id.endswith(".quality_retry")
    )
    assert retry.metadata["issues"]
    assert retry.metadata["recovered_issues"] == []


def test_manual_retry_returns_quality_checked_relative_lyrics():
    adapter = FakeQualityRetryOmni(
        endpoint="http://127.0.0.1:9999", model="test-model"
    )

    lyrics, issues = asyncio.run(
        adapter.retry_lyrics(b"test-wav", 2.0, "zh")
    )

    assert [item.text for item in lyrics] == ["等到放晴那天"]
    assert lyrics[0].span == TimeSpan(start_s=0.2, end_s=1.8)
    assert issues == []


def test_chat_json_retries_malformed_success_response():
    adapter = FakeJsonRetryOmni(
        endpoint="http://127.0.0.1:9999", model="test-model"
    )
    request = {
        "messages": [{"role": "system", "content": "Return JSON."}],
        "response_format": adapter._chunk_response_format(),
        "max_tokens": 1800,
    }

    parsed = asyncio.run(adapter._chat_json(request, timeout=1.0))

    assert parsed == {"lyrics": [], "themes": []}
    assert len(adapter.requests) == 2
    assert adapter.requests[1]["response_format"] == {"type": "json_object"}
    assert adapter.requests[1]["max_tokens"] == 1200


def test_adapter_labels_minicpm_model_family():
    adapter = QwenOmniUnifiedAdapter(
        endpoint="http://127.0.0.1:9999",
        model="MiniCPM-o-4_5-Q4_K_M.gguf",
    )

    assert asyncio.run(adapter._model()) == "MiniCPM-o-4_5-Q4_K_M.gguf"
    assert adapter.source == "MiniCPM-o · http://127.0.0.1:9999"


def test_chunk_parser_filters_artificial_boundary_clicks():
    adapter = QwenOmniUnifiedAdapter(
        endpoint="http://127.0.0.1:9999",
        model="test-model",
        chunk_seconds=30.0,
    )
    parsed = adapter._parse_chunk(
        {
            "lyrics": [],
            "instruments": [],
            "sound_events": [
                {"text": "clicking", "start_s": 29.99, "end_s": 30.0},
                {"text": "applause", "start_s": 29.9, "end_s": 30.0},
            ],
            "emotion_timeline": [],
            "themes": [],
            "narrative": "",
        },
        index=1,
        chunk_start=0.0,
        chunk_end=30.0,
    )

    assert [item.text for item in parsed["sound_events"]] == ["applause"]


def test_inferred_atmosphere_is_marked_interpretive_with_basis():
    adapter = QwenOmniUnifiedAdapter(
        endpoint="http://127.0.0.1:9999", model="test-model"
    )

    items = adapter._atmosphere_items(
        [
            {
                "text": "宁静、童真",
                "confidence": 0.72,
                "basis": "童谣歌词与平稳能量",
            },
            {"text": "欢快", "confidence": 0.7, "basis": "重复节奏"},
            {"text": "愉快", "confidence": 0.6, "basis": "重复节奏"},
        ],
        model="test-model",
    )

    assert items[0].kind == EvidenceType.INTERPRETIVE
    assert items[0].span is None
    assert items[0].metadata["basis"] == "童谣歌词与平稳能量"
    assert [item.text for item in items] == ["宁静、童真", "欢快"]


def test_api_exposes_no_tts_route():
    paths = {route.path for route in app.routes}

    assert "/analyze" in paths
    assert "/synthesize" not in paths


def test_upload_store_stops_before_oversized_file_is_persisted(tmp_path):
    upload = UploadFile(
        file=io.BytesIO(b"x" * 32),
        filename="large.wav",
        headers=Headers({"content-type": "audio/wav"}),
    )
    store = LocalAudioStore(tmp_path / "uploads")

    with pytest.raises(UploadTooLargeError):
        asyncio.run(store.save_upload(upload, max_bytes=16))

    assert list((tmp_path / "uploads").iterdir()) == []
