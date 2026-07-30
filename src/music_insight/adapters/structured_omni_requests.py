from __future__ import annotations

import base64
import json
from typing import Any

from music_insight.schemas import DspResult, Evidence, LyricsSegment


def _evenly_sample(values: list[Any], limit: int) -> list[Any]:
    if len(values) <= limit:
        return values
    indices = {
        round(index * (len(values) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [values[index] for index in sorted(indices)]


def _audio_content(audio_bytes: bytes, instruction: str) -> list[dict[str, Any]]:
    encoded = base64.b64encode(audio_bytes).decode("ascii")
    return [
        {"type": "text", "text": instruction},
        {
            "type": "input_audio",
            "input_audio": {"data": encoded, "format": "wav"},
        },
    ]


def chunk_analysis_request(
    *,
    model: str,
    audio_bytes: bytes,
    duration_s: float,
    language_hint: str | None,
    response_format: dict[str, Any],
) -> dict[str, Any]:
    language_instruction = {
        "zh": "歌词若存在，逐字输出中文原文，不翻译。",
        "en": "If lyrics exist, transcribe the original English verbatim.",
    }.get(language_hint, "歌词若存在，保留原语言，不翻译。")
    instruction = (
        f"分析这段 {duration_s:.2f} 秒的音乐音频。{language_instruction}"
        "不要补写听不清的歌词。所有时间戳使用当前分块内的相对秒数，"
        f"范围必须在 0 到 {duration_s:.2f} 之间。每类时间事件最多 6 项。"
        "返回 JSON 对象，且只能包含这些字段：lyrics、instruments、"
        "vocals_detected、vocal_confidence、sound_events、"
        "emotion_timeline、themes、narrative。"
        "vocals_detected 只判断实际歌唱、说话或合唱：明确听见时为 true，"
        "明确整段无人声时为 false；乐器音色像人声、过弱或不确定时必须为 null。"
        "vocal_confidence 是对该判断的置信度；判断为 null 时也必须为 null。"
        "人声只包括可听见的人类歌唱、合唱、哼唱、说话、念白、吟诵或说唱；"
        "不要把弦乐、管乐、合成器或其他近似人声的乐器音色判为人声。"
        "sound_events 不是只记录环境杂音；应记录这段中最值得复听的音乐听觉"
        "事实，例如声部或乐器进入退出、织体增减、旋律走向、节奏重音、"
        "和声色彩、音色、力度、空间或段落边界的变化。每项 dimension 必须是"
        " melody、harmony、rhythm、timbre、dynamics、instrumentation、"
        "space、structure、other 之一。只写能直接从声音中观察的事实，"
        "不要在 sound_events 中解释情绪或创作意图。"
        "narrative 用一至三句描述这段实际听见的配器、织体、节奏、力度或"
        "音色如何发展；即使没有人声，也应在有可辨声音时给出局部声音描述。"
        "themes 只写有声音或歌词依据的局部表达线索，无法确认则留空。"
        "lyrics、sound_events、emotion_timeline 的每项必须包含 text、start_s、"
        "end_s、confidence；lyrics 还包含 language。除 text 外无法确认的值"
        "必须写 null，不得省略字段。"
        "每个 lyrics 项只包含一行连续歌词，不得换行，不得补全分块之外的内容。"
        "只填入实际听到的内容，不要复述字段说明；无法确认时使用空数组或空字符串。"
    )
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是严谨的音乐听觉分析器。只报告音频支持的内容，"
                    "区分听见与推断；只输出 JSON，不使用 Markdown。"
                ),
            },
            {
                "role": "user",
                "content": _audio_content(audio_bytes, instruction),
            },
        ],
        "response_format": response_format,
        "temperature": 0,
        "max_tokens": 1800,
    }


def missing_recovery_request(
    *,
    model: str,
    audio_bytes: bytes,
    duration_s: float,
    language_hint: str | None,
    missing: list[str],
    response_format: dict[str, Any],
) -> dict[str, Any]:
    language_instruction = {
        "zh": "只转写实际听见的中文原词，不翻译。",
        "en": "Transcribe only the English words actually heard; do not translate.",
    }.get(language_hint, "保留歌词原语言，不翻译。")
    vocal_fields_requested = any(
        field in {"vocals_detected", "vocal_confidence"} for field in missing
    )
    vocal_instruction = (
        "同时只判断当前整段是否存在真实人类声音：歌唱、合唱、哼唱、说话、"
        "念白、吟诵或说唱算人声；弦乐、管乐、合成器等近似人声的音色不算。"
        "明确听到人声时 vocals_detected=true，明确整段无人声时为 false，"
        "不能确定时为 null；vocal_confidence 只表示这一判断的把握，"
        "判断为 null 时也必须为 null。"
        if vocal_fields_requested
        else ""
    )
    instruction = (
        f"重新仔细听这段 {duration_s:.2f} 秒音频，只补充这些缺失字段："
        f"{', '.join(missing)}。{language_instruction}"
        f"{vocal_instruction}"
        "歌词听不清时必须留空，不得根据语境补写。情绪必须依据人声的音高、力度、"
        "音色或演唱方式；没有可确认人声时留空。时间戳必须位于音频时长内。"
        "返回只含所请字段的 JSON 对象。lyrics 与 emotion_timeline 每项为 text、"
        "start_s、end_s、confidence；lyrics 项还包含 language。"
        "无法确认的非 text 值必须写 null。"
        "每个 lyrics 项只包含一行连续歌词。不要复述字段说明。"
    )
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是保守的人声存在性、歌声转写与人声情绪分析器。"
                    "宁可留空也不猜测；只输出 JSON。"
                ),
            },
            {
                "role": "user",
                "content": _audio_content(audio_bytes, instruction),
            },
        ],
        "response_format": response_format,
        "temperature": 0,
        "max_tokens": 1000,
    }


