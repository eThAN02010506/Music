import { useEffect, useRef, useState } from "react";
import type { DragEvent } from "react";
import { api, isAbortError } from "../../api";
import { AbortableLatestRequest } from "../../hooks/abortableLatestRequest";
import { MODEL_PROFILES, profileForEndpoint } from "../../modelProfiles";
import type { JobSnapshot, ModelProbeResult } from "../../types";

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

export function UploadPanel({
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
    if (busy) return;
    const next = event.dataTransfer.files[0];
    if (next?.type.startsWith("audio/")) onFile(next);
  };

  return (
    <section className="upload-card panel">
      <div className="section-kicker">NEW ANALYSIS</div>
      <h1>听见音乐里的证据</h1>
      <p className="lead">上传一段音频，识别歌词、乐器、声音事件与情绪，并结合本地 DSP 形成可核查的分析。</p>

      <div
        className={`drop-zone ${dragging ? "dragging" : ""} ${file ? "has-file" : ""} ${busy ? "disabled" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          if (!busy) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={drop}
        onClick={() => {
          if (!busy) inputRef.current?.click();
        }}
        role="button"
        tabIndex={busy ? -1 : 0}
        aria-disabled={busy}
        onKeyDown={(event) => {
          if (!busy && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            inputRef.current?.click();
          }
        }}
      >
        <input
          ref={inputRef}
          type="file"
          accept="audio/*,.wav,.mp3,.flac,.m4a,.ogg,.oga,.webm"
          hidden
          disabled={busy}
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

export function ModelSettings({
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
  const probeRequestsRef = useRef(new AbortableLatestRequest());

  useEffect(() => {
    probeRequestsRef.current.invalidate();
    setProbe(null);
    setProbing(false);
  }, [defaultEndpoint]);

  useEffect(() => () => probeRequestsRef.current.invalidate(), []);

  const invalidateProbe = () => {
    probeRequestsRef.current.invalidate();
    setProbe(null);
    setProbing(false);
  };

  const changeModelSource = (source: "network" | "local") => {
    invalidateProbe();
    onModelSource(source);
  };

  const changeModelEndpoint = (endpoint: string) => {
    invalidateProbe();
    onModelEndpoint(endpoint);
  };

  const testModel = async () => {
    const request = probeRequestsRef.current.begin();
    setProbing(true);
    try {
      const next = await api.probeModel(activeLocation, request.signal);
      if (probeRequestsRef.current.isCurrent(request.id)) setProbe(next);
    } catch (cause) {
      if (probeRequestsRef.current.isCurrent(request.id) && !isAbortError(cause)) {
        setProbe({
          endpoint: activeLocation,
          online: false,
          model: null,
          protocol: null,
          analysis_supported: null,
          audio_supported: null,
          openai_audio_supported: null,
          service: "OpenAI-compatible",
          detail: cause instanceof Error ? cause.message : "模型连接测试失败",
        });
      }
    } finally {
      if (probeRequestsRef.current.isCurrent(request.id)) {
        probeRequestsRef.current.settle(request.id);
        setProbing(false);
      }
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
          <button disabled={busy} className={modelSource === "network" ? "active" : ""} onClick={() => changeModelSource("network")} type="button">模型接口</button>
          <button disabled={busy} className={modelSource === "local" ? "active" : ""} onClick={() => changeModelSource("local")} type="button">本地权重</button>
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
                  onClick={() => changeModelEndpoint(
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
              <span>模型服务地址</span>
              <input value={modelEndpoint} onChange={(event) => changeModelEndpoint(event.target.value)} placeholder={defaultEndpoint} disabled={busy} />
              <small>
                {activeProfile.id === "minicpm-8005"
                  ? "8005 使用 Comni WebSocket；服务会自动选择专用音频协议。"
                  : "留空使用默认 8004；其他地址会自动探测 OpenAI 或专用 Gateway 协议。"}
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
                !probe.online
                  ? "error"
                  : probe.analysis_supported === false
                    ? "warning"
                    : "ready"
              }`}>
                <strong>{probe.online ? probe.service : "连接失败"}</strong>
                <span>{probe.detail}</span>
                {(probe.model || probe.protocol) && (
                  <small>{[probe.protocol, probe.model].filter(Boolean).join(" · ")}</small>
                )}
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

export function ProgressPanel({ job, onCancel }: { job: JobSnapshot; onCancel: () => void }) {
  const running = job.state === "queued" || job.state === "running";
  const progressPercent = Math.max(
    0,
    Math.min(100, Math.round(job.progress * 100)),
  );
  const stageLabel = stageLabels[job.stage] || job.stage;
  return (
    <section className="progress-card panel" aria-busy={running}>
      <div className="progress-heading" aria-live="polite" aria-atomic="true">
        <div>
          <span className={`status-pulse ${job.state}`} />
          <span>{stageLabel}</span>
        </div>
        <strong>{progressPercent}%</strong>
      </div>
      <div
        className="progress-track"
        role="progressbar"
        aria-label="音乐分析进度"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progressPercent}
        aria-valuetext={`${stageLabel}，${progressPercent}%`}
      >
        <span style={{ width: `${progressPercent}%` }} />
      </div>
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
