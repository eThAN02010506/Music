import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, API_BASE, isAbortError } from "../../api";
import type { StemName, StemStatus, StemTrack } from "../../types";
import { usePlayer } from "./PlayerContext";


const STEM_POLL_MS = 2_000;

export function StemMixer({ historyId }: { historyId: string }) {
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

  const clearPoll = useCallback(() => {
    if (pollRef.current !== null) {
      clearTimeout(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const load = async () => {
      try {
        const next = await api.historyStems(historyId, controller.signal);
        if (!active) return;
        setStatus(next);
        setError("");
        if (next.status === "processing") {
          pollRef.current = setTimeout(() => {
            pollRef.current = null;
            void load();
          }, STEM_POLL_MS);
        }
      } catch (cause) {
        if (!active || isAbortError(cause)) return;
        setError(cause instanceof Error ? cause.message : "无法读取分轨状态");
      }
    };
    setStatus(null);
    setLoaded(new Set());
    setFailed(new Set());
    setMuted(new Set());
    setSolo(null);
    setMixEnabled(false);
    player.setStemMixActive(false);
    void load();
    return () => {
      active = false;
      controller.abort();
      clearPoll();
      player.setStemMixActive(false);
    };
  }, [clearPoll, historyId, player]);

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
    try {
      const next = await api.generateHistoryStems(historyId);
      setStatus(next);
      setLoaded(new Set());
      setFailed(new Set());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "分轨生成失败");
    } finally {
      setGenerating(false);
    }
  };

  const setEnabled = (enabled: boolean) => {
    setMixEnabled(enabled);
    player.setStemMixActive(enabled);
  };

  if (!status && !error) {
    return <div className="stem-mixer-status" role="status">正在检查分轨缓存…</div>;
  }
  if (status?.status === "unavailable") {
    return (
      <section className="stem-mixer compact">
        <strong>分轨独听暂不可用</strong>
        <span>{status.detail || "服务端没有可用的分轨后端。"}</span>
      </section>
    );
  }
  if (status?.status === "processing") {
    return (
      <section className="stem-mixer compact" role="status">
        <strong>正在生成四个分轨</strong>
        <span>首次运行可能需要下载模型；完成后会自动刷新。</span>
      </section>
    );
  }
  if (status?.status !== "ready") {
    return (
      <section className="stem-mixer compact">
        <div>
          <strong>分轨独听 / 静音</strong>
          <span>生成可单独控制的人声、鼓、低音和其他乐器轨。</span>
        </div>
        <button
          type="button"
          className="stem-generate"
          disabled={generating}
          onClick={() => void generate()}
        >
          {generating ? "正在分离，可能需要数分钟…" : "生成四轨"}
        </button>
        {error && <p role="alert">{error}</p>}
      </section>
    );
  }

  return (
    <section className={`stem-mixer ${mixEnabled ? "active" : ""}`}>
      <header>
        <div>
          <strong>分轨独听 / 静音</strong>
          <span>{status.model} · 主播放器继续控制时间与 A/B 循环</span>
        </div>
        <button
          type="button"
          className="stem-mix-toggle"
          disabled={!mixEnabled && (!allLoaded || failed.size > 0)}
          onClick={() => setEnabled(!mixEnabled)}
        >
          {mixEnabled
            ? "还原原曲"
            : allLoaded
              ? "启用分轨混音"
              : "正在载入四轨…"}
        </button>
      </header>
      <div className="stem-track-controls">
        {tracks.map((track) => {
          const isSolo = solo === track.name;
          const isMuted = muted.has(track.name);
          return (
            <article key={track.name}>
              <div>
                <strong>{track.label}</strong>
                <small>{track.name}</small>
              </div>
              <button
                type="button"
                className={isSolo ? "active" : ""}
                aria-pressed={isSolo}
                onClick={() => setSolo(isSolo ? null : track.name)}
              >
                独听
              </button>
              <button
                type="button"
                className={isMuted ? "active danger" : ""}
                aria-pressed={isMuted}
                onClick={() => setMuted((current) => toggled(current, track.name))}
              >
                静音
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
              setError(`${track.label}分轨无法载入。`);
            }}
          />
        ))}
      </div>
      {error && <p role="alert">{error}</p>}
      <small className="stem-disclaimer">
        分轨是模型估计结果，复杂混音中可能出现串音或少量伪影。
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