def lyrics_quality_recovery_request(
    *,
    model: str,
    audio_bytes: bytes,
    duration_s: float,
    language_hint: str | None,
    issues: list[str],
    response_format: dict[str, Any],
) -> dict[str, Any]:
    language_instruction = {
        "zh": "只转写实际听见的中文原词，不翻译。",
        "en": "Transcribe only the English words actually heard; do not translate.",
    }.get(language_hint, "保留歌词原语言，不翻译。")
    instruction = (
        f"重新听这段 {duration_s:.2f} 秒音频，修正歌词时间轴并确认人声状态。"
        f"上次结果存在这些质量问题：{'；'.join(issues[:6])}。"
        f"{language_instruction}"
        "返回且只返回 lyrics、vocals_detected、vocal_confidence。"
        "人声只包括可听见的人类歌唱、合唱、哼唱、说话、念白、吟诵或说唱；"
        "弦乐、管乐、合成器等近似人声的乐器音色不算。明确听到人声时"
        " vocals_detected=true，明确整段无人声时为 false，不能确定时为 null；"
        "vocal_confidence 只表示这一判断的把握，判断为 null 时也必须为 null。"
        "lyrics 每项只含一行连续歌词及 start_s、end_s、"
        "confidence、language。时间必须是当前音频内的相对秒数，"
        "无法确认的时间、可信度或语言写 null，"
        "同一时间不能塞入过多文字，不得重叠、重复或补写听不清的内容。"
        "无法确认时返回空数组。只输出 JSON。"
    )
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是保守的人声存在性与歌词时间轴校对器。"
                    "宁可省略也不猜测，只输出严格 JSON。"
                ),
            },
            {
                "role": "user",
                "content": _audio_content(audio_bytes, instruction),
            },
        ],
        "response_format": response_format,
        "temperature": 0,
        "max_tokens": 1200,
    }


def synthesis_request(
    *,
    model: str,
    lyrics: list[LyricsSegment],
    instruments: list[str],
    sound_events: list[Evidence],
    emotions: list[Evidence],
    chunk_themes: list[str],
    chunk_narratives: list[str],
    dsp: DspResult,
    response_format: dict[str, Any],
) -> dict[str, Any]:
    compact = {
        "lyrics": [
            item.model_dump(mode="json")
            for item in _evenly_sample(lyrics, 60)
        ],
        "instruments": instruments,
        "sound_events": [
            item.model_dump(mode="json")
            for item in _evenly_sample(sound_events, 30)
        ],
        "emotion_timeline": [
            item.model_dump(mode="json")
            for item in _evenly_sample(emotions, 30)
        ],
        "chunk_themes": chunk_themes,
        "chunk_descriptions": _evenly_sample(chunk_narratives, 24),
        "dsp": {
            "bpm": dsp.bpm,
            "bpm_confidence": dsp.bpm_confidence,
            "bpm_candidates": dsp.bpm_candidates,
            "bpm_ambiguous": dsp.bpm_ambiguous,
            "key": dsp.key,
            "key_confidence": dsp.key_confidence,
        },
    }
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是音乐证据融合器。只根据提供的证据写结论，"
                    "冲突或不确定时明确说明。只输出 JSON。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "返回只含 themes、narrative 和 inferred_atmosphere 的 JSON 对象。"
                    "themes 填入实际主题，narrative 写四至八句综合分析；"
                    "inferred_atmosphere 是根据歌词、能量、分块描述和已有情绪证据"
                    "推断的整体氛围，最多 4 项；每项包含 text、confidence 和 basis。"
                    "同义或高度重叠的氛围必须合并，不要同时输出近义词。"
                    "它不是直接听觉观测，证据不足时留空，不得伪造时间戳。"
                    "不得仅凭管弦乐、铜管、定音鼓或调性猜测战争、庆典、历史事件、"
                    "创作者心理或创作意图。BPM 可信度低于 0.4 或 bpm_ambiguous"
                    " 为 true 时，只能把速度作为不确定候选，不得用它强化情绪结论。"
                    "没有歌词时使用“音乐”或“作品”，不要默认称为歌曲。"
                    "不要复制这些字段说明。证据："
                    + json.dumps(compact, ensure_ascii=False)
                ),
            },
        ],
        "response_format": response_format,
        "temperature": 0,
        "max_tokens": 1400,
    }
