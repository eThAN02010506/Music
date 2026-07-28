import { DragEvent, useEffect, useMemo, useRef, useState } from "react";
import type {
  AnalysisResult,
  Evidence,
  HealthResult,
  HistoryDetail,
  HistoryRevision,
  HistorySummary,
  JobSnapshot,
  LyricsRetryResult,
  LyricsSegment,
  ModelProbeResult,
  SingingScore,
} from "./types";
import { api, ApiError, API_BASE } from "./api";
import { confidenceClass, percent, seconds } from "./format";
import { MODEL_PROFILES, profileForEndpoint } from "./modelProfiles";

const stageLabels: Record<string, string> = {
  queued: "等待处理",
  starting: "启动分析",
  preprocessing: "音频预处理",
  dsp: "声学计算",
  audio_analysis: "模型聆听",
  model_queue: "等待模型",
  model_synthesis: "模型综合",
  fusion: "证据融合",
  finalizing: "整理报告",
  completed: "分析完成",
  failed: "分析失败",
  cancelled: "已取消",
};

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

function ModelSettings({
  modelSource,
  modelEndpoint,
  localModelPath,
  defaultEndpoint,
  localModelRoot,
  localRunnerAvailable,
  busy,
  onModelSource,
  onModelEndpoint,
  onLocalModelPath,
}: {
  modelSource: "network" | "local";
  modelEndpoint: string;
  localModelPath: string;
  defaultEndpoint: string;
  localModelRoot: string;
  localRunnerAvailable: boolean;
  busy: boolean;
  onModelSource: (source: "network" | "local") => void;
  onModelEndpoint: (endpoint: string) => void;
  onLocalModelPath: (path: string) => void;
}) {
  const activeLocation = modelSource === "network"
    ? (modelEndpoint || defaultEndpoint)
    : (localModelPath || localModelRoot);
  const activeProfile = profileForEndpoint(modelEndpoint, defaultEndpoint);
  const [probe, setProbe] = useState<ModelProbeResult | null>(null);
  const [probing, setProbing] = useState(false);
  useEffect(() => setProbe(null), [modelEndpoint, defaultEndpoint]);

  const testModel = async () => {
    setProbing(true);
    try {
      setProbe(await api.probeModel(activeLocation));
    } catch (cause) {
      setProbe({
        endpoint: activeLocation,
        online: false,
        model: null,
        audio_supported: null,
        service: "OpenAI-compatible",
        detail: cause instanceof Error ? cause.message : "模型连接测试失败",
      });
    } finally {
      setProbing(false);
    }
  };
  return (
    <details className="topbar-model-settings">
      <summary aria-label="模型设置">
        <span className="settings-icon" aria-hidden="true">⌘</span>
        <span><strong>模型</strong><small>{modelSource === "local" ? "本地权重" : activeLocation.replace(/^https?:\/\//, "")}</small></span>
        <span className="settings-chevron" aria-hidden="true">⌄</span>
      </summary>
      <div className="model-popover">
        <header>
          <div><strong>模型设置</strong><small>仅影响下一次分析</small></div>
          <span className={`runner-dot ${modelSource === "network" || localRunnerAvailable ? "ready" : "missing"}`} />
        </header>
        <div className="model-source-tabs" role="group" aria-label="模型来源">
          <button disabled={busy} className={modelSource === "network" ? "active" : ""} onClick={() => onModelSource("network")} type="button">模型接口</button>
          <button disabled={busy} className={modelSource === "local" ? "active" : ""} onClick={() => onModelSource("local")} type="button">本地权重</button>
        </div>
        {modelSource === "network" ? (
          <>
            <div className="model-presets" role="group" aria-label="模型预设">
              {MODEL_PROFILES.filter((profile) => profile.id !== "custom").map((profile) => (
                <button
                  key={profile.id}
                  type="button"
                  disabled={busy}
                  className={activeProfile.id === profile.id ? "active" : ""}
                  onClick={() => onModelEndpoint(
                    profile.endpoint === defaultEndpoint
                      ? ""
                      : profile.endpoint,
                  )}
                >
                  <strong>{profile.name}</strong>
                  <small>{profile.note}</small>
                </button>
              ))}
            </div>
            <label className="model-field">
              <span>OpenAI 兼容接口地址</span>
              <input value={modelEndpoint} onChange={(event) => onModelEndpoint(event.target.value)} placeholder={defaultEndpoint} disabled={busy} />
              <small>
                {activeProfile.id === "minicpm-8005"
                  ? "8005 当前为 MiniCPM Gateway；若 OpenAI 路由未加载音频模态，分析会明确报告兼容性错误。"
                  : "留空使用默认 8004，也可填写本机 127.0.0.1 地址。"}
              </small>
            </label>
            <button
              type="button"
              className="model-probe-button"
              disabled={busy || probing}
              onClick={testModel}
            >
              {probing ? "正在测试…" : "测试模型连接"}
            </button>
            {probe && (
              <div className={`model-probe-result ${
                !probe.online ? "error" : probe.audio_supported === false ? "warning" : "ready"
              }`}>
                <strong>{probe.online ? probe.service : "连接失败"}</strong>
                <span>{probe.detail}</span>
                {probe.model && <small>{probe.model}</small>}
              </div>
            )}
          </>
        ) : (
          <label className="model-field">
            <span>本地模型目录或主 GGUF 路径</span>
            <input value={localModelPath} onChange={(event) => onLocalModelPath(event.target.value)} placeholder={localModelRoot || "src/model"} disabled={busy} />
            <small>
              允许目录：{localModelRoot || "src/model"}，自动配对 mmproj。
              {!localRunnerAvailable && " 当前未检测到 llama-server。"}
            </small>
          </label>
        )}
        {busy && <p className="model-locked">分析进行中，模型设置暂时锁定</p>}
      </div>
    </details>
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

function SingingComparison({ historyId }: { historyId: string | null }) {
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const chunks = useRef<Blob[]>([]);
  const [recording, setRecording] = useState(false);
  const [attempt, setAttempt] = useState<Blob | null>(null);
  const [attemptName, setAttemptName] = useState("my-singing.webm");
  const [attemptUrl, setAttemptUrl] = useState("");
  const [score, setScore] = useState<SingingScore | null>(null);
  const [scoring, setScoring] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => () => {
    stream.current?.getTracks().forEach((track) => track.stop());
    if (attemptUrl) URL.revokeObjectURL(attemptUrl);
  }, [attemptUrl]);

  const useAttempt = (blob: Blob, name: string) => {
    if (attemptUrl) URL.revokeObjectURL(attemptUrl);
    setAttempt(blob);
    setAttemptName(name);
    setAttemptUrl(URL.createObjectURL(blob));
    setScore(null);
    setError("");
  };

  const startRecording = async () => {
    setError("");
    try {
      stream.current = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunks.current = [];
      const next = new MediaRecorder(stream.current);
      next.ondataavailable = (event) => {
        if (event.data.size) chunks.current.push(event.data);
      };
      next.onstop = () => {
        const blob = new Blob(chunks.current, {
          type: next.mimeType || "audio/webm",
        });
        useAttempt(blob, "my-singing.webm");
        stream.current?.getTracks().forEach((track) => track.stop());
        stream.current = null;
      };
      recorder.current = next;
      next.start();
      setRecording(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法使用麦克风");
    }
  };

  const stopRecording = () => {
    recorder.current?.stop();
    recorder.current = null;
    setRecording(false);
  };

  const runScore = async () => {
    if (!historyId || !attempt) return;
    setScoring(true);
    setError("");
    try {
      setScore(await api.scoreSinging(historyId, attempt, attemptName));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "演唱评分失败");
    } finally {
      setScoring(false);
    }
  };

  return (
    <section className="panel singing-panel">
      <header>
        <div><span className="section-number">06</span><h3>演唱对比</h3></div>
        <small>本地声学评分 · 不由大模型决定总分</small>
      </header>
      <div className="singing-actions">
        <button
          type="button"
          className={recording ? "recording" : ""}
          disabled={!historyId}
          onClick={() => recording ? stopRecording() : void startRecording()}
        >
          {recording ? "■ 停止录音" : "● 开始演唱"}
        </button>
        <label>
          上传录音
          <input
            type="file"
            accept="audio/*,.wav,.mp3,.m4a,.webm,.ogg"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) useAttempt(file, file.name);
            }}
          />
        </label>
        <button
          type="button"
          className="score-button"
          disabled={!attempt || !historyId || scoring || recording}
          onClick={() => void runScore()}
        >
          {scoring ? "正在对齐音高与节奏…" : "开始评分"}
        </button>
      </div>
      {attemptUrl && <audio src={attemptUrl} controls preload="metadata" />}
      {error && <p className="singing-error">{error}</p>}
      {score && (
        <div className="score-result">
          <div className="total-score">
            <strong>{score.total}</strong><span>/ 100</span><small>综合得分</small>
          </div>
          <div className="score-breakdown">
            {[
              ["音准", score.pitch],
              ["节奏", score.rhythm],
              ["完整度", score.completeness],
              ["稳定性", score.stability],
            ].map(([label, value]) => (
              <div key={label}>
                <span>{label}</span><strong>{value}</strong>
                <i><b style={{ width: `${value}%` }} /></i>
              </div>
            ))}
          </div>
          <div className="pitch-error-strip" aria-label="音高误差时间轴">
            {score.pitch_curve.map((point, index) => {
              const errorValue = point.error_semitones;
              const level = errorValue == null ? "missing"
                : errorValue <= 0.5 ? "good"
                : errorValue <= 1.5 ? "medium" : "bad";
              return <span key={index} className={level} title={
                errorValue == null ? "无可比音高" : `偏差 ${errorValue} 半音`
              } />;
            })}
          </div>
          <p className="score-summary">
            音高误差中位数：
            {score.median_pitch_error == null ? "证据不足" : `${score.median_pitch_error} 半音`}
            {score.in_tune_ratio != null && ` · 半音内命中 ${percent(score.in_tune_ratio)}`}
          </p>
          {score.notes.map((note) => <p className="score-note" key={note}>{note}</p>)}
        </div>
      )}
    </section>
  );
}

