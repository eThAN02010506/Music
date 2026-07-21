from __future__ import annotations

import hashlib
from typing import Any

import httpx
import streamlit as st


DEFAULT_API_URL = "http://127.0.0.1:8000"


def post_audio(
    api_url: str,
    file_name: str,
    content_type: str,
    data: bytes,
    language: str | None = None,
) -> dict[str, Any]:
    endpoint = f"{api_url.rstrip('/')}/analyze"
    files = {"file": (file_name, data, content_type)}

    timeout = httpx.Timeout(3600.0, connect=10.0)
    with httpx.Client(timeout=timeout) as client:
        form = {"language": language} if language else None
        response = client.post(endpoint, files=files, data=form)
        response.raise_for_status()
        return response.json()


def render_evidence(evidence: list[dict[str, Any]]) -> None:
    if not evidence:
        st.info("暂无 evidence。")
        return

    rows = [
        {
            "id": item.get("id"),
            "source": item.get("source"),
            "kind": item.get("kind"),
            "confidence": item.get("confidence"),
            "start_s": (item.get("span") or {}).get("start_s"),
            "end_s": (item.get("span") or {}).get("end_s"),
            "text": item.get("text"),
            "basis": (item.get("metadata") or {}).get("basis"),
        }
        for item in evidence
    ]
    st.dataframe(rows, width="stretch", hide_index=True)


def main() -> None:
    st.set_page_config(page_title="Music Insight", page_icon="🎵", layout="wide")

    st.title("Music Insight")
    st.caption("上传音频，调用后端分析歌词、声景、情绪、主题和技术指标。")

    with st.sidebar:
        st.header("设置")
        api_url = st.text_input("API 地址", value=DEFAULT_API_URL)
        uploaded = st.file_uploader(
            "上传音频",
            type=["wav", "mp3", "m4a", "flac", "ogg", "aac"],
            accept_multiple_files=False,
        )
        language_label = st.selectbox(
            "歌词语言",
            ["自动检测", "中文", "英文"],
            index=0,
            help="演唱歌词建议明确选择语言，可降低跨语言误识别。",
        )
        language_codes = {"自动检测": None, "中文": "zh", "英文": "en"}
        language = language_codes[language_label]
        st.caption("统一模型：Qwen3-Omni · 192.168.1.97:8004")
        st.caption("策略：30 秒音频分块 + 同模型最终融合 + 本地 DSP")
        analyze = st.button("开始分析", type="primary", disabled=uploaded is None)

        st.divider()
        st.markdown("接口入口")
        st.code(f"{api_url.rstrip('/')}/analyze", language="text")

    if uploaded is None:
        st.info("请先在左侧上传音频文件。")
        return

    data = uploaded.getvalue()
    content_type = uploaded.type or "audio/wav"
    upload_key = hashlib.sha256(data).hexdigest()
    if st.session_state.get("upload_key") != upload_key:
        st.session_state["upload_key"] = upload_key
        st.session_state.pop("analysis_result", None)

    st.subheader("音频")
    st.audio(data, format=content_type)
    st.caption(f"{uploaded.name} · {len(data) / 1024 / 1024:.2f} MB · {content_type}")

    if analyze:
        try:
            with st.spinner(
                "分析中：8004 分块提取歌词、声景与情绪 → 同模型最终融合…"
            ):
                result = post_audio(
                    api_url, uploaded.name, content_type, data, language
                )
                st.session_state["analysis_result"] = result
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            st.error(f"后端返回错误：HTTP {exc.response.status_code}")
            st.code(detail, language="json")
            return
        except httpx.RequestError as exc:
            st.error(f"无法连接后端：{exc}")
            return

    result = st.session_state.get("analysis_result")
    if not result:
        return

    warnings = result.get("warnings") or []
    for warning in warnings:
        st.warning(warning)

    st.subheader("总览")
    st.write(result.get("summary") or "暂无摘要。")

    tab_overview, tab_lyrics, tab_scene, tab_metrics, tab_json = st.tabs(
        ["概览", "歌词", "声景与情绪", "技术指标", "原始 JSON"]
    )

    with tab_overview:
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("#### 乐器")
            instruments = result.get("instruments") or []
            st.write(", ".join(instruments) if instruments else "待识别")
        with col2:
            st.markdown("#### 主题")
            themes = result.get("themes") or []
            st.write(", ".join(themes) if themes else "待提取")

        st.markdown("#### Evidence")
        render_evidence(result.get("evidence") or [])

    with tab_lyrics:
        lyrics = result.get("lyrics") or []
        if lyrics:
            for index, segment in enumerate(lyrics, start=1):
                confidence = segment.get("confidence")
                suffix = f" · confidence={confidence:.2f}" if isinstance(confidence, float) else ""
                span = segment.get("span") or {}
                if isinstance(span.get("start_s"), (int, float)) and isinstance(
                    span.get("end_s"), (int, float)
                ):
                    suffix += (
                        f" · {span['start_s']:.1f}–{span['end_s']:.1f}s"
                    )
                st.markdown(f"**{index}.** {segment.get('text', '')}{suffix}")
        else:
            st.info("暂无歌词片段。")

    with tab_scene:
        st.markdown("#### 声音事件")
        render_evidence(result.get("sound_events") or [])

        st.markdown("#### 情绪时间线")
        render_evidence(result.get("emotion_timeline") or [])

        st.markdown("#### 推断氛围（非直接听觉证据）")
        inferred_atmosphere = result.get("inferred_atmosphere") or []
        if inferred_atmosphere:
            render_evidence(inferred_atmosphere)
        else:
            st.info("现有证据不足，未推断整体氛围。")

    with tab_metrics:
        metrics = result.get("technical_metrics") or {}
        bpm = metrics.get("bpm")
        bpm_confidence = metrics.get("bpm_confidence")
        key = metrics.get("key")
        key_confidence = metrics.get("key_confidence")
        bpm_display = (
            f"{bpm} ({bpm_confidence:.0%})"
            if bpm is not None and bpm_confidence is not None
            else bpm or "待计算"
        )
        key_display = (
            f"{key} ({key_confidence:.0%})"
            if key and key_confidence is not None
            else key or "待计算"
        )
        col1, col2 = st.columns(2)
        col1.metric("BPM", bpm_display)
        col2.metric("Key", key_display)
        st.markdown("#### DSP Evidence")
        render_evidence(metrics.get("evidence") or [])

    with tab_json:
        st.json(result)


if __name__ == "__main__":
    main()
