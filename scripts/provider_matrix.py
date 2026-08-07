# Probe and exercise each configured network model endpoint.
#
# This is the FR-AN-008 regression harness: for every endpoint reachable from
# the configured settings it records capability discovery, first-event and
# total latency for a fixed short audio, structured-output success, and
# timeout/busy behavior. Run with the endpoints you want to verify, e.g.:
#
#     PYTHONPATH=src python scripts/provider_matrix.py \
#       http://192.168.1.97:8004 \
#       http://192.168.1.97:8005
#
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from music_insight.adapters.model_capabilities import (
    clear_probe_cache,
    probe_model_service,
)
from music_insight.adapters.network_omni import (
    NetworkOmniAdapter,
    NetworkOmniProviderRegistry,
)
from music_insight.schemas import AudioAsset, DspResult


def _registry() -> NetworkOmniProviderRegistry:
    adapter = NetworkOmniAdapter(endpoint="http://127.0.0.1:1")
    return adapter.registry


def _registry() -> NetworkOmniProviderRegistry:
    adapter = NetworkOmniAdapter(endpoint="http://127.0.0.1:1")
    return adapter.registry


async def _probe_endpoint(endpoint: str) -> dict:
    start = time.perf_counter()
    clear_probe_cache(endpoint)
    capabilities = await probe_model_service(endpoint, timeout=8.0)
    probe_ms = (time.perf_counter() - start) * 1000
    return {
        "endpoint": endpoint,
        "online": capabilities.online,
        "protocol": capabilities.protocol,
        "model": capabilities.model,
        "analysis_supported": capabilities.analysis_supported,
        "audio_supported": capabilities.audio_supported,
        "openai_audio_supported": capabilities.openai_audio_supported,
        "detail": capabilities.detail,
        "probe_ms": round(probe_ms, 1),
    }


async def _exercise_analysis(endpoint: str, audio: Path, duration_hint: float) -> dict:
    """Run a real analysis on one short audio and time the phases."""
    adapter = NetworkOmniAdapter(
        endpoint=endpoint,
        chunk_seconds=min(30.0, max(5.0, duration_hint)),
    )
    asset = AudioAsset(
        path=audio,
        media_type="audio/wav",
        size_bytes=audio.stat().st_size,
        language_hint="zh",
    )
    events: list[tuple[str, float]] = []

    async def progress(stage: str, value: float, message: str) -> None:
        events.append((stage, value))

    start = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            adapter.analyze(asset, DspResult(), progress=progress),
            timeout=120,
        )
    except asyncio.TimeoutError:
        return {"analysis_status": "timeout", "elapsed_ms": round((time.perf_counter() - start) * 1000, 1)}
    except Exception as exc:
        return {
            "analysis_status": "error",
            "error": f"{type(exc).__name__}: {str(exc)[:120]}",
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
        }
    total_ms = (time.perf_counter() - start) * 1000
    return {
        "analysis_status": "ok",
        "elapsed_ms": round(total_ms, 1),
        "lyrics": len(result.asr.lyrics),
        "instruments": len(result.scene.instruments),
        "sound_events": len(result.scene.sound_events),
        "emotions": len(result.scene.emotion_timeline),
        "stages": [stage for stage, _ in events],
        "vocal_status": result.asr.lyrics and "lyrics" or "unknown",
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoints", nargs="+", help="Model endpoint URLs to verify")
    parser.add_argument(
        "--audio",
        default="test_samples/johnny_cash_new_mexico_30s.wav",
        help="Short WAV used for the analysis exercise",
    )
    args = parser.parse_args()

    audio = Path(args.audio)
    if not audio.is_file():
        raise SystemExit(f"Missing audio file: {audio}")

    duration_hint = 30.0 if "30s" in audio.name else 5.0
    registry = _registry()
    report: dict = {"protocols": list(registry.protocols), "endpoints": []}

    for endpoint in args.endpoints:
        probe = await _probe_endpoint(endpoint)
        entry = {"probe": probe}
        if probe["online"] and probe["analysis_supported"]:
            entry["analysis"] = await _exercise_analysis(endpoint, audio, duration_hint)
        report["endpoints"].append(entry)

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
