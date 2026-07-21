import { DragEvent, useEffect, useMemo, useRef, useState } from "react";
import type {
  AnalysisResult,
  Evidence,
  HealthResult,
  JobSnapshot,
  LyricsSegment,
} from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

const stageLabels: Record<string, string> = {
  queued: "等待处理",
  starting: "启动分析",
  preprocessing: "音频预处理",
  dsp: "声学计算",
  audio_analysis: "模型聆听",
  fusion: "证据融合",
  finalizing: "整理报告",
  completed: "分析完成",
  failed: "分析失败",
  cancelled: "已取消",
};

function seconds(value: number | undefined | null) {
  if (value == null || Number.isNaN(value)) return "--:--";
  const minutes = Math.floor(value / 60);
  const rest = Math.floor(value % 60);
  return `${minutes}:${rest.toString().padStart(2, "0")}`;
}

function percent(value: number | null | undefined) {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

function confidenceClass(value: number | null | undefined) {
  if (value == null) return "neutral";
  if (value >= 0.75) return "good";
  if (value >= 0.4) return "medium";
  return "low";
}

function SignalMark() {
  return (
    <div className="signal-mark" aria-hidden="true">
      {[16, 28, 40, 22, 34, 15].map((height, index) => (
        <span key={index} style={{ height }} />
      ))}
    </div>
  );
}

function UploadPanel({
  file,
  language,
  busy,
  onFile,
  onLanguage,
  onAnalyze,
}: {
  file: File | null;
  language: string;
  busy: boolean;
  onFile: (file: File) => void;
  onLanguage: (language: string) => void;
  onAnalyze: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const drop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    const next = event.dataTransfer.files[0];
    if (next?.type.startsWith("audio/")) onFile(next);
  };

  return (
    <section className="upload-card panel">
      <div className="section-kicker">NEW ANALYSIS</div>
      <h1>听见音乐里的证据</h1>
      <p className="lead">上传一段音频，识别歌词、乐器、声音事件与情绪，并结合本地 DSP 形成可核查的分析。</p>

      <div
        className={`drop-zone ${dragging ? "dragging" : ""} ${file ? "has-file" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={drop}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(event) => event.key === "Enter" && inputRef.current?.click()}
      >
        <input
          ref={inputRef}
          type="file"
          accept="audio/*,.wav,.mp3,.flac,.m4a,.ogg,.oga,.webm"
          hidden
          onChange={(event) => event.target.files?.[0] && onFile(event.target.files[0])}
        />
        <div className="upload-icon">↥</div>
        {file ? (
          <>
            <strong>{file.name}</strong>
            <span>{(file.size / 1024 / 1024).toFixed(1)} MB · 点击更换</span>
          </>
        ) : (
          <>
            <strong>拖放音频到这里</strong>
            <span>或点击选择 WAV、MP3、FLAC、M4A、OGG</span>
          </>
        )}
      </div>

      <div className="upload-actions">
        <label>
          <span>歌词语言</span>
          <select value={language} onChange={(event) => onLanguage(event.target.value)} disabled={busy}>
            <option value="auto">自动识别</option>
            <option value="zh">中文</option>
            <option value="en">English</option>
          </select>
        </label>
        <button className="primary-button" disabled={!file || busy} onClick={onAnalyze}>
          {busy ? "分析进行中" : "开始分析"}
          <span>→</span>
        </button>
      </div>
    </section>
  );
}

function ProgressPanel({ job, onCancel }: { job: JobSnapshot; onCancel: () => void }) {
  const running = job.state === "queued" || job.state === "running";
  return (
    <section className="progress-card panel">
      <div className="progress-heading">
        <div>
          <span className={`status-pulse ${job.state}`} />
          <span>{stageLabels[job.stage] || job.stage}</span>
        </div>
        <strong>{Math.round(job.progress * 100)}%</strong>
      </div>
      <div className="progress-track"><span style={{ width: `${job.progress * 100}%` }} /></div>
      <div className="progress-meta">
        <p>{job.error || job.message}</p>
        {running && <button onClick={onCancel}>取消任务</button>}
      </div>
      <div className="stage-row">
        {["音频预处理", "声学计算", "模型聆听", "证据融合"].map((item, index) => (
          <span key={item} className={job.progress >= [0.08, 0.18, 0.36, 0.94][index] ? "active" : ""}>{item}</span>
        ))}
      </div>
    </section>
  );
}

function TagList({ items, empty = "暂无可靠结果" }: { items: string[]; empty?: string }) {
  if (!items.length) return <p className="empty-copy">{empty}</p>;
  return <div className="tag-list">{items.map((item) => <span key={item}>{item}</span>)}</div>;
}

function Confidence({ value }: { value: number | null }) {
  return <span className={`confidence ${confidenceClass(value)}`}>{percent(value)}</span>;
}

function ResultPanel({
  result,
  audioUrl,
  fileName,
}: {
  result: AnalysisResult;
  audioUrl: string;
  fileName: string;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const duration = useMemo(() => {
    const ends = [
      ...result.lyrics.map((item) => item.span?.end_s || 0),
      ...result.emotion_timeline.map((item) => item.span?.end_s || 0),
      ...result.sound_events.map((item) => item.span?.end_s || 0),
    ];
    return Math.max(...ends, 1);
  }, [result]);

  const seek = (value: number | undefined) => {
    if (audioRef.current && value != null) {
      audioRef.current.currentTime = value;
      void audioRef.current.play();
    }
  };

  return (
    <div className="results">
      <section className="result-hero panel">
        <div className="record-art"><SignalMark /></div>
        <div className="track-info">
          <div className="section-kicker">ANALYSIS COMPLETE</div>
          <h2>{result.title || fileName.replace(/\.[^.]+$/, "")}</h2>
          <p>{result.summary}</p>
          <audio ref={audioRef} src={audioUrl} controls preload="metadata" />
        </div>
        <div className="metric-strip">
          <div>
            <span>BPM</span>
            <strong>{result.technical_metrics.bpm?.toFixed(1) || "—"}</strong>
            <small>
              可信度 {percent(result.technical_metrics.bpm_confidence)}
              {result.technical_metrics.bpm_ambiguous && result.technical_metrics.bpm_candidates.length > 1
                ? ` · 倍频候选 ${result.technical_metrics.bpm_candidates.slice(1).join(" / ")}`
                : ""}
            </small>
          </div>
          <div><span>KEY</span><strong>{result.technical_metrics.key || "—"}</strong><small>可信度 {percent(result.technical_metrics.key_confidence)}</small></div>
          <div><span>LYRICS</span><strong>{result.lyrics.length}</strong><small>个片段</small></div>
        </div>
      </section>

      {result.warnings.length > 0 && (
        <section className="warning-box">
          <strong>分析提醒</strong>
          {result.warnings.map((warning) => <p key={warning}>{warning}</p>)}
        </section>
      )}

      <div className="result-grid">
        <section className="panel lyrics-panel">
          <header><div><span className="section-number">01</span><h3>歌词时间轴</h3></div><small>{result.lyrics.length} 个片段</small></header>
          {result.lyrics.length ? (
            <div className="lyrics-list">
              {result.lyrics.map((line: LyricsSegment, index) => (
                <button key={`${line.text}-${index}`} onClick={() => seek(line.span?.start_s)}>
                  <span>{seconds(line.span?.start_s)}</span>
                  <p>{line.text}</p>
                  <Confidence value={line.confidence} />
                </button>
              ))}
            </div>
          ) : <p className="empty-copy">没有确认到可靠歌词</p>}
        </section>

        <div className="side-stack">
          <section className="panel compact-panel">
            <header><div><span className="section-number">02</span><h3>乐器与声源</h3></div></header>
            <TagList items={result.instruments} empty="没有确认到具体乐器" />
            {result.sound_events.length > 0 && (
              <div className="event-list">
                {result.sound_events.map((event) => (
                  <button key={event.id} onClick={() => seek(event.span?.start_s)}>
                    <span>{event.text}</span><small>{seconds(event.span?.start_s)}–{seconds(event.span?.end_s)}</small>
                  </button>
                ))}
              </div>
            )}
          </section>

          <section className="panel compact-panel">
            <header><div><span className="section-number">03</span><h3>主题</h3></div></header>
            <TagList items={result.themes} />
          </section>
        </div>
      </div>

      <section className="panel emotion-panel">
        <header>
          <div><span className="section-number">04</span><h3>直接情绪证据</h3></div>
          <small>来自音色、力度与演唱方式</small>
        </header>
        {result.emotion_timeline.length ? (
          <div className="timeline">
            <div className="timeline-ruler"><span>0:00</span><span>{seconds(duration / 2)}</span><span>{seconds(duration)}</span></div>
            <div className="timeline-track">
              {result.emotion_timeline.map((item: Evidence) => (
                <button
                  key={item.id}
                  style={{
                    left: `${((item.span?.start_s || 0) / duration) * 100}%`,
                    width: `${Math.max((((item.span?.end_s || duration) - (item.span?.start_s || 0)) / duration) * 100, 7)}%`,
                  }}
                  onClick={() => seek(item.span?.start_s)}
                >
                  <span>{item.text}</span><Confidence value={item.confidence} />
                </button>
              ))}
            </div>
          </div>
        ) : <p className="empty-copy">模型没有确认直接情绪；这不等于音乐没有氛围。</p>}
      </section>

      <section className="panel atmosphere-panel">
        <header>
          <div><span className="section-number">05</span><h3>推断氛围</h3></div>
          <small>非直接听觉证据</small>
        </header>
        {result.inferred_atmosphere.length ? (
          <div className="atmosphere-grid">
            {result.inferred_atmosphere.map((item) => (
              <article key={item.id}>
                <div><strong>{item.text}</strong><Confidence value={item.confidence} /></div>
                <p>{String(item.metadata.basis || "由歌词、节奏和声音描述综合推断")}</p>
              </article>
            ))}
          </div>
        ) : <p className="empty-copy">证据不足，未生成推断氛围</p>}
      </section>
    </div>
  );
}

export default function App() {
  const [health, setHealth] = useState<HealthResult | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [audioUrl, setAudioUrl] = useState("");
  const [language, setLanguage] = useState("auto");
  const [job, setJob] = useState<JobSnapshot | null>(null);
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch(`${API_BASE}/health`)
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then(setHealth)
      .catch(() => setHealth(null));
  }, []);

  useEffect(() => {
    if (!job || ["completed", "failed", "cancelled"].includes(job.state)) return;
    const events = new EventSource(`${API_BASE}/jobs/${job.id}/events`);
    let terminal = false;
    events.addEventListener("progress", async (event) => {
      const snapshot = JSON.parse((event as MessageEvent).data) as JobSnapshot;
      setJob(snapshot);
      if (snapshot.state === "completed") {
        terminal = true;
        events.close();
        try {
          const response = await fetch(`${API_BASE}/jobs/${snapshot.id}/result`);
          if (!response.ok) throw new Error("无法读取分析结果");
          setResult(await response.json());
        } catch (cause) {
          setError(cause instanceof Error ? cause.message : "无法读取分析结果");
        }
      }
      if (snapshot.state === "failed" || snapshot.state === "cancelled") {
        terminal = true;
        events.close();
      }
      if (snapshot.state === "failed") setError(snapshot.error || "分析失败");
    });
    events.onerror = () => {
      events.close();
      if (!terminal) setError("进度连接中断，请确认后端仍在运行");
    };
    return () => events.close();
  }, [job?.id]);

  useEffect(() => () => {
    if (audioUrl) URL.revokeObjectURL(audioUrl);
  }, [audioUrl]);

  const chooseFile = (next: File) => {
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    setFile(next);
    setAudioUrl(URL.createObjectURL(next));
    setResult(null);
    setJob(null);
    setError("");
  };

  const analyze = async () => {
    if (!file) return;
    setError("");
    setResult(null);
    const form = new FormData();
    form.append("file", file);
    if (language !== "auto") form.append("language", language);
    try {
      const response = await fetch(`${API_BASE}/jobs`, { method: "POST", body: form });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`后端返回 HTTP ${response.status}: ${detail}`);
      }
      setJob(await response.json());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法创建分析任务");
    }
  };

  const cancel = async () => {
    if (!job) return;
    const response = await fetch(`${API_BASE}/jobs/${job.id}/cancel`, { method: "POST" });
    if (response.ok) setJob(await response.json());
  };

  const busy = job?.state === "queued" || job?.state === "running";

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#top"><SignalMark /><span>Music Insight</span></a>
        <div className="service-status">
          <span className={health ? "online" : "offline"} />
          <div><strong>{health ? "分析服务在线" : "后端未连接"}</strong><small>{health?.model_endpoint || API_BASE}</small></div>
        </div>
      </header>

      <main id="top">
        <UploadPanel
          file={file}
          language={language}
          busy={Boolean(busy)}
          onFile={chooseFile}
          onLanguage={setLanguage}
          onAnalyze={analyze}
        />
        {error && <div className="error-banner"><strong>出现问题</strong><span>{error}</span></div>}
        {job && <ProgressPanel job={job} onCancel={cancel} />}
        {result && file && <ResultPanel result={result} audioUrl={audioUrl} fileName={file.name} />}
      </main>

      <footer><span>Music Insight · 本地优先的音乐证据分析</span><span>FastAPI + React</span></footer>
    </div>
  );
}
