from music_insight.schemas import AnalysisResult


def render_markdown_report(result: AnalysisResult) -> str:
    lyrics = "\n".join(
        f"- {_span(segment.span)}{segment.text}" for segment in result.lyrics
    )
    instruments = ", ".join(result.instruments) if result.instruments else "待识别"
    themes = ", ".join(result.themes) if result.themes else "待提取"
    sound_events = "\n".join(
        f"- {_span(item.span)}{item.text}" for item in result.sound_events
    )
    emotions = "\n".join(
        f"- {_span(item.span)}{item.text}" for item in result.emotion_timeline
    )
    inferred_atmosphere = "\n".join(
        (
            f"- {item.text}"
            + (
                f"（可信度 {item.confidence:.0%}"
                if item.confidence is not None
                else "（可信度未提供"
            )
            + f"；依据：{item.metadata.get('basis', '未提供')}）"
        )
        for item in result.inferred_atmosphere
    )
    metrics = result.technical_metrics

    return "\n".join(
        [
            "# 音乐分析报告",
            "",
            "## 总览",
            result.summary,
            "",
            "## 歌词",
            lyrics or "待识别",
            "",
            "## 编曲与声景",
            f"乐器：{instruments}",
            "",
            "### 声音事件",
            sound_events or "待识别",
            "",
            "## 情绪时间线",
            emotions or "待分析",
            "",
            "## 推断氛围（非直接听觉证据）",
            inferred_atmosphere or "现有证据不足，未推断",
            "",
            "## 技术指标",
            (
                f"- BPM：{metrics.bpm}（可信度 {metrics.bpm_confidence:.0%}）"
                + (
                    "；倍频候选 "
                    + " / ".join(str(item) for item in metrics.bpm_candidates[1:])
                    if metrics.bpm_ambiguous and len(metrics.bpm_candidates) > 1
                    else ""
                )
                if metrics.bpm is not None and metrics.bpm_confidence is not None
                else f"- BPM：{metrics.bpm if metrics.bpm is not None else '待计算'}"
            ),
            (
                f"- 调性：{metrics.key}（可信度 {metrics.key_confidence:.0%}）"
                if metrics.key and metrics.key_confidence is not None
                else f"- 调性：{metrics.key or '待计算'}"
            ),
            "",
            "## 主题",
            themes,
        ]
    )


def _span(span) -> str:
    if span is None:
        return ""
    return f"[{span.start_s:.1f}–{span.end_s:.1f}s] "
