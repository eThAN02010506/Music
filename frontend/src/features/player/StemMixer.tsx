import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, API_BASE, isAbortError } from "../../api";
import { useI18n } from "../../i18n";
import type {
  StemName,
  StemStatus,
  StemTrack,
  VocalPresence,
} from "../../types";
import { usePlayer } from "./PlayerContext";


const STEM_POLL_MS = 2_000;

export function StemMixer({
  historyId,
  vocalPresence,
}: {
  historyId: string;
  vocalPresence: VocalPresence;
}) {
  const { t } = useI18n();
  const player = usePlayer();
  const [status, setStatus] = useState<StemStatus | null>(null);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState<Set<StemName>>(new Set());
  const [failed, setFailed] = useState<Set<StemName>>(new Set());
  const [mixEnabled, setMixEnabled] = useState(false);
  const [muted, setMuted] = useState<Set<StemName>>(new Set());
  const [solo, setSolo] = useState<StemName | null>(null);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const activeRef = useRef(true);
  const historyIdRef = useRef(historyId);
  const loadRef = useRef<() => void>(() => {});

  const clearPoll = useCallback(() => {
    if (pollRef.current !== null) {
      clearTimeout(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const schedulePoll = useCallback(() => {
    if (pollRef.current !== null) return;
    pollRef.current = setTimeout(() => {
      pollRef.current = null;
      loadRef.current();
    }, STEM_POLL_MS);
  }, []);

  const load = useCallback(async () => {
    const controller = controllerRef.current;
    if (!controller) return;
    try {
      const next = await api.historyStems(historyId, controller.signal);
      if (!activeRef.current) return;
      setStatus(next);
      setError("");
      if (next.status === "processing") {
        schedulePoll();
      }
    } catch (cause) {
      if (!activeRef.current || isAbortError(cause)) return;
      setError(cause instanceof Error ? cause.message : "无法读取分轨状态");
    }
  }, [historyId, schedulePoll]);

  useEffect(() => {
    loadRef.current = () => { void load(); };
  }, [load]);

  useEffect(() => {
    const controller = new AbortController();
    controllerRef.current = controller;
    activeRef.current = true;
    historyIdRef.current = historyId;
    setStatus(null);
    setLoaded(new Set());
    setFailed(new Set());
    setMuted(new Set());
    setSolo(null);
    setMixEnabled(false);
    player.setStemMixActive(false);
    void load();
    return () => {
      activeRef.current = false;
      controller.abort();
      clearPoll();
      player.setStemMixActive(false);
    };
  }, [clearPoll, historyId, load, player]);

  const tracks = useMemo(
    () => (status?.stems ?? []).map((track) => ({
      ...track,
      audio_url: `${API_BASE}${track.audio_url}`,
    })),
    [status?.stems],
  );
  const allLoaded = tracks.length === 4 && tracks.every(
    (track) => loaded.has(track.name),
  );

  const generate = async () => {
    if (generating) return;
    clearPoll();
    setGenerating(true);
    setError("");
    const controller = controllerRef.current;
    const requestedId = historyId;
    try {
      const next = await api.generateHistoryStems(
        requestedId,
        controller?.signal,
      );
      // A stale POST for a previous historyId must not overwrite the current
      // component's status after a history switch.
      if (!activeRef.current || requestedId !== historyIdRef.current) return;
      setStatus(next);
      setLoaded(new Set());
      setFailed(new Set());
      if (next.status === "processing") {
        schedulePoll();
      }
    } catch (cause) {
      if (
        activeRef.current
        && requestedId === historyIdRef.current
        && !isAbortError(cause)
      ) {
        setError(cause instanceof Error ? cause.message : "分轨生成失败");
      }
    } finally {
      if (
        activeRef.current
        && requestedId === historyIdRef.current
      ) {
        setGenerating(false);
      }
    }
  };

  const setEnabled = (enabled: boolean) => {
    setMixEnabled(enabled);
    player.setStemMixActive(enabled);
  };

  if (!status && !error) {
    return <div className="stem-mixer-status" role="status">{t("正在检查分轨缓存…")}</div>;
  }
  if (status?.status === "unavailable") {
    return (
      <section className="stem-mixer compact">
        <strong>{t("分轨独听暂不可用")}</strong>
        <span>{t(status.detail || "服务端没有可用的分轨后端。")}</span>
      </section>
    );
  }
  if (status?.status === "processing") {
    return (
      <section className="stem-mixer compact" role="status">
        <strong>{t("正在生成四个分轨")}</strong>
        <span>{t("首次运行可能需要下载模型；完成后会自动刷新。")}</span>
      </section>
    );
  }
  if (status?.status !== "ready") {
    return (
      <section className="stem-mixer compact">
        <div>
          <strong>{t("分轨独听 / 静音")}</strong>
          <span>{t("生成可单独控制的人声、鼓、低音和其他乐器轨。")}</span>
        </div>
        <button
          type="button"
          className="stem-generate"
          disabled={generating}
          onClick={() => void generate()}
        >
          {generating ? t("正在分离，可能需要数分钟…") : t("生成四轨")}
        </button>
        {error && <p role="alert">{t(error)}</p>}
      </section>
    );
  }

  return (
    <section className={`stem-mixer ${mixEnabled ? "active" : ""}`}>
      <header>
        <div>
          <strong>{t("分轨独听 / 静音")}</strong>
          <span>{status.model} · {t("主播放器继续控制时间与 A/B 循环")}</span>
        </div>
        <button
          type="button"
          className="stem-mix-toggle"
          disabled={!mixEnabled && (!allLoaded || failed.size > 0)}
          onClick={() => setEnabled(!mixEnabled)}
        >
          {mixEnabled
            ? t("还原原曲")
            : allLoaded
              ? t("启用分轨混音")
              : t("正在载入四轨…")}
        </button>
      </header>
      {vocalPresence.status === "instrumental" && (
        <div className="instrumental-stem-note">
          <strong>{t("纯器乐提示")}</strong>
          <span>
            {t("古典音乐通常主要落在“其他乐器”和“低音”；“人声”轨若有声音，更可能是串音或分离伪影，不代表作品包含歌唱。")}
          </span>
        </div>
      )}
      <div className="stem-track-controls">
        {tracks.map((track) => {
          const isSolo = solo === track.name;
          const isMuted = muted.has(track.name);
          return (
            <article key={track.name}>
              <div>
                <strong>{t(track.label)}</strong>
                <small>{track.name}</small>
              </div>
              <button
                type="button"
                className={isSolo ? "active" : ""}
                aria-pressed={isSolo}
                onClick={() => setSolo(isSolo ? null : track.name)}
              >
                {t("独听")}
              </button>
              <button
                type="button"
                className={isMuted ? "active danger" : ""}
                aria-pressed={isMuted}
                onClick={() => setMuted((current) => toggled(current, track.name))}
              >
                {t("静音")}
              </button>
            </article>
          );
        })}
      </div>
      <div className="stem-audio-elements" aria-hidden="true">
        {tracks.map((track) => (
          <StemAudio
            key={track.name}
            track={track}
            enabled={mixEnabled}
            muted={solo !== null ? solo !== track.name : muted.has(track.name)}
            onLoaded={() => setLoaded((current) => added(current, track.name))}
            onFailed={() => {
              setFailed((current) => added(current, track.name));
              setEnabled(false);
              setError(t("{{track}}分轨无法载入。", { track: t(track.label) }));
            }}
          />
        ))}
      </div>
      {error && <p role="alert">{t(error)}</p>}
      <small className="stem-disclaimer">
        {t("分轨是模型估计结果，复杂混音中可能出现串音或少量伪影。")}
      </small>
    </section>
  );
}

function StemAudio({
  track,
  enabled,
  muted,
  onLoaded,
  onFailed,
}: {
  track: StemTrack;
  enabled: boolean;
  muted: boolean;
  onLoaded: () => void;
  onFailed: () => void;
}) {
  const ref = useRef<HTMLAudioElement>(null);
  const player = usePlayer();

  useEffect(() => {
    const media = ref.current;
    if (!media || !enabled) return;
    return player.attachFollower(media);
  }, [enabled, player, track.audio_url]);

  useEffect(() => {
    if (ref.current) ref.current.muted = muted;
  }, [muted]);

  return (
    <audio
      ref={ref}
      src={track.audio_url}
      crossOrigin="use-credentials"
      preload="auto"
      muted={muted}
      onCanPlay={onLoaded}
      onError={onFailed}
    />
  );
}

function added<T>(current: Set<T>, value: T): Set<T> {
  const next = new Set(current);
  next.add(value);
  return next;
}

function toggled<T>(current: Set<T>, value: T): Set<T> {
  const next = new Set(current);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}