function ResultPanel({
  result,
  audioUrl,
  fileName,
  historyId,
  revisionCount,
  onSaveLyrics,
}: {
  result: AnalysisResult;
  audioUrl: string;
  fileName: string;
  historyId: string | null;
  revisionCount: number;
  onSaveLyrics: (lyrics: LyricsSegment[]) => Promise<void>;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [editingLyrics, setEditingLyrics] = useState(false);
  const [draftLyrics, setDraftLyrics] = useState<LyricsSegment[]>(result.lyrics);
  const [savingLyrics, setSavingLyrics] = useState(false);
  const [lyricError, setLyricError] = useState("");
  const [revisions, setRevisions] = useState<HistoryRevision[]>([]);
  const [selectedRevision, setSelectedRevision] = useState("current");
  const [loadingRevisions, setLoadingRevisions] = useState(false);
  const [retryingChunk, setRetryingChunk] = useState("");
  const [retryPreview, setRetryPreview] = useState<{
    key: string;
    result: LyricsRetryResult;
  } | null>(null);
  useEffect(() => {
    setDraftLyrics(result.lyrics);
    setEditingLyrics(false);
    setSelectedRevision("current");
    setRevisions([]);
    setRetryPreview(null);
    setRetryingChunk("");
    setLyricError("");
  }, [result]);
  const displayedLyrics = selectedRevision === "current"
    ? result.lyrics
    : revisions.find((item) => String(item.id) === selectedRevision)?.lyrics || result.lyrics;
  const qualityEvents = result.evidence.filter((item) =>
    item.id.includes(".quality_retry")
  );
  const duration = useMemo(() => {
    const ends = [
      ...result.lyrics.map((item) => item.span?.end_s || 0),
      ...result.emotion_timeline.map((item) => item.span?.end_s || 0),
      ...result.sound_events.map((item) => item.span?.end_s || 0),
      ...result.technical_metrics.evidence.map((item) => {
        const value = item.metadata.duration_s;
        return typeof value === "number" ? value : 0;
      }),
    ];
    return Math.max(...ends, 1);
  }, [result]);

  const seek = (value: number | undefined) => {
    if (audioRef.current && value != null) {
      audioRef.current.currentTime = value;
      void audioRef.current.play();
    }
  };

  const loadRevisions = async () => {
    if (!historyId || loadingRevisions || revisions.length) return;
    setLoadingRevisions(true);
    try {
      setRevisions(await api.historyRevisions(historyId));
    } catch (cause) {
      setLyricError(cause instanceof Error ? cause.message : "无法读取修订历史");
    } finally {
      setLoadingRevisions(false);
    }
  };

  const saveLyrics = async () => {
    const invalid = draftLyrics.some((line) =>
      !line.text.trim()
      || Boolean(line.span && line.span.end_s < line.span.start_s)
    );
    if (invalid) {
      setLyricError("歌词不能为空，结束时间也不能早于开始时间。");
      return;
    }
    setSavingLyrics(true);
    setLyricError("");
    try {
      await onSaveLyrics(draftLyrics);
      setEditingLyrics(false);
      setSelectedRevision("current");
      setRevisions([]);
    } catch (cause) {
      setLyricError(cause instanceof Error ? cause.message : "歌词保存失败");
    } finally {
      setSavingLyrics(false);
    }
  };

  const updateDraft = (
    index: number,
    field: "text" | "start_s" | "end_s",
    value: string,
  ) => {
    setDraftLyrics((items) => items.map((item, itemIndex) => {
      if (itemIndex !== index) return item;
      if (field === "text") return { ...item, text: value };
      const numeric = Math.max(0, Number(value) || 0);
      const span = item.span || { start_s: 0, end_s: 0 };
      return { ...item, span: { ...span, [field]: numeric } };
    }));
  };

  const retryChunk = async (key: string, start_s: number, end_s: number) => {
    if (!historyId) return;
    setRetryingChunk(key);
    setRetryPreview(null);
    setLyricError("");
    try {
      const retried = await api.retryLyrics(historyId, start_s, end_s);
      setRetryPreview({ key, result: retried });
    } catch (cause) {
      setLyricError(cause instanceof Error ? cause.message : "分块重听失败");
    } finally {
      setRetryingChunk("");
    }
  };

  const applyRetry = async () => {
    if (!retryPreview?.result.lyrics.length) return;
    const retried = retryPreview.result;
    const unaffected = result.lyrics.filter((line) =>
      !line.span
      || line.span.end_s <= retried.start_s
      || line.span.start_s >= retried.end_s
    );
    const merged = [...unaffected, ...retried.lyrics].sort((first, second) =>
      (first.span?.start_s ?? Number.POSITIVE_INFINITY)
      - (second.span?.start_s ?? Number.POSITIVE_INFINITY)
    );
    setSavingLyrics(true);
    setLyricError("");
    try {
      await onSaveLyrics(merged);
      setRetryPreview(null);
    } catch (cause) {
      setLyricError(cause instanceof Error ? cause.message : "重听结果保存失败");
    } finally {
      setSavingLyrics(false);
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
          <header>
            <div><span className="section-number">01</span><h3>歌词时间轴</h3></div>
            <div className="lyrics-tools">
              {revisionCount > 0 && (
                <select
                  aria-label="歌词修订版本"
                  value={selectedRevision}
                  disabled={editingLyrics}
                  onFocus={() => void loadRevisions()}
                  onChange={(event) => setSelectedRevision(event.target.value)}
                >
                  <option value="current">当前版本</option>
                  {revisions.map((revision, index) => (
                    <option key={revision.id} value={String(revision.id)}>
                      修订前 {revisions.length - index}
                    </option>
                  ))}
                </select>
              )}
              {historyId && selectedRevision === "current" && (
                editingLyrics ? (
                  <>
                    <button
                      type="button"
                      onClick={() => {
                        setDraftLyrics(result.lyrics);
                        setEditingLyrics(false);
                        setLyricError("");
                      }}
                    >
                      取消
                    </button>
                    <button
                      type="button"
                      className="save"
                      disabled={savingLyrics}
                      onClick={() => void saveLyrics()}
                    >
                      {savingLyrics ? "保存中…" : "保存修订"}
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    onClick={() => {
                      setDraftLyrics(result.lyrics);
                      setEditingLyrics(true);
                    }}
                  >
                    校对歌词
                  </button>
                )
              )}
              <small>{displayedLyrics.length} 个片段</small>
            </div>
          </header>
          {qualityEvents.length > 0 && selectedRevision === "current" && (
            <div className="lyrics-quality">
              <strong>自动质量检查处理了 {qualityEvents.length} 个分块</strong>
              {qualityEvents.map((event) => {
                const issues = Array.isArray(event.metadata.issues)
                  ? event.metadata.issues.map(String)
                  : [];
                const original = Array.isArray(event.metadata.original_lyrics)
                  ? event.metadata.original_lyrics as LyricsSegment[]
                  : [];
                const recovered = Array.isArray(event.metadata.recovered_lyrics)
                  ? event.metadata.recovered_lyrics as LyricsSegment[]
                  : [];
                return (
                  <details key={event.id}>
                    <summary>
                      <span>{seconds(event.span?.start_s)}–{seconds(event.span?.end_s)}</span>
                      {event.text}
                    </summary>
                    {issues.map((issue) => <p key={issue}>{issue}</p>)}
                    {(original.length > 0 || recovered.length > 0) && (
                      <div className="quality-compare">
                        <div>
                          <small>初次结果</small>
                          {original.map((line, index) => (
                            <p key={`${line.text}-${index}`}>{line.text}</p>
                          ))}
                        </div>
                        <div>
                          <small>重听结果</small>
                          {recovered.length
                            ? recovered.map((line, index) => (
                              <p key={`${line.text}-${index}`}>{line.text}</p>
                            ))
                            : <p>未获得可靠替代结果</p>}
                        </div>
                      </div>
                    )}
                    {historyId && event.span && (
                      <button
                        type="button"
                        className="retry-chunk"
                        disabled={Boolean(retryingChunk) || savingLyrics}
                        onClick={() => void retryChunk(
                          event.id,
                          event.span!.start_s,
                          event.span!.end_s,
                        )}
                      >
                        {retryingChunk === event.id
                          ? "8004 正在重新聆听…"
                          : "重新聆听此分块"}
                      </button>
                    )}
                  </details>
                );
              })}
            </div>
          )}
          {lyricError && <p className="lyrics-error">{lyricError}</p>}
          {retryPreview && (
            <div className="retry-preview">
              <div>
                <strong>重听预览</strong>
                <small>
                  {seconds(retryPreview.result.start_s)}–
                  {seconds(retryPreview.result.end_s)} · {retryPreview.result.source}
                </small>
              </div>
              {retryPreview.result.lyrics.length ? (
                <ul>
                  {retryPreview.result.lyrics.map((line, index) => (
                    <li key={`${line.text}-${index}`}>
                      <span>{seconds(line.span?.start_s)}</span>
                      {line.text}
                    </li>
                  ))}
                </ul>
              ) : (
                <p>本次重听没有确认到可靠歌词，不会覆盖当前结果。</p>
              )}
              {retryPreview.result.issues.map((issue) => (
                <p className="retry-issue" key={issue}>{issue}</p>
              ))}
              <div className="retry-actions">
                <button type="button" onClick={() => setRetryPreview(null)}>
                  放弃
                </button>
                <button
                  type="button"
                  className="apply"
                  disabled={!retryPreview.result.lyrics.length || savingLyrics}
                  onClick={() => void applyRetry()}
                >
                  {savingLyrics ? "保存中…" : "应用并保存为新版本"}
                </button>
              </div>
            </div>
          )}
          {editingLyrics ? (
            <div className="lyrics-editor">
              {draftLyrics.map((line, index) => (
                <div className="lyrics-editor-row" key={index}>
                  <input
                    aria-label={`第 ${index + 1} 行开始时间`}
                    type="number"
                    min="0"
                    step="0.01"
                    value={line.span?.start_s ?? 0}
                    onChange={(event) => updateDraft(index, "start_s", event.target.value)}
                  />
                  <input
                    aria-label={`第 ${index + 1} 行结束时间`}
                    type="number"
                    min="0"
                    step="0.01"
                    value={line.span?.end_s ?? 0}
                    onChange={(event) => updateDraft(index, "end_s", event.target.value)}
                  />
                  <input
                    aria-label={`第 ${index + 1} 行歌词`}
                    value={line.text}
                    onChange={(event) => updateDraft(index, "text", event.target.value)}
                  />
                  <button
                    type="button"
                    aria-label={`删除第 ${index + 1} 行`}
                    onClick={() => setDraftLyrics((items) =>
                      items.filter((_, itemIndex) => itemIndex !== index)
                    )}
                  >
                    ×
                  </button>
                </div>
              ))}
              <button
                type="button"
                className="add-lyric"
                onClick={() => setDraftLyrics((items) => [
                  ...items,
                  {
                    text: "",
                    span: {
                      start_s: items.at(-1)?.span?.end_s || 0,
                      end_s: items.at(-1)?.span?.end_s || 0,
                    },
                    language: null,
                    confidence: null,
                  },
                ])}
              >
                ＋ 添加歌词行
              </button>
            </div>
          ) : displayedLyrics.length ? (
            <div className="lyrics-list">
              {displayedLyrics.map((line: LyricsSegment, index) => {
                const checked = selectedRevision === "current" && qualityEvents.some(
                  (event) => event.span && line.span
                    && line.span.start_s >= event.span.start_s
                    && line.span.start_s <= event.span.end_s
                );
                const chunkStart = Math.floor((line.span?.start_s || 0) / 30) * 30;
                const chunkEnd = Math.min(chunkStart + 30, duration);
                const retryKey = `line-${index}`;
                return (
                  <div className="lyric-row" key={`${line.text}-${index}`}>
                    <button
                      className="lyric-seek"
                      onClick={() => seek(line.span?.start_s)}
                    >
                      <span>{seconds(line.span?.start_s)}</span>
                      <p>
                        {line.text}
                        {checked && <small className="quality-badge">已重听</small>}
                      </p>
                      <Confidence value={line.confidence} />
                    </button>
                    {historyId && selectedRevision === "current" && line.span && (
                      <button
                        type="button"
                        className="line-retry"
                        title={`${seconds(chunkStart)}–${seconds(chunkEnd)} 重新聆听`}
                        disabled={Boolean(retryingChunk) || savingLyrics}
                        onClick={() => void retryChunk(retryKey, chunkStart, chunkEnd)}
                      >
                        {retryingChunk === retryKey ? "…" : "重听"}
                      </button>
                    )}
                  </div>
                );
              })}
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
      <SingingComparison historyId={historyId} />
    </div>
  );
}

const historyStateLabels: Record<string, string> = {
  queued: "排队中",
  running: "分析中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

function historyTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function HistorySidebar({
  items,
  activeId,
  compareIds,
  onNew,
  onSelect,
  onDelete,
  onRename,
  onToggleCompare,
  onCompare,
}: {
  items: HistorySummary[];
  activeId: string | null;
  compareIds: string[];
  onNew: () => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onRename: (item: HistorySummary) => void;
  onToggleCompare: (id: string) => void;
  onCompare: () => void;
}) {
  return (
    <aside className="history-sidebar">
      <div className="history-brand"><SignalMark /><span>Music Insight</span></div>
      <button className="new-analysis" onClick={onNew}><span>＋</span> 新分析</button>
      <div className="history-heading">
        <span>分析历史</span><small>{items.length}</small>
      </div>
      <div className="history-list">
        {items.length ? items.map((item) => (
          <article key={item.id} className={`history-item ${activeId === item.id ? "active" : ""}`}>
            <button className="history-open" onClick={() => onSelect(item.id)}>
              <strong>{item.title}</strong>
              <span>{historyTime(item.created_at)} · {historyStateLabels[item.state] || item.state}</span>
              {item.state === "completed" && (
                <small>{item.lyrics_count} 段歌词{item.bpm ? ` · ${item.bpm.toFixed(1)} BPM` : ""}</small>
              )}
              <small>{item.model_source === "local" ? "本地权重" : item.model_location || "默认 8004"}</small>
            </button>
            <div className="history-actions">
              <label title="加入对比">
                <input
                  type="checkbox"
                  checked={compareIds.includes(item.id)}
                  disabled={item.state !== "completed" || (!compareIds.includes(item.id) && compareIds.length >= 2)}
                  onChange={() => onToggleCompare(item.id)}
                />
                对比
              </label>
              <button onClick={() => onRename(item)} aria-label={`重命名 ${item.title}`}>✎</button>
              <button onClick={() => onDelete(item.id)} aria-label={`删除 ${item.title}`}>×</button>
            </div>
          </article>
        )) : <p className="history-empty">分析完成后会保存在这里</p>}
      </div>
      <button className="compare-button" disabled={compareIds.length !== 2} onClick={onCompare}>
        对比分析 {compareIds.length}/2
      </button>
      <p className="local-note">历史与音频仅保存在本机</p>
    </aside>
  );
}

function ComparisonPanel({ entries }: { entries: HistoryDetail[] }) {
  const rows: Array<[string, (entry: HistoryDetail) => string]> = [
    ["时长", (entry) => seconds(entry.duration_s)],
    ["BPM", (entry) => entry.result?.technical_metrics.bpm?.toFixed(1) || "—"],
    ["调性", (entry) => entry.result?.technical_metrics.key || "—"],
    ["歌词", (entry) => `${entry.lyrics_count} 个片段`],
    ["乐器", (entry) => entry.instruments.join("、") || "未确认"],
    ["主题", (entry) => entry.result?.themes.join("、") || "未确认"],
    ["直接情绪", (entry) => Array.from(new Set(entry.result?.emotion_timeline.map((item) => item.text) || [])).join("、") || "未确认"],
    ["推断氛围", (entry) => entry.result?.inferred_atmosphere.map((item) => item.text).join("、") || "未确认"],
  ];
  return (
    <section className="comparison panel">
      <div className="section-kicker">COMPARE ANALYSES</div>
      <h2>并排比较</h2>
      <div className="comparison-grid comparison-head">
        <span>指标</span>
        {entries.map((entry) => <strong key={entry.id}>{entry.title}</strong>)}
      </div>
      {rows.map(([label, read]) => (
        <div className="comparison-grid" key={label}>
          <span>{label}</span>
          {entries.map((entry) => <p key={entry.id}>{read(entry)}</p>)}
        </div>
      ))}
      <div className="comparison-summaries">
        {entries.map((entry) => (
          <article key={entry.id}><strong>{entry.title}</strong><p>{entry.summary || "暂无摘要"}</p></article>
        ))}
      </div>
    </section>
  );
}

export default function App() {
  const [health, setHealth] = useState<HealthResult | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [audioUrl, setAudioUrl] = useState("");
  const [fileName, setFileName] = useState("");
  const [language, setLanguage] = useState("auto");
  const [modelSource, setModelSource] = useState<"network" | "local">("network");
  const [modelEndpoint, setModelEndpoint] = useState("");
  const [localModelPath, setLocalModelPath] = useState("");
  const [job, setJob] = useState<JobSnapshot | null>(null);
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const [error, setError] = useState("");
  const [history, setHistory] = useState<HistorySummary[]>([]);
  const [activeHistoryId, setActiveHistoryId] = useState<string | null>(null);
  const [activeRevisionCount, setActiveRevisionCount] = useState(0);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [comparison, setComparison] = useState<HistoryDetail[]>([]);

  const refreshHistory = () => {
    api.history()
      .then(setHistory)
      .catch(() => setHistory([]));
  };

  useEffect(() => {
    api.health()
      .then(setHealth)
      .catch(() => setHealth(null));
    refreshHistory();
  }, []);

  useEffect(() => {
    if (!job || ["completed", "failed", "cancelled"].includes(job.state)) return;
    const events = new EventSource(`${API_BASE}/jobs/${job.id}/events`);
    let terminal = false;
    events.addEventListener("progress", async (event) => {
      const snapshot = JSON.parse((event as MessageEvent).data) as JobSnapshot;
      setJob(snapshot);
      refreshHistory();
      if (snapshot.state === "completed") {
        terminal = true;
        events.close();
        try {
          setResult(await api.jobResult(snapshot.id));
          setActiveHistoryId(snapshot.id);
          setActiveRevisionCount(0);
          refreshHistory();
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
    if (audioUrl.startsWith("blob:")) URL.revokeObjectURL(audioUrl);
  }, [audioUrl]);

  const chooseFile = (next: File) => {
    if (audioUrl.startsWith("blob:")) URL.revokeObjectURL(audioUrl);
    setFile(next);
    setFileName(next.name);
    setAudioUrl(URL.createObjectURL(next));
    setResult(null);
    setJob(null);
    setActiveHistoryId(null);
    setActiveRevisionCount(0);
    setComparison([]);
    setError("");
  };

  const analyze = async () => {
    if (!file) return;
    setError("");
    setResult(null);
    const form = new FormData();
    form.append("file", file);
    if (language !== "auto") form.append("language", language);
    form.append("model_source", modelSource);
    if (modelSource === "network" && modelEndpoint.trim()) {
      form.append("model_endpoint", modelEndpoint.trim());
    }
    if (modelSource === "local") {
      form.append("local_model_path", localModelPath.trim() || health?.local_model_root || "src/model");
    }
    try {
      const snapshot = await api.createJob(form);
      setJob(snapshot);
      setActiveHistoryId(snapshot.id);
      refreshHistory();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法创建分析任务");
    }
  };

  const cancel = async () => {
    if (!job) return;
    try {
      setJob(await api.cancelJob(job.id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "取消失败");
    }
  };

  const newAnalysis = () => {
    if (audioUrl.startsWith("blob:")) URL.revokeObjectURL(audioUrl);
    setFile(null);
    setFileName("");
    setAudioUrl("");
    setJob(null);
    setResult(null);
    setError("");
    setActiveHistoryId(null);
    setActiveRevisionCount(0);
    setComparison([]);
  };

  const selectHistory = async (id: string) => {
    setError("");
    setComparison([]);
    try {
      const entry = await api.historyDetail(id);
      setActiveHistoryId(id);
      setActiveRevisionCount(entry.revision_count);
      setFile(null);
      setFileName(entry.file_name);
      setResult(entry.result);
      setAudioUrl(entry.audio_url ? `${API_BASE}${entry.audio_url}` : "");
      if (entry.state === "running" || entry.state === "queued") {
        try {
          setJob(await api.job(id));
        } catch {
          setJob(null);
        }
      } else {
        setJob(null);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取历史分析");
    }
  };

  const deleteHistory = async (id: string) => {
    try {
      await api.deleteHistory(id);
      if (activeHistoryId === id) newAnalysis();
      setCompareIds((items) => items.filter((item) => item !== id));
      refreshHistory();
    } catch (cause) {
      setError(cause instanceof ApiError && cause.status === 409
        ? "请先取消正在运行的任务"
        : cause instanceof Error ? cause.message : "删除失败");
    }
  };

  const renameHistory = async (item: HistorySummary) => {
    const title = window.prompt("重命名分析", item.title)?.trim();
    if (!title || title === item.title) return;
    try {
      await api.renameHistory(item.id, title);
      refreshHistory();
    } catch {
      setError("重命名失败");
    }
  };

  const toggleCompare = (id: string) => {
    setCompareIds((items) => items.includes(id) ? items.filter((item) => item !== id) : [...items, id].slice(0, 2));
  };

  const compareHistory = async () => {
    if (compareIds.length !== 2) return;
    try {
      setComparison(await Promise.all(compareIds.map(api.historyDetail)));
      setActiveHistoryId(null);
      setJob(null);
      setResult(null);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取对比结果");
    }
  };

  const saveLyrics = async (lyrics: LyricsSegment[]) => {
    if (!activeHistoryId) throw new Error("当前分析尚未保存到历史记录。");
    const entry = await api.updateLyrics(activeHistoryId, lyrics);
    if (!entry.result) throw new Error("后端没有返回修订结果。");
    setResult(entry.result);
    setActiveRevisionCount(entry.revision_count);
    refreshHistory();
  };

  const busy = job?.state === "queued" || job?.state === "running";

  return (
    <div className="app-shell">
      <HistorySidebar
        items={history}
        activeId={activeHistoryId}
        compareIds={compareIds}
        onNew={newAnalysis}
        onSelect={selectHistory}
        onDelete={deleteHistory}
        onRename={renameHistory}
        onToggleCompare={toggleCompare}
        onCompare={compareHistory}
      />
      <div className="app-main">
        <header className="topbar">
          <a className="brand" href="#top"><SignalMark /><span>Music Insight</span></a>
          <div className="topbar-actions">
            <div className="service-status">
              <span className={health ? "online" : "offline"} />
              <div><strong>{health ? "分析服务在线" : "后端未连接"}</strong><small>{health?.model_endpoint || API_BASE}</small></div>
            </div>
            <ModelSettings
              modelSource={modelSource}
              modelEndpoint={modelEndpoint}
              localModelPath={localModelPath}
              defaultEndpoint={health?.model_endpoint || "http://192.168.1.97:8004"}
              localModelRoot={health?.local_model_root || "src/model"}
              localRunnerAvailable={health?.local_runner_available ?? false}
              busy={Boolean(busy)}
              onModelSource={setModelSource}
              onModelEndpoint={setModelEndpoint}
              onLocalModelPath={setLocalModelPath}
            />
          </div>
        </header>

        <main id="top">
          {!activeHistoryId && comparison.length === 0 && (
            <UploadPanel
              file={file}
              language={language}
              busy={Boolean(busy)}
              onFile={chooseFile}
              onLanguage={setLanguage}
              onAnalyze={analyze}
            />
          )}
          {comparison.length === 2 && <ComparisonPanel entries={comparison} />}
          {error && <div className="error-banner"><strong>出现问题</strong><span>{error}</span></div>}
          {job && <ProgressPanel job={job} onCancel={cancel} />}
          {result && fileName && (
            <ResultPanel
              result={result}
              audioUrl={audioUrl}
              fileName={fileName}
              historyId={activeHistoryId}
              revisionCount={activeRevisionCount}
              onSaveLyrics={saveLyrics}
            />
          )}
        </main>

        <footer><span>Music Insight · 本地优先的音乐证据分析</span><span>FastAPI + React</span></footer>
      </div>
    </div>
  );
}
