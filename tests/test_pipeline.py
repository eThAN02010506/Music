import asyncio
import io
import threading
from unittest.mock import patch
import wave

import av
import numpy as np
import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from music_insight.adapters.dsp import BasicDspAdapter
from music_insight.adapters.openai_compat_utils import parse_json_object
from music_insight.adapters.qwen_omni_unified import QwenOmniUnifiedAdapter
from music_insight.api.app import app
from music_insight.api.services.uploads import save_audio_upload
from music_insight.audio import (
    AudioDurationExceededError,
    decode_mono,
    slice_wav,
)
from music_insight.config import Settings
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
    VocalPresenceStatus,
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
            "vocals_detected": True,
            "vocal_confidence": 0.9,
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


class FakeHallucinatedRecoveryOmni(FakeRecoveringOmni):
    async def _recover_missing(self, **kwargs):
        return {
            "lyrics": [
                {
                    "text": "this recovered lyric is impossibly dense",
                    "start_s": 0,
                    "end_s": 0.05,
                }
            ],
            "emotion_timeline": [],
        }


class FakeInstrumentalOmni(FakeRecoveringOmni):
    async def _analyze_chunk(self, **kwargs):
        return {
            "lyrics": [],
            "instruments": ["钢琴"],
            "vocals_detected": False,
            "vocal_confidence": 0.92,
            "sound_events": [],
            "emotion_timeline": [],
            "themes": ["器乐"],
            "narrative": "钢琴持续演奏。",
        }

    async def _recover_missing(self, **kwargs):
        return {"lyrics": []}


class FakePartiallyClassifiedOmni(FakeInstrumentalOmni):
    async def _analyze_chunk(self, **kwargs):
        payload = await super()._analyze_chunk(**kwargs)
        if kwargs["start_s"] > 0:
            payload["vocals_detected"] = None
            payload["vocal_confidence"] = None
        return payload


class FakeRecoveredInstrumentalOmni(FakeRecoveringOmni):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.requested_missing_fields = []

    async def _analyze_chunk(self, **kwargs):
        return {
            "lyrics": [],
            "instruments": ["弦乐组", "铜管组"],
            "vocals_detected": (
                None if kwargs["start_s"] == 0 else False
            ),
            "vocal_confidence": None,
            "sound_events": [],
            "emotion_timeline": [],
            "themes": [],
            "narrative": "",
        }

    async def _recover_missing(self, **kwargs):
        self.requested_missing_fields.append(kwargs["missing"])
        return {
            "lyrics": [],
            "vocals_detected": False,
            "vocal_confidence": 0.9,
        }


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


class FakeQualityRetryInstrumentalOmni(FakeQualityRetryOmni):
    async def _recover_lyrics_quality(self, **kwargs):
        self.quality_recovery_calls += 1
        return {
            "lyrics": [],
            "vocals_detected": False,
            "vocal_confidence": 0.94,
        }


