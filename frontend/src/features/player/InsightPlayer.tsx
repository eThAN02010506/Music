import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, API_BASE, isAbortError } from "../../api";
import { seconds } from "../../format";
import { useI18n } from "../../i18n";
import type {
  HistoryWaveform,
  SectionMarker,
  VocalPresence,
} from "../../types";
import { rangeAround } from "./playerController";
import { usePlayer, usePlayerSnapshot } from "./PlayerContext";
import { WaveformView } from "./WaveformView";
import { loadWaveformOnce } from "./waveformRequest";
import { StemMixer } from "./StemMixer";

const EMPTY_SECTIONS: SectionMarker[] = [];

export function InsightPlayer({
  audioUrl,
  title,
  historyId,
  vocalPresence,
  sections = EMPTY_SECTIONS,
}: {
  audioUrl: string;
  title: string;
  historyId: string | null;
  vocalPresence: VocalPresence;
  sections?: SectionMarker[];
}) {
  const { t } = useI18n();
  const audioRef = useRef<HTMLAudioElement>(null);
  const [media, setMedia] = useState<HTMLAudioElement | null>(null);
  const [waveform, setWaveform] = useState<HistoryWaveform | null>(null);
  const [waveformError, setWaveformError] = useState("");
  const player = usePlayer();
  const snapshot = usePlayerSnapshot();
  const waveformSections = useMemo(
    () => sections.map((section) => ({
      id: section.id,
      label: section.label,
      ...section.span,
    })),
    [sections],
  );
  const setAudioElement = useCallback((node: HTMLAudioElement | null) => {
    audioRef.current = node;
    setMedia((current) => current === node ? current : node);
  }, []);

  useEffect(() => {
    const media = audioRef.current;
    if (!media) return;
    return player.attach(media);
  }, [audioUrl, player]);

  useEffect(() => {
    setWaveform(null);
    setWaveformError("");
    if (!historyId) return;
    let active = true;
    void loadWaveformOnce(
      historyId,
      () => api.historyWaveform(historyId),
    )
      .then((next) => {
        if (active) setWaveform(next);
      })
      .catch((cause: unknown) => {
        if (!active || isAbortError(cause)) return;
        setWaveformError(
          cause instanceof Error ? cause.message : "波形生成失败",
        );
      });
    return () => {
      active = false;
    };
  }, [historyId]);

  const setSelection = (field: "start_s" | "end_s", value: number) => {
    const current = snapshot.selectedRange
      || rangeAround(snapshot.currentTime, snapshot.duration);
    player.setRange("selection", { ...current, [field]: value });
  };

  return (
    <div className="insight-player">
      <audio
        aria-label={`${title} ${t("播放器")}`}
        crossOrigin={audioUrl.startsWith(API_BASE) ? "use-credentials" : undefined}
        ref={setAudioElement}
        src={audioUrl}
        controls
        preload="metadata"
      />
      {historyId && (
        <StemMixer
          historyId={historyId}
          vocalPresence={vocalPresence}
        />
      )}
      {media && waveform && (
        <WaveformView
          media={media}
          waveform={waveform}
          sections={waveformSections}
        />
      )}
      {historyId && !waveform && !waveformError && (
        <div className="waveform-loading" role="status">
          {t("正在准备可框选波形…")}
        </div>
      )}
      {waveformError && (
        <p className="waveform-error" role="alert">
          {t("波形暂不可用，仍可使用下方时间输入选区。")}
        </p>
      )}
      <div className="player-readout" aria-live="off">
        <strong>{seconds(snapshot.currentTime)}</strong>
        <span>/ {seconds(snapshot.duration)}</span>
        {snapshot.activePlayback && (
          <button type="button" onClick={() => player.clearPlayback()}>
            {t("退出")}
            {snapshot.activePlayback.mode === "once"
              ? t("选区播放")
              : snapshot.activePlayback.mode === "loop"
                ? t("选区循环")
                : snapshot.activePlayback.phase === "a"
                  ? ` A→B (${t("正在播放 A")})`
                  : ` A→B (${t("正在播放 B")})`}
          </button>
        )}
      </div>
      <div className="selection-controls">
        <button
          type="button"
          onClick={() => player.setRange(
            "selection",
            rangeAround(snapshot.currentTime, snapshot.duration),
          )}
        >
          {t("选取当前 15 秒")}
        </button>
        <label>
          {t("开始")}
          <input
            type="number"
            min="0"
            max={snapshot.duration || undefined}
            step="0.1"
            value={snapshot.selectedRange?.start_s ?? ""}
            placeholder="0.0"
            onChange={(event) => setSelection("start_s", Number(event.target.value))}
          />
        </label>
        <label>
          {t("结束")}
          <input
            type="number"
            min="0"
            max={snapshot.duration || undefined}
            step="0.1"
            value={snapshot.selectedRange?.end_s ?? ""}
            placeholder="15.0"
            onChange={(event) => setSelection("end_s", Number(event.target.value))}
          />
        </label>
        {snapshot.selectedRange && (
          <>
            <button
              type="button"
              onClick={() => player.playRange(snapshot.selectedRange!)}
            >
              {t("播放选区")}
            </button>
            <button
              type="button"
              onClick={() => player.playRange(snapshot.selectedRange!, true)}
            >
              {t("循环选区")}
            </button>
            <button
              type="button"
              onClick={() => player.setRange("a", snapshot.selectedRange)}
            >
              {t("设为 A")}
            </button>
            <button
              type="button"
              onClick={() => player.setRange("b", snapshot.selectedRange)}
            >
              {t("设为 B")}
            </button>
          </>
        )}
      </div>
      {(snapshot.rangeA || snapshot.rangeB) && (
        <div className="ab-ranges">
          <span>
            A {snapshot.rangeA
              ? `${seconds(snapshot.rangeA.start_s)}–${seconds(snapshot.rangeA.end_s)}`
              : t("未设置")}
          </span>
          <span>
            B {snapshot.rangeB
              ? `${seconds(snapshot.rangeB.start_s)}–${seconds(snapshot.rangeB.end_s)}`
              : t("未设置")}
          </span>
          {snapshot.rangeA && (
            <button type="button" onClick={() => player.playRange(snapshot.rangeA!)}>
              {t("播放 A")}
            </button>
          )}
          {snapshot.rangeB && (
            <button type="button" onClick={() => player.playRange(snapshot.rangeB!)}>
              {t("播放 B")}
            </button>
          )}
          {snapshot.rangeA && snapshot.rangeB && (
            <button
              type="button"
              onClick={() => player.playAB(snapshot.rangeA!, snapshot.rangeB!)}
            >
              {t("连续播放 A→B")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
