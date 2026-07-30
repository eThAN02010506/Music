import asyncio
import math
import wave

import httpx
import pytest

from music_insight.adapters.openai_asr_verifier import (
    AsrVerificationHttpError,
    AsrVerificationProtocolError,
    OpenAIAsrVerifier,
)
from music_insight.config import Settings
from music_insight.pipeline.asr_verification import AsrVerificationFusion
from music_insight.pipeline.orchestrator import AnalysisOrchestrator
from music_insight.schemas import (
    AsrResult,
    AsrVerificationResult,
    AudioAsset,
    AudioSceneResult,
    DspResult,
    Evidence,
    EvidenceType,
    LiteraryResult,
    LyricsSegment,
    TimeSpan,
    UnifiedAudioResult,
    VerifiedLyricsSynthesisResult,
    VocalPresenceStatus,
)


class _ChunkedResponse(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.iterated = False

    async def __aiter__(self):
        self.iterated = True
        for chunk in self.chunks:
            yield chunk


def _write_silent_wav(path, duration_s=4.0):
    sample_rate = 16_000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\0\0" * int(sample_rate * duration_s))


def _asset(path, language="zh"):
    return AudioAsset(
        path=path,
        media_type="audio/wav",
        size_bytes=path.stat().st_size,
        language_hint=language,
    )


def _primary(text="主模型候选歌词"):
    return AsrResult(
        model="Qwen2.5-Omni",
        lyrics=[
            LyricsSegment(
                text=text,
                span=TimeSpan(start_s=0, end_s=2),
                language="zh",
                confidence=0.7,
            )
        ],
        evidence=[],
    )


def test_openai_asr_verifier_sends_multipart_and_parses_timestamps(tmp_path):
    audio = tmp_path / "song.wav"
    _write_silent_wav(audio)
    observed = {}

    async def handler(request):
        observed["method"] = request.method
        observed["path"] = request.url.path
        observed["authorization"] = request.headers.get("authorization")
        observed["content_type"] = request.headers.get("content-type")
        observed["body"] = await request.aread()
        return httpx.Response(
            200,
            json={
                "task": "transcribe",
                "language": "zh",
                "duration": 4.0,
                "text": "星光照亮夜空",
                "segments": [
                    {
                        "id": 0,
                        "start": 0.4,
                        "end": 2.2,
                        "text": "星光照亮",
                        "avg_logprob": -0.2,
                        "no_speech_prob": 0.1,
                    },
                    {
                        "id": 1,
                        "start": 2.2,
                        "end": 3.8,
                        "text": "夜空",
                        "avg_logprob": 0,
                        "no_speech_prob": 0.0,
                    },
                ],
            },
        )

    verifier = OpenAIAsrVerifier(
        endpoint="http://127.0.0.1:18003",
        dialect="crisp_asr",
        model="mimo-asr",
        api_key="test-token",
        vad=True,
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(verifier.verify(_asset(audio)))

    assert observed["method"] == "POST"
    assert observed["path"] == "/v1/audio/transcriptions"
    assert observed["authorization"] == "Bearer test-token"
    assert observed["content_type"].startswith("multipart/form-data;")
    assert b'name="response_format"' in observed["body"]
    assert b"verbose_json" in observed["body"]
    assert b'name="model"' in observed["body"]
    assert b"mimo-asr" in observed["body"]
    assert b'name="vad"' in observed["body"]
    assert result.vocals_detected is True
    assert result.vocal_confidence == 0.95
    assert len(result.segments) == 2
    assert result.segments[0].span == TimeSpan(start_s=0.4, end_s=2.2)
    assert result.segments[0].confidence == math.exp(-0.2) * 0.9
    assert result.segments[1].confidence is None
    assert result.transcript_confidence is None


def test_openai_asr_verifier_successful_empty_is_distinct_and_omits_false_vad(
    tmp_path,
):
    audio = tmp_path / "silence.wav"
    _write_silent_wav(audio)
    observed = {}

    async def handler(request):
        observed["body"] = await request.aread()
        return httpx.Response(
            200,
            json={
                "task": "transcribe",
                "language": "zh",
                "duration": 4.0,
                "text": "",
                "segments": [],
            },
        )

    verifier = OpenAIAsrVerifier(
        endpoint="http://127.0.0.1:18003",
        dialect="crisp_asr",
        vad=False,
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(verifier.verify(_asset(audio)))

    assert b'name="vad"' not in observed["body"]
    assert result.segments == []
    assert result.vocals_detected is None
    assert result.transcript_confidence is None
    assert result.evidence[0].id == "asr.verifier.response"


def test_openai_asr_verifier_rejects_text_without_timestamp_segments(tmp_path):
    audio = tmp_path / "song.wav"
    _write_silent_wav(audio)

    async def handler(_request):
        return httpx.Response(200, json={"text": "没有可追溯时间轴", "segments": []})

    verifier = OpenAIAsrVerifier(
        endpoint="http://127.0.0.1:18003",
        dialect="crisp_asr",
        transport=httpx.MockTransport(handler),
    )

    try:
        asyncio.run(verifier.verify(_asset(audio)))
    except AsrVerificationProtocolError as error:
        assert "没有可验证的时间片段" in str(error)
    else:
        raise AssertionError("timestamp-free ASR text must not be accepted")


def test_openai_asr_verifier_does_not_treat_unrelated_200_json_as_silence(
    tmp_path,
):
    audio = tmp_path / "song.wav"
    _write_silent_wav(audio)

    async def handler(_request):
        return httpx.Response(200, json={"detail": "wrong route"})

    verifier = OpenAIAsrVerifier(
        endpoint="http://127.0.0.1:18003",
        dialect="crisp_asr",
        transport=httpx.MockTransport(handler),
    )

    try:
        asyncio.run(verifier.verify(_asset(audio)))
    except AsrVerificationProtocolError as error:
        assert "text 字段" in str(error)
    else:
        raise AssertionError("unrelated JSON must be unavailable, not silence")


def test_standard_whisper_dialect_sends_required_model_and_segment_timestamps(
    tmp_path,
):
    audio = tmp_path / "song.wav"
    _write_silent_wav(audio)
    observed = {}

    async def handler(request):
        observed["body"] = await request.aread()
        return httpx.Response(
            200,
            json={
                "text": "hello",
                "segments": [
                    {
                        "start": 0.2,
                        "end": 1.4,
                        "text": "hello",
                        "confidence": 0.9,
                    }
                ],
            },
        )

    verifier = OpenAIAsrVerifier(
        endpoint="http://127.0.0.1:18003",
        dialect="openai_whisper",
        model="whisper-1",
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(verifier.verify(_asset(audio, language="en")))

    assert b'name="model"' in observed["body"]
    assert b"whisper-1" in observed["body"]
    assert b'name="timestamp_granularities[]"' in observed["body"]
    assert b"segment" in observed["body"]
    assert b'name="vad"' not in observed["body"]
    assert result.segments[0].text == "hello"


def test_standard_whisper_requires_model_and_rejects_crisp_vad():
    with pytest.raises(ValueError, match="model is required"):
        OpenAIAsrVerifier(
            endpoint="http://127.0.0.1:18003",
            dialect="openai_whisper",
        )

    with pytest.raises(ValueError, match="crisp_asr"):
        OpenAIAsrVerifier(
            endpoint="http://127.0.0.1:18003",
            dialect="openai_whisper",
            model="whisper-1",
            vad=True,
        )

    with pytest.raises(ValueError, match="asr_verifier_model"):
        Settings(
            asr_verifier_enabled=True,
            asr_verifier_dialect="openai_whisper",
        )


@pytest.mark.parametrize("status_code", [200, 500])
def test_asr_stream_hard_limits_success_and_error_bodies(
    tmp_path,
    status_code,
):
    audio = tmp_path / "song.wav"
    _write_silent_wav(audio)
    stream = _ChunkedResponse([b"x" * 12, b"y" * 12])

    async def handler(_request):
        return httpx.Response(status_code, stream=stream)

    verifier = OpenAIAsrVerifier(
        endpoint="http://127.0.0.1:18003",
        dialect="crisp_asr",
        transport=httpx.MockTransport(handler),
    )
    verifier.MAX_RESPONSE_BYTES = 16

    with pytest.raises(AsrVerificationProtocolError, match="大小限制"):
        asyncio.run(verifier.verify(_asset(audio)))

    assert stream.iterated is True


def test_asr_http_error_body_and_authorization_are_redacted(tmp_path):
    audio = tmp_path / "song.wav"
    _write_silent_wav(audio)
    upstream_secret = "Bearer upstream-secret-token"
    stream = _ChunkedResponse(
        [f'{{"detail":"{upstream_secret}"}}'.encode()]
    )

    async def handler(request):
        assert request.headers["authorization"] == upstream_secret
        await request.aread()
        return httpx.Response(401, stream=stream)

    verifier = OpenAIAsrVerifier(
        endpoint="http://127.0.0.1:18003",
        dialect="crisp_asr",
        api_key="upstream-secret-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AsrVerificationHttpError) as raised:
        asyncio.run(verifier.verify(_asset(audio)))

    assert stream.iterated is True
    assert raised.value.status_code == 401
    assert "upstream-secret-token" not in str(raised.value)

    evidence_result = AsrVerificationFusion.mark_unavailable(
        _primary(),
        source=verifier.source,
        error=RuntimeError(upstream_secret),
    )
    serialized = evidence_result.model_dump_json()
    assert "upstream-secret-token" not in serialized
    assert evidence_result.evidence[-1].text == (
        "歌词二次验证不可用，已保留统一模型歌词。"
    )


def test_empty_transcript_without_no_speech_evidence_is_inconclusive():
    verification = AsrVerificationResult(
        model="MiMo ASR",
        segments=[],
        vocals_detected=None,
        vocal_confidence=None,
    )

    decision = AsrVerificationFusion().evaluate(_primary(), verification)

    assert decision.status == "inconclusive"
    assert [item.text for item in decision.result.lyrics] == [
        "主模型候选歌词"
    ]
    assert decision.result.evidence[-1].id == "asr.verifier.inconclusive"


def test_explicit_high_confidence_no_speech_can_clear_primary(tmp_path):
    audio = tmp_path / "silence.wav"
    _write_silent_wav(audio)

    async def handler(_request):
        return httpx.Response(
            200,
            json={
                "text": "",
                "segments": [],
                "no_speech_prob": 0.94,
            },
        )

    verifier = OpenAIAsrVerifier(
        endpoint="http://127.0.0.1:18003",
        dialect="crisp_asr",
        transport=httpx.MockTransport(handler),
    )
    verification = asyncio.run(verifier.verify(_asset(audio)))
    decision = AsrVerificationFusion().evaluate(_primary(), verification)

    assert verification.vocals_detected is False
    assert verification.vocal_confidence == 0.94
    assert decision.status == "verified_silence"
    assert decision.result.lyrics == []
    assert decision.result.evidence[-1].confidence == 0.94


def test_asr_fusion_prioritizes_verified_text_and_timeline():
    verification = AsrVerificationResult(
        model="MiMo ASR",
        segments=[
            LyricsSegment(
                text="专用 ASR 歌词",
                span=TimeSpan(start_s=1.2, end_s=3.4),
                confidence=0.62,
            )
        ],
        vocals_detected=True,
        transcript_confidence=0.62,
    )

    fusion = AsrVerificationFusion()
    decision = fusion.evaluate(
        _primary(),
        verification,
        duration_s=4.0,
    )
    result = fusion.finalize_candidate(decision)

    assert [item.text for item in result.lyrics] == ["专用 ASR 歌词"]
    assert result.lyrics[0].span == TimeSpan(start_s=1.2, end_s=3.4)
    assert result.evidence[-1].id == "asr.verifier.verified"
    assert result.model == "Qwen2.5-Omni"


def test_asr_fusion_successful_empty_removes_primary_as_verified_silence():
    verification = AsrVerificationResult(
        model="MiMo ASR",
        segments=[],
        vocals_detected=False,
        vocal_confidence=0.95,
    )

    result = AsrVerificationFusion().evaluate(
        _primary(),
        verification,
    ).result

    assert result.lyrics == []
    assert result.evidence[-1].id == "asr.verifier.verified_silence"
    assert result.evidence[-1].metadata["primary_segments_removed"] == 1


def test_asr_fusion_rejects_implausibly_dense_long_audio_transcript():
    verification = AsrVerificationResult(
        model="MiMo ASR",
        segments=[
            LyricsSegment(
                text=f"这是明显过于密集的幻觉歌词片段{i}",
                span=TimeSpan(start_s=i, end_s=i + 0.1),
            )
            for i in range(12)
        ],
        vocals_detected=True,
    )

    result = AsrVerificationFusion().evaluate(
        _primary(),
        verification,
        duration_s=30,
    ).result

    assert [item.text for item in result.lyrics] == ["主模型候选歌词"]
    assert result.evidence[-1].id == "asr.verifier.inconclusive"
    assert result.evidence[-1].metadata["status"] == "inconclusive"


def test_asr_fusion_uses_raw_received_count_for_ten_to_one_valid_ratio():
    verification = AsrVerificationResult(
        model="MiMo ASR",
        segments=[
            LyricsSegment(
                text="唯一有效歌词",
                span=TimeSpan(start_s=0.5, end_s=2.5),
                confidence=0.9,
            )
        ],
        segments_received=10,
        segments_invalid=9,
        duration_s=4,
        vocals_detected=True,
        transcript_confidence=0.9,
    )

    decision = AsrVerificationFusion().evaluate(_primary(), verification)

    assert decision.status == "inconclusive"
    assert decision.metrics["valid_ratio"] == 0.1
    assert [item.text for item in decision.result.lyrics] == [
        "主模型候选歌词"
    ]


def test_asr_fusion_rejects_more_than_240_without_truncating_and_accepting():
    verification = AsrVerificationResult(
        model="MiMo ASR",
        segments=[
            LyricsSegment(
                text=f"line {index}",
                span=TimeSpan(start_s=index * 2, end_s=index * 2 + 1),
                confidence=0.9,
            )
            for index in range(241)
        ],
        duration_s=500,
        vocals_detected=True,
        transcript_confidence=0.9,
    )

    decision = AsrVerificationFusion().evaluate(_primary(), verification)

    assert decision.status == "inconclusive"
    assert decision.metrics["segments_received"] == 241
    assert len(decision.result.lyrics) == 1
    assert "超过安全上限" in decision.result.evidence[-1].text


def test_no_confidence_high_chinese_agreement_only_corroborates_primary():
    verification = AsrVerificationResult(
        model="MiMo ASR",
        segments=[
            LyricsSegment(
                text="主模型，候选歌词！",
                span=TimeSpan(start_s=0.5, end_s=2.8),
                confidence=None,
            )
        ],
        duration_s=4,
        vocals_detected=True,
    )
    fusion = AsrVerificationFusion()

    decision = fusion.evaluate(_primary(), verification)
    finalized = fusion.finalize_candidate(decision)

    assert decision.status == "corroboration_candidate"
    assert decision.metrics["text_agreement"] == 1
    assert finalized.lyrics == _primary().lyrics
    assert finalized.evidence[-1].metadata["mode"] == "corroboration"


def test_no_confidence_high_english_agreement_normalizes_case_and_punctuation():
    primary = AsrResult(
        model="Qwen2.5-Omni",
        lyrics=[
            LyricsSegment(
                text="We're in the night!",
                span=TimeSpan(start_s=0, end_s=2),
                language="en",
            )
        ],
        evidence=[],
    )
    verification = AsrVerificationResult(
        model="Whisper",
        segments=[
            LyricsSegment(
                text="WE’RE, in the night.",
                span=TimeSpan(start_s=0.2, end_s=2.2),
                language="en",
            )
        ],
        duration_s=4,
        vocals_detected=True,
    )

    decision = AsrVerificationFusion().evaluate(primary, verification)

    assert decision.status == "corroboration_candidate"
    assert decision.metrics["text_agreement"] >= 0.88


def test_no_confidence_conflict_and_explicit_low_confidence_keep_primary():
    fusion = AsrVerificationFusion()
    conflicting = AsrVerificationResult(
        model="MiMo ASR",
        segments=[
            LyricsSegment(
                text="完全不同的内容",
                span=TimeSpan(start_s=0.5, end_s=2.8),
                confidence=None,
            )
        ],
        duration_s=4,
        vocals_detected=True,
    )
    low_confidence = conflicting.model_copy(
        update={
            "segments": [
                conflicting.segments[0].model_copy(
                    update={
                        "text": "主模型候选歌词",
                        "confidence": 0.1,
                    }
                )
            ],
            "transcript_confidence": 0.1,
        }
    )

    conflict_decision = fusion.evaluate(_primary(), conflicting)
    low_decision = fusion.evaluate(_primary(), low_confidence)

    assert conflict_decision.status == "inconclusive"
    assert low_decision.status == "inconclusive"
    assert conflict_decision.result.lyrics == _primary().lyrics
    assert low_decision.result.lyrics == _primary().lyrics


def test_high_confidence_but_tiny_timeline_coverage_cannot_replace_long_song():
    verification = AsrVerificationResult(
        model="MiMo ASR",
        segments=[
            LyricsSegment(
                text="短句",
                span=TimeSpan(start_s=1, end_s=2),
                confidence=0.95,
            )
        ],
        duration_s=240,
        vocals_detected=True,
        transcript_confidence=0.95,
    )

    decision = AsrVerificationFusion().evaluate(_primary(), verification)

    assert decision.status == "inconclusive"
    assert decision.metrics["timeline_coverage"] < 0.05


class _FakeUnified:
    source = "Qwen2.5-Omni"

    async def analyze(self, asset, dsp, progress=None):
        return UnifiedAudioResult(
            asr=_primary(),
            scene=AudioSceneResult(model=self.source),
            literary=LiteraryResult(
                model=self.source,
                narrative="主模型报告",
            ),
        )


class _FakeDsp:
    async def analyze(self, asset):
        return DspResult()


class _TimeoutVerifier:
    source = "timeout ASR"
    endpoint = "http://127.0.0.1:18003"

    async def verify(self, asset):
        raise httpx.ReadTimeout("verification timed out")


class _StaticVerifier:
    source = "static ASR"
    endpoint = "http://127.0.0.1:18003"

    def __init__(self, result):
        self.result = result

    async def verify(self, asset):
        return self.result


def _strong_verification(text="新的可靠歌词"):
    return AsrVerificationResult(
        model="MiMo ASR",
        segments=[
            LyricsSegment(
                text=text,
                span=TimeSpan(start_s=0.4, end_s=2.6),
                language="zh",
                confidence=0.9,
            )
        ],
        duration_s=4,
        vocals_detected=True,
        vocal_confidence=0.9,
        transcript_confidence=0.9,
    )


class _OldLyricsUnified:
    source = "Qwen2.5-Omni"

    async def analyze(self, asset, dsp, progress=None):
        return UnifiedAudioResult(
            asr=_primary("旧歌词秘密"),
            scene=AudioSceneResult(
                model=self.source,
                instruments=["钢琴"],
                themes=["旧歌词场景"],
                narrative="旧歌词秘密形成怀旧叙事。",
                inferred_atmosphere=[
                    Evidence(
                        id="omni.final.atmosphere.1",
                        source=self.source,
                        kind=EvidenceType.INTERPRETIVE,
                        text="旧歌词氛围",
                    )
                ],
            ),
            literary=LiteraryResult(
                model=self.source,
                themes=["旧歌词主题"],
                narrative="OLD_LYRIC_SECRET：旧歌词秘密表达怀旧。",
            ),
        )


class _ResynthesizingUnified(_OldLyricsUnified):
    def __init__(self):
        self.received_lyrics = None

    async def resynthesize_verified_lyrics(self, lyrics, scene, dsp):
        self.received_lyrics = lyrics
        return VerifiedLyricsSynthesisResult(
            literary=LiteraryResult(
                model=self.source,
                themes=["新歌词主题"],
                narrative="只根据新的可靠歌词重新综合。",
            ),
            inferred_atmosphere=[
                Evidence(
                    id="omni.final.verified_lyrics.atmosphere.1",
                    source=self.source,
                    kind=EvidenceType.INTERPRETIVE,
                    text="新歌词氛围",
                )
            ],
        )


class _NoLyricsAudioUnified:
    source = "Qwen2.5-Omni"

    async def analyze(self, asset, dsp, progress=None):
        return UnifiedAudioResult(
            asr=AsrResult(model=self.source, lyrics=[], evidence=[]),
            scene=AudioSceneResult(
                model=self.source,
                instruments=["钢琴"],
                inferred_atmosphere=[
                    Evidence(
                        id="omni.final.atmosphere.1",
                        source=self.source,
                        kind=EvidenceType.INTERPRETIVE,
                        text="纯器乐宁静氛围",
                    )
                ],
            ),
            literary=LiteraryResult(
                model=self.source,
                themes=["纯器乐"],
                narrative="钢琴形成宁静的纯器乐声景。",
            ),
        )


def test_orchestrator_keeps_primary_lyrics_when_verifier_is_unavailable(tmp_path):
    audio = tmp_path / "song.wav"
    _write_silent_wav(audio)
    orchestrator = AnalysisOrchestrator(
        unified=_FakeUnified(),
        dsp=_FakeDsp(),
        asr_verifier=_TimeoutVerifier(),
    )

    result = asyncio.run(orchestrator.analyze(_asset(audio)))

    assert [item.text for item in result.lyrics] == ["主模型候选歌词"]
    unavailable = [
        item for item in result.evidence if item.id == "asr.verifier.unavailable"
    ]
    assert len(unavailable) == 1
    assert unavailable[0].metadata["status"] == "unverified"
    assert any("保留统一模型歌词" in warning for warning in result.warnings)


def test_orchestrator_resynthesizes_summary_after_verified_replacement(tmp_path):
    audio = tmp_path / "song.wav"
    _write_silent_wav(audio)
    unified = _ResynthesizingUnified()
    orchestrator = AnalysisOrchestrator(
        unified=unified,
        dsp=_FakeDsp(),
        asr_verifier=_StaticVerifier(_strong_verification()),
    )

    result = asyncio.run(orchestrator.analyze(_asset(audio)))

    assert [item.text for item in result.lyrics] == ["新的可靠歌词"]
    assert result.summary == "只根据新的可靠歌词重新综合。"
    assert result.themes == ["新歌词主题"]
    assert "OLD_LYRIC_SECRET" not in result.summary
    assert [item.text for item in unified.received_lyrics] == ["新的可靠歌词"]
    assert any(item.id == "asr.verifier.verified" for item in result.evidence)


def test_orchestrator_sanitizes_old_summary_when_resynthesis_is_unsupported(
    tmp_path,
):
    audio = tmp_path / "song.wav"
    _write_silent_wav(audio)
    orchestrator = AnalysisOrchestrator(
        unified=_OldLyricsUnified(),
        dsp=_FakeDsp(),
        asr_verifier=_StaticVerifier(_strong_verification()),
    )

    result = asyncio.run(orchestrator.analyze(_asset(audio)))

    assert [item.text for item in result.lyrics] == ["新的可靠歌词"]
    assert "OLD_LYRIC_SECRET" not in result.summary
    assert "旧歌词秘密" not in result.summary
    assert result.themes == []
    assert result.inferred_atmosphere == []
    assert any(
        item.id == "asr.verifier.consistency.unavailable"
        for item in result.evidence
    )


def test_language_rejected_candidate_restores_primary_without_verified_marker(
    tmp_path,
):
    audio = tmp_path / "song.wav"
    _write_silent_wav(audio)
    wrong_language = _strong_verification("completely different lyrics")
    orchestrator = AnalysisOrchestrator(
        unified=_OldLyricsUnified(),
        dsp=_FakeDsp(),
        asr_verifier=_StaticVerifier(wrong_language),
    )

    result = asyncio.run(orchestrator.analyze(_asset(audio, language="zh")))

    assert [item.text for item in result.lyrics] == ["旧歌词秘密"]
    assert result.summary == "OLD_LYRIC_SECRET：旧歌词秘密表达怀旧。"
    evidence_ids = {item.id for item in result.evidence}
    assert "asr.verifier.verified" not in evidence_ids
    assert "asr.verifier.transcript.rejected" in evidence_ids
    assert "asr.verifier.inconclusive" in evidence_ids


def test_verified_silence_does_not_clear_audio_report_when_primary_was_empty(
    tmp_path,
):
    audio = tmp_path / "instrumental.wav"
    _write_silent_wav(audio)
    silence = AsrVerificationResult(
        model="MiMo ASR",
        vocals_detected=False,
        vocal_confidence=0.95,
    )
    orchestrator = AnalysisOrchestrator(
        unified=_NoLyricsAudioUnified(),
        dsp=_FakeDsp(),
        asr_verifier=_StaticVerifier(silence),
    )

    result = asyncio.run(orchestrator.analyze(_asset(audio)))

    assert result.lyrics == []
    assert result.vocal_presence.status is VocalPresenceStatus.INSTRUMENTAL
    assert result.vocal_presence.confidence == 0.95
    assert not any(
        "没有足够证据断言为纯器乐" in warning
        for warning in result.warnings
    )
    assert result.summary == "钢琴形成宁静的纯器乐声景。"
    assert result.themes == ["纯器乐"]
    assert [item.text for item in result.inferred_atmosphere] == [
        "纯器乐宁静氛围"
    ]