class FakeJsonRetryOmni(QwenOmniUnifiedAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.requests = []

    async def _chat(self, request, timeout):
        self.requests.append(request)
        if len(self.requests) == 1:
            return '{"lyrics": [{"text": "broken"} "themes": []}'
        return (
            '{"lyrics": [], "instruments": [], "sound_events": [], '
            '"vocals_detected": null, "vocal_confidence": null, '
            '"emotion_timeline": [], "themes": [], "narrative": ""}'
        )


class FakeBatchUnavailableOmni(QwenOmniUnifiedAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.analysis_calls = 0
        self.synthesis_calls = 0

    async def _model(self):
        return "test-model"

    async def _analyze_chunk(self, **kwargs):
        self.analysis_calls += 1
        raise RuntimeError("Worker busy")

    def should_abort_chunking(self, error):
        return "Worker busy" in str(error)

    async def _synthesize_report(self, **kwargs):
        self.synthesis_calls += 1
        raise AssertionError("aborted batches must not call remote synthesis")


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


def _write_mp3_with_apev2_tag(path, seconds=1.0, sample_rate=44_100):
    samples = np.zeros((1, int(seconds * sample_rate)), dtype=np.float32)
    with av.open(str(path), "w", format="mp3") as output:
        stream = output.add_stream("mp3", rate=sample_rate)
        stream.layout = "mono"
        frame = av.AudioFrame.from_ndarray(
            samples,
            format="fltp",
            layout="mono",
        )
        frame.sample_rate = sample_rate
        for packet in stream.encode(frame):
            output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)

    value = b"test"
    field = (
        len(value).to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + b"Title\0"
        + value
    )
    tag_size = len(field) + 32
    footer = (
        b"APETAGEX"
        + (2_000).to_bytes(4, "little")
        + tag_size.to_bytes(4, "little")
        + (1).to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + b"\0" * 8
    )
    with path.open("ab") as output:
        output.write(field)
        output.write(footer)


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
    assert result.vocal_presence.status is VocalPresenceStatus.VOCALS
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
    assert result.vocal_presence.status is VocalPresenceStatus.UNKNOWN
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


def test_dsp_analysis_does_not_block_event_loop(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    _write_test_audio(audio)
    adapter = BasicDspAdapter()
    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocked_analysis(asset):
        worker_started.set()
        if not release_worker.wait(timeout=2):
            raise TimeoutError("test did not release DSP worker")
        return DspResult()

    monkeypatch.setattr(adapter, "_analyze_sync", blocked_analysis)

    async def exercise():
        task = asyncio.create_task(adapter.analyze(_asset(audio)))
        for _ in range(100):
            if worker_started.is_set():
                break
            await asyncio.sleep(0)
        assert worker_started.is_set()
        # This checkpoint runs while the synchronous DSP worker is blocked.
        await asyncio.sleep(0)
        assert not task.done()
        release_worker.set()
        return await task

    result = asyncio.run(exercise())

    assert isinstance(result, DspResult)


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


def test_preprocessor_concurrent_same_content_publishes_atomic_wav(
    tmp_path,
    monkeypatch,
):
    import music_insight.audio as audio_module

    audio = tmp_path / "source.wav"
    _write_test_audio(audio)
    real_decode = audio_module.decode_mono
    concurrent_decodes = threading.Barrier(2)

    def synchronized_decode(*args, **kwargs):
        concurrent_decodes.wait(timeout=2)
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(audio_module, "decode_mono", synchronized_decode)
    preprocessor = Preprocessor(workspace_dir=tmp_path / "cache")

    async def exercise():
        return await asyncio.gather(
            preprocessor.prepare(_asset(audio)),
            preprocessor.prepare(_asset(audio)),
        )

    first, second = asyncio.run(exercise())

    assert first.scene.path == second.scene.path
    assert first.scene.path != audio
    assert first.scene.path.stat().st_size > 44
    assert first.evidence[0].id == "preprocess.omni.wav"
    assert second.evidence[0].id == "preprocess.omni.wav"
    assert not list(first.scene.path.parent.glob("*.tmp.wav"))


def test_preprocessor_does_not_fallback_to_original_on_normalization_error(
    tmp_path,
    monkeypatch,
):
    audio = tmp_path / "source.wav"
    _write_test_audio(audio)
    preprocessor = Preprocessor(workspace_dir=tmp_path / "cache")

    def fail_normalization(*args, **kwargs):
        raise RuntimeError("test normalization failure")

    monkeypatch.setattr(preprocessor, "_normalize_for_omni", fail_normalization)
    prepared = asyncio.run(preprocessor.prepare(_asset(audio)))

    assert prepared.scene is None
    assert prepared.evidence[0].id == "preprocess.omni.error"
    assert "已跳过统一模型" in prepared.evidence[0].text


def test_orchestrator_skips_unified_model_when_normalization_fails(
    tmp_path,
    monkeypatch,
):
    class MustNotRunUnified(FakeUnifiedAdapter):
        called = False

        async def analyze(self, asset, dsp, progress=None):
            self.called = True
            raise AssertionError("unified model must not receive the original file")

    audio = tmp_path / "source.wav"
    _write_test_audio(audio)
    preprocessor = Preprocessor(workspace_dir=tmp_path / "cache")

    def fail_normalization(*args, **kwargs):
        raise RuntimeError("test normalization failure")

    monkeypatch.setattr(preprocessor, "_normalize_for_omni", fail_normalization)
    unified = MustNotRunUnified()
    orchestrator = AnalysisOrchestrator(
        unified=unified,
        dsp=FakeDspAdapter(),
        preprocessor=preprocessor,
    )

    result = asyncio.run(orchestrator.analyze(_asset(audio)))

    assert unified.called is False
    assert not result.lyrics


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


def test_unified_adapter_chunks_wav_with_overlap(tmp_path):
    audio = tmp_path / "long.wav"
    _write_test_audio(audio, seconds=12.0, sample_rate=16_000)
    adapter = QwenOmniUnifiedAdapter(
        endpoint="http://127.0.0.1:9999",
        model="test-model",
        chunk_seconds=5.0,
    )

    chunks = list(adapter._wav_chunks(audio))

    assert [chunk[1:] for chunk in chunks] == [
        (0.0, 5.0),
        (3.5, 8.5),
        (7.0, 12.0),
    ]


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
                {
                    "description": "鼓声",
                    "start_time": 20,
                    "end_time": 35,
                    "dimension": "rhythm",
                },
                {
                    "description": "越界幻觉",
                    "start_time": 40,
                    "end_time": 42,
                    "dimension": "other",
                },
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
    assert (
        parsed["sound_events"][0].metadata["teaching_dimension"]
        == "rhythm"
    )


def test_chunk_parser_keeps_missing_timestamps_unknown_for_each_line():
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
    assert [item.span for item in parsed["lyrics"]] == [None, None]


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
    vocal_recovery = adapter._recovery_response_format(
        ["lyrics", "vocals_detected", "vocal_confidence"]
    )
    assert set(
        vocal_recovery["json_schema"]["schema"]["properties"]
    ) == {"lyrics", "vocals_detected", "vocal_confidence"}


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


def test_missing_lyrics_recovery_is_quality_filtered_before_use(tmp_path):
    audio = tmp_path / "recover-hallucination.wav"
    _write_test_audio(audio, seconds=2.0, sample_rate=16_000)
    adapter = FakeHallucinatedRecoveryOmni(
        endpoint="http://127.0.0.1:9999",
        model="test-model",
    )

    result = asyncio.run(adapter.analyze(_asset(audio, language="en"), DspResult()))

    assert result.asr.lyrics == []
    quality_evidence = next(
        item
        for item in result.scene.evidence
        if item.id.endswith(".recovery.quality")
    )
    assert quality_evidence.confidence == 0
    assert quality_evidence.metadata["issues"]


def test_unified_adapter_confirms_instrumental_only_from_chunk_consensus(
    tmp_path,
):
    audio = tmp_path / "instrumental.wav"
    _write_test_audio(audio, seconds=6.0, sample_rate=16_000)
    adapter = FakeInstrumentalOmni(
        endpoint="http://127.0.0.1:9999",
        model="test-model",
        chunk_seconds=5,
    )

    result = asyncio.run(adapter.analyze(_asset(audio), DspResult()))

    assert result.asr.lyrics == []
    assert result.scene.vocals_detected is False
    assert result.scene.vocal_confidence == pytest.approx(0.92)
    aggregate = next(
        item
        for item in result.scene.evidence
        if item.id == "omni.vocal_presence"
    )
    assert aggregate.metadata["expected_chunks"] == 2
    assert aggregate.metadata["classified_chunks"] == 2


def test_unified_adapter_keeps_partial_no_vocal_coverage_unknown(tmp_path):
    audio = tmp_path / "partial-instrumental.wav"
    _write_test_audio(audio, seconds=6.0, sample_rate=16_000)
    adapter = FakePartiallyClassifiedOmni(
        endpoint="http://127.0.0.1:9999",
        model="test-model",
        chunk_seconds=5,
    )

    result = asyncio.run(adapter.analyze(_asset(audio), DspResult()))

    assert result.scene.vocals_detected is None
    assert result.scene.vocal_confidence is None
    assert not any(
        item.id == "omni.vocal_presence"
        for item in result.scene.evidence
    )


def test_unified_adapter_recovers_vocal_presence_without_extra_call(tmp_path):
    audio = tmp_path / "recovered-instrumental.wav"
    _write_test_audio(audio, seconds=6.0, sample_rate=16_000)
    adapter = FakeRecoveredInstrumentalOmni(
        endpoint="http://127.0.0.1:9999",
        model="test-model",
        chunk_seconds=5,
    )

    result = asyncio.run(adapter.analyze(_asset(audio), DspResult()))

    assert result.scene.vocals_detected is False
    assert result.scene.vocal_confidence == pytest.approx(0.9)
    assert adapter.requested_missing_fields == [
        ["lyrics", "vocals_detected", "vocal_confidence"],
        ["lyrics", "vocals_detected", "vocal_confidence"],
    ]
    assert not any(
        item.id.endswith(".recovery.inconclusive")
        for item in result.scene.evidence
    )
    instrumentation = [
        item
        for item in result.scene.evidence
        if item.id.endswith(".instrumentation")
    ]
    assert len(instrumentation) == 2
    assert all(
        item.metadata["teaching_dimension"] == "instrumentation"
        for item in instrumentation
    )
    assert not any(
        item.text.startswith("已完成第")
        for item in result.scene.evidence
    )


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


def test_unified_adapter_aborts_remaining_chunks_when_provider_is_unavailable(
    tmp_path,
):
    audio = tmp_path / "batch-unavailable.wav"
    _write_test_audio(audio, seconds=12.0, sample_rate=16_000)
    adapter = FakeBatchUnavailableOmni(
        endpoint="http://127.0.0.1:9999",
        model="test-model",
        chunk_seconds=5,
        chunk_overlap_seconds=0,
    )

    result = asyncio.run(adapter.analyze(_asset(audio), DspResult()))

    assert adapter.analysis_calls == 1
    assert adapter.synthesis_calls == 0
    batch_error = next(
        item for item in result.scene.evidence if item.id == "omni.batch.error"
    )
    assert batch_error.metadata["failed_chunk"] == 1
    assert batch_error.metadata["skipped_chunks"] == 2
    assert "Worker busy" in batch_error.text


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


def test_bad_lyrics_retry_can_confirm_instrumental_without_another_call(
    tmp_path,
):
    audio = tmp_path / "quality-retry-instrumental.wav"
    _write_test_audio(audio, seconds=2.0, sample_rate=16_000)
    adapter = FakeQualityRetryInstrumentalOmni(
        endpoint="http://127.0.0.1:9999",
        model="test-model",
    )

    result = asyncio.run(adapter.analyze(_asset(audio), DspResult()))

    assert adapter.quality_recovery_calls == 1
    assert result.asr.lyrics == []
    assert result.scene.vocals_detected is False
    assert result.scene.vocal_confidence == pytest.approx(0.94)


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

    assert parsed == {
        "lyrics": [],
        "instruments": [],
        "vocals_detected": None,
        "vocal_confidence": None,
        "sound_events": [],
        "emotion_timeline": [],
        "themes": [],
        "narrative": "",
    }
    assert len(adapter.requests) == 2
    assert adapter.requests[1]["response_format"] == {"type": "json_object"}
    assert adapter.requests[1]["max_tokens"] == 1200


def test_qwen_adapter_does_not_guess_transport_from_model_name():
    adapter = QwenOmniUnifiedAdapter(
        endpoint="http://127.0.0.1:9999",
        model="MiniCPM-o-4_5-Q4_K_M.gguf",
    )

    assert asyncio.run(adapter._model()) == "MiniCPM-o-4_5-Q4_K_M.gguf"
    assert adapter.source == "Qwen Omni · http://127.0.0.1:9999"


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
    paths = set(app.openapi()["paths"])

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


def test_upload_store_removes_partial_file_when_cancelled(tmp_path):
    class SlowUpload:
        filename = "cancelled.wav"
        content_type = "audio/wav"

        def __init__(self):
            self.read_count = 0
            self.second_read_started = asyncio.Event()

        async def read(self, size):
            self.read_count += 1
            if self.read_count == 1:
                return b"partial"
            self.second_read_started.set()
            await asyncio.Event().wait()

    store = LocalAudioStore(tmp_path / "uploads")

    async def exercise():
        upload = SlowUpload()
        task = asyncio.create_task(store.save_upload(upload))  # type: ignore[arg-type]
        await asyncio.wait_for(upload.second_read_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert list((tmp_path / "uploads").iterdir()) == []


def test_decode_mono_stops_at_duration_cap(tmp_path):
    audio = tmp_path / "too-long.wav"
    _write_test_audio(audio, seconds=2.0, sample_rate=8_000)

    with pytest.raises(AudioDurationExceededError):
        decode_mono(audio, sample_rate=8_000, max_duration_s=1.0)


def test_decode_mono_ignores_valid_trailing_apev2_metadata(tmp_path):
    audio = tmp_path / "ape-tagged.mp3"
    _write_mp3_with_apev2_tag(audio)

    decoded, sample_rate = decode_mono(audio, sample_rate=16_000)

    assert sample_rate == 16_000
    assert len(decoded) / sample_rate == pytest.approx(1.0, abs=0.05)


def test_decode_mono_rejects_invalid_audio_with_apev2_like_footer(tmp_path):
    audio = tmp_path / "invalid.mp3"
    _write_mp3_with_apev2_tag(audio)
    data = audio.read_bytes()
    tag_start = len(data) - int.from_bytes(data[-20:-16], "little")
    audio.write_bytes(b"not valid mp3 audio" + data[tag_start:])

    with pytest.raises(av.FFmpegError):
        decode_mono(audio, sample_rate=16_000)


def test_upload_validation_rejects_overlong_and_invalid_audio(tmp_path):
    long_audio = tmp_path / "long.wav"
    _write_test_audio(long_audio, seconds=61.0, sample_rate=8_000)
    settings = Settings(workspace_dir=tmp_path / "workspace", max_audio_minutes=1)

    async def exercise():
        overlong = UploadFile(
            file=io.BytesIO(long_audio.read_bytes()),
            filename="long.wav",
            headers=Headers({"content-type": "audio/wav"}),
        )
        with pytest.raises(HTTPException) as duration_error:
            await save_audio_upload(
                overlong,
                "en",
                settings,
                "user-a",
            )
        invalid = UploadFile(
            file=io.BytesIO(b"not an audio container"),
            filename="invalid.wav",
            headers=Headers({"content-type": "audio/wav"}),
        )
        with pytest.raises(HTTPException) as invalid_error:
            await save_audio_upload(
                invalid,
                "en",
                settings,
                "user-a",
            )
        return duration_error.value, invalid_error.value

    duration_error, invalid_error = asyncio.run(exercise())

    assert duration_error.status_code == 413
    assert invalid_error.status_code == 415
    assert not [
        path
        for path in (settings.workspace_dir / "users").rglob("*")
        if path.is_file()
    ]
