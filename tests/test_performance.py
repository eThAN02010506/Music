"""Performance baselines for the local, model-independent hot paths.

These are generous upper bounds (not micro-benchmarks) so they stay stable on
CI and slower machines while still catching a regression that makes a core
local path egregiously slow. They use the committed 30-second test WAVs and do
not require a model endpoint.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import time

import pytest

from music_insight.api.services.waveform import build_waveform
from music_insight.pipeline.preprocess import Preprocessor
from music_insight.schemas import (
    AnalysisResult,
    AudioAsset,
    DspResult,
    Evidence,
    EvidenceType,
    LyricsSegment,
    TimeSpan,
)
from music_insight.teaching.fallback import EvidenceTeachingModel
from music_insight.teaching.grounding import validate_understanding_map
from music_insight.teaching.models import (
    ListenerProfile,
    MapGenerationContext,
)

_SAMPLES = Path(__file__).parent / ".." / "test_samples"


def _minimal_result() -> AnalysisResult:
    observed = Evidence(
        id="scene.piano",
        source="audio-model",
        kind=EvidenceType.OBSERVED,
        text="钢琴由稀疏单音变为连续和弦",
        confidence=0.88,
        span=TimeSpan(start_s=2, end_s=10),
    )
    energy = Evidence(
        id="dsp.energy",
        source="librosa",
        kind=EvidenceType.COMPUTED,
        text="能量曲线在中段持续上升",
        confidence=0.91,
        span=TimeSpan(start_s=6, end_s=16),
    )
    return AnalysisResult(
        title="perf",
        summary="钢琴从稀疏到连续，情绪逐渐明亮。",
        lyrics=[
            LyricsSegment(
                text="等到放晴那天",
                span=TimeSpan(start_s=2, end_s=6),
                confidence=0.9,
            )
        ],
        instruments=["钢琴"],
        sound_events=[observed],
        emotion_timeline=[
            Evidence(
                id="scene.emotion",
                source="audio-model",
                kind=EvidenceType.INFERRED,
                text="情绪从克制逐渐变得明亮",
                confidence=0.72,
                span=TimeSpan(start_s=8, end_s=18),
            )
        ],
        inferred_atmosphere=[],
        themes=["夜空", "希望"],
        technical_metrics=DspResult(
            bpm=112.0,
            bpm_confidence=0.4,
            key="F major",
            key_confidence=0.3,
            energy_curve=[
                Evidence(
                    id=f"dsp.energy.{i}",
                    source="librosa",
                    kind=EvidenceType.COMPUTED,
                    text=f"能量 {v:.2f}",
                    confidence=0.9,
                    span=TimeSpan(start_s=i * 2, end_s=i * 2 + 2),
                )
                for i, v in enumerate((0.1, 0.2, 0.3, 0.4, 0.5))
            ],
            evidence=[energy],
        ),
        evidence=[observed, energy],
        warnings=[],
    )


@pytest.fixture(scope="module")
def short_wav(tmp_path_factory):
    """The 30-second committed sample, copied so we do not mutate the sample."""
    source = (_SAMPLES / "twinkle_30s.wav").resolve()
    if not source.is_file():
        pytest.skip("test_samples/twinkle_30s.wav not present")
    path = tmp_path_factory.mktemp("perf") / "twinkle.wav"
    path.write_bytes(source.read_bytes())
    return path


def test_waveform_build_budget(short_wav):
    start = time.perf_counter()
    waveform = build_waveform(short_wav, points=1200)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert waveform.duration_s == pytest.approx(30.0, abs=1.0)
    assert elapsed_ms < 2_000


def test_preprocessing_budget(short_wav):
    start = time.perf_counter()
    asyncio.run(
        Preprocessor(short_wav.parent / "prep").prepare(
            AudioAsset(
                path=short_wav,
                media_type="audio/wav",
                size_bytes=short_wav.stat().st_size,
            )
        )
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 10_000


def test_fallback_map_generation_budget():
    """The evidence-only guide must be fast enough to build on demand."""

    result = _minimal_result()
    context = MapGenerationContext(
        analysis_id="perf-guide",
        result=result,
        duration_s=20,
        language="zh",
        output_language="zh",
        listener_profile=ListenerProfile(),
    )
    start = time.perf_counter()
    guide = asyncio.run(EvidenceTeachingModel().build_understanding_map(context))
    elapsed_ms = (time.perf_counter() - start) * 1000

    validate_understanding_map(guide, result=result, duration_s=20)
    assert guide.events
    assert elapsed_ms < 1_000
