from __future__ import annotations

from collections.abc import Collection, Mapping
import math
import re
from typing import Any

from music_insight.schemas import Evidence, EvidenceType, LyricsSegment, TimeSpan


def meaningful(value: str, placeholder_values: Collection[str]) -> bool:
    text = value.strip()
    placeholders = {item.casefold() for item in placeholder_values}
    return bool(text) and text.casefold() not in placeholders


def meaningful_text(value: Any, placeholder_values: Collection[str]) -> str:
    text = str(value or "").strip()
    return text if meaningful(text, placeholder_values) else ""


def normalize_label(value: str, label_aliases: Mapping[str, str]) -> str:
    compact = " ".join(value.split())
    return label_aliases.get(compact.casefold(), compact)


def strings(
    value: Any,
    limit: int,
    *,
    placeholder_values: Collection[str],
    label_aliases: Mapping[str, str],
) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        text = normalize_label(str(item).strip(), label_aliases)
        if meaningful(text, placeholder_values):
            output.append(text)
        if len(output) >= limit:
            break
    return output


def confidence(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return min(1.0, max(0.0, float(value)))


def offset_span(
    item: dict[str, Any],
    chunk_start: float,
    duration: float,
) -> TimeSpan | None:
    start = item.get("start_s", item.get("start_time"))
    end = item.get("end_s", item.get("end_time"))
    if (
        isinstance(start, (int, float))
        and isinstance(end, (int, float))
        and 0 <= start <= end
        and start <= duration
    ):
        return TimeSpan(
            start_s=chunk_start + float(start),
            end_s=chunk_start + min(float(end), duration),
        )
    return None


def is_boundary_artifact(
    prefix: str,
    text: str,
    span: TimeSpan | None,
    chunk_start: float,
    duration: float,
    chunk_seconds: float,
) -> bool:
    if ".sound" not in prefix or span is None:
        return False
    label = " ".join(text.casefold().split())
    if label not in {
        "click",
        "clicking",
        "click sound",
        "pop",
        "popping",
        "点击声",
        "咔哒声",
        "爆音",
    }:
        return False
    event_duration = span.end_s - span.start_s
    local_start = span.start_s - chunk_start
    local_end = span.end_s - chunk_start
    at_internal_start = chunk_start > 0 and local_start <= 0.05
    at_full_chunk_end = (
        duration >= chunk_seconds - 0.05
        and duration - local_end <= 0.05
    )
    return event_duration <= 0.25 and (at_internal_start or at_full_chunk_end)


def evidence_items(
    value: Any,
    prefix: str,
    chunk_start: float,
    duration: float,
    *,
    source: str,
    chunk_seconds: float,
    placeholder_values: Collection[str],
) -> list[Evidence]:
    if not isinstance(value, list):
        return []
    results: list[Evidence] = []
    for index, item in enumerate(value[:6], start=1):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = str(
            item.get("text")
            or item.get("label")
            or item.get("description")
            or ""
        ).strip()
        if not meaningful(text, placeholder_values):
            continue
        start_value = item.get("start_s", item.get("start_time"))
        if isinstance(start_value, (int, float)) and start_value > duration:
            continue
        span = offset_span(item, chunk_start, duration)
        if is_boundary_artifact(
            prefix,
            text,
            span,
            chunk_start,
            duration,
            chunk_seconds,
        ):
            continue
        results.append(
            Evidence(
                id=f"{prefix}.{index}",
                source=source,
                kind=EvidenceType.INFERRED,
                text=text,
                confidence=confidence(item.get("confidence")),
                span=span,
            )
        )
    return results


def parse_chunk(
    payload: dict[str, Any],
    index: int,
    chunk_start: float,
    chunk_end: float,
    *,
    source: str,
    chunk_seconds: float,
    placeholder_values: Collection[str],
    label_aliases: Mapping[str, str],
) -> dict[str, Any]:
    duration = chunk_end - chunk_start
    lyrics: list[LyricsSegment] = []
    raw_lyrics = payload.get("lyrics") or []
    if isinstance(raw_lyrics, str):
        raw_lyrics = [{"text": raw_lyrics}]
    if isinstance(raw_lyrics, list):
        for item in raw_lyrics[:20]:
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not meaningful(text, placeholder_values):
                continue
            span = offset_span(item, chunk_start, duration)
            lines = [
                line.strip()
                for line in text.splitlines()
                if meaningful(line.strip(), placeholder_values)
            ]
            line_duration = (
                (span.end_s - span.start_s) / max(1, len(lines))
                if span is not None
                else None
            )
            for line_index, line in enumerate(lines):
                line_span = span
                if len(lines) > 1 and span is not None and line_duration is not None:
                    line_span = TimeSpan(
                        start_s=span.start_s + line_index * line_duration,
                        end_s=span.start_s + (line_index + 1) * line_duration,
                    )
                lyrics.append(
                    LyricsSegment(
                        text=line,
                        span=line_span,
                        language=item.get("language") or item.get("lang"),
                        confidence=confidence(item.get("confidence")),
                    )
                )

    sound_events = evidence_items(
        payload.get("sound_events"),
        f"omni.chunk.{index}.sound",
        chunk_start,
        duration,
        source=source,
        chunk_seconds=chunk_seconds,
        placeholder_values=placeholder_values,
    )
    emotions = evidence_items(
        payload.get("emotion_timeline"),
        f"omni.chunk.{index}.emotion",
        chunk_start,
        duration,
        source=source,
        chunk_seconds=chunk_seconds,
        placeholder_values=placeholder_values,
    )
    return {
        "lyrics": lyrics,
        "instruments": strings(
            payload.get("instruments"),
            12,
            placeholder_values=placeholder_values,
            label_aliases=label_aliases,
        ),
        "vocals_detected": (
            payload.get("vocals_detected")
            if isinstance(payload.get("vocals_detected"), bool)
            else None
        ),
        "vocal_confidence": confidence(payload.get("vocal_confidence")),
        "sound_events": sound_events,
        "emotions": emotions,
        "themes": strings(
            payload.get("themes"),
            6,
            placeholder_values=placeholder_values,
            label_aliases=label_aliases,
        ),
        "narrative": meaningful_text(
            payload.get("narrative"),
            placeholder_values,
        ),
    }


def atmosphere_items(
    value: Any,
    model: str,
    *,
    source: str,
    placeholder_values: Collection[str],
    atmosphere_aliases: Mapping[str, str],
) -> list[Evidence]:
    if not isinstance(value, list):
        return []
    results: list[Evidence] = []
    seen: set[str] = set()
    for index, item in enumerate(value[:4], start=1):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        basis = str(item.get("basis") or "").strip()
        if not meaningful(text, placeholder_values) or not meaningful(
            basis,
            placeholder_values,
        ):
            continue
        canonical = atmosphere_aliases.get(text.casefold(), text.casefold())
        if canonical in seen:
            continue
        seen.add(canonical)
        results.append(
            Evidence(
                id=f"omni.final.atmosphere.{index}",
                source=source,
                kind=EvidenceType.INTERPRETIVE,
                text=text,
                confidence=confidence(item.get("confidence")),
                metadata={"basis": basis, "model": model},
            )
        )
    return results


def deduplicate(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
        if len(output) >= limit:
            break
    return output


def deduplicate_lyrics(
    values: list[LyricsSegment],
    limit: int,
) -> list[LyricsSegment]:
    seen: set[tuple[str, tuple[float | None, float | None]]] = set()
    output: list[LyricsSegment] = []
    for item in values:
        span_key = (
            (round(item.span.start_s, 1), round(item.span.end_s, 1))
            if item.span
            else (None, None)
        )
        key = (" ".join(item.text.casefold().split()), span_key)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
        if len(output) >= limit:
            break
    return sorted(
        output,
        key=lambda item: (
            item.span.start_s if item.span else math.inf,
            item.span.end_s if item.span else math.inf,
        ),
    )


def lyric_units(text: str) -> int:
    cjk = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    latin_words = len(re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?", text))
    return cjk + latin_words


def near_duplicate(first: LyricsSegment, second: LyricsSegment) -> bool:
    if first.span is None or second.span is None:
        return False
    first_text = re.sub(r"\W+", "", first.text.casefold())
    second_text = re.sub(r"\W+", "", second.text.casefold())
    if not first_text or not second_text:
        return False
    close_in_time = second.span.start_s - first.span.end_s <= 1.0
    if not close_in_time:
        return False
    if first_text == second_text:
        return True
    shorter = min(len(first_text), len(second_text))
    return shorter >= 8 and (
        first_text in second_text or second_text in first_text
    )


def filter_lyrics_quality(
    values: list[LyricsSegment],
) -> tuple[list[LyricsSegment], list[str]]:
    ordered = sorted(
        values,
        key=lambda item: (
            item.span.start_s if item.span else math.inf,
            item.span.end_s if item.span else math.inf,
        ),
    )
    kept: list[LyricsSegment] = []
    issues: list[str] = []
    for item in ordered:
        span = item.span
        if span is None:
            kept.append(item)
            issues.append(f"歌词缺少时间戳：{item.text[:24]}")
            continue

        duration = span.end_s - span.start_s
        units = lyric_units(item.text)
        if duration <= 0.05:
            issues.append(f"零长度或过短时间段：{item.text[:24]}")
            continue
        if units >= 6 and units / duration > 9.0:
            issues.append(
                f"文字密度过高（{units} 单位/{duration:.2f} 秒）："
                f"{item.text[:24]}"
            )
            continue

        previous = kept[-1] if kept else None
        if previous and previous.span:
            overlap = previous.span.end_s - span.start_s
            shorter = min(
                previous.span.end_s - previous.span.start_s,
                duration,
            )
            if overlap > 0.5 and overlap / max(shorter, 0.01) > 0.45:
                issues.append(f"歌词时间明显重叠：{item.text[:24]}")
                continue
            if near_duplicate(previous, item):
                issues.append(f"近邻歌词重复：{item.text[:24]}")
                continue
        kept.append(item)
    return kept, issues


def deduplicate_evidence(
    values: list[Evidence],
    limit: int,
) -> list[Evidence]:
    seen: set[tuple[str, tuple[float | None, float | None]]] = set()
    output: list[Evidence] = []
    for item in values:
        span_key = (
            (round(item.span.start_s, 1), round(item.span.end_s, 1))
            if item.span
            else (None, None)
        )
        key = (" ".join(item.text.casefold().split()), span_key)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
        if len(output) >= limit:
            break
    return output
