import { useEffect, useRef, useState } from "react";
import type { DragEvent } from "react";
import { api, isAbortError } from "../../api";
import { AbortableLatestRequest } from "../../hooks/abortableLatestRequest";
import { useI18n } from "../../i18n";
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
  inputSource,
  remoteUrl,
  language,
  busy,
  onFile,
  onInputSource,
  onRemoteUrl,
  onLanguage,
  onAnalyze,
}: {
  file: File | null;
  inputSource: "file" | "url";
  remoteUrl: string;
  language: string;
  busy: boolean;
  onFile: (file: File) => void;
  onInputSource: (source: "file" | "url") => void;
  onRemoteUrl: (url: string) => void;
  onLanguage: (language: string) => void;
  onAnalyze: () => void;
}) {
  const { t } = useI18n();
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
      <div className="upload-intro">
        <div>
          <div className="section-kicker">{t("NEW LISTENING SESSION")}</div>
          <h1>{t("先听懂整首歌，")}<br />{t("再追问每个瞬间。")}</h1>
          <p className="lead">
            {t("上传歌曲后，从歌词、段落与声音证据出发，生成可以边听边问、随时跳转复听的音乐导赏。")}
          </p>
        </div>
        <div className="upload-capabilities" aria-label={t("分析流程")}>
          <span><i>01</i><strong>{t("理解全曲")}</strong><small>{t("气氛、结构与情绪弧线")}</small></span>
          <span><i>02</i><strong>{t("定位证据")}</strong><small>{t("歌词、乐器与声音变化")}</small></span>
          <span><i>03</i><strong>{t("带着问题复听")}</strong><small>{t("时间地图与持续对话")}</small></span>
        </div>
      </div>

      <div className="audio-source-tabs" role="group" aria-label={t("音频来源")}>
        <button
          type="button"
          className={inputSource === "file" ? "active" : ""}
          disabled={busy}
          onClick={() => onInputSource("file")}
        >
          {t("本地文件")}
        </button>
        <button
          type="button"
          className={inputSource === "url" ? "active" : ""}
          disabled={busy}
          onClick={() => onInputSource("url")}
        >
          {t("直接音频链接")}
        </button>
      </div>
      {inputSource === "file" ? (
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
          <div className="upload-icon" aria-hidden="true">↑</div>
          {file ? (
            <>
              <strong>{file.name}</strong>
              <span>{(file.size / 1024 / 1024).toFixed(1)} MB · {t("点击更换")}</span>
            </>
          ) : (
            <>
              <strong>{t("拖放音频到这里")}</strong>
              <span>{t("或点击选择 WAV、MP3、FLAC、M4A、OGG")}</span>
            </>
          )}
        </div>
      ) : (
        <label className="remote-audio-field">
          <span>{t("公开的直接音频 URL")}</span>
          <input
            type="url"
            inputMode="url"
            value={remoteUrl}
            disabled={busy}
            placeholder="https://example.com/music.mp3"
            onChange={(event) => onRemoteUrl(event.target.value)}
          />
          <small>
            {t("仅支持直接返回音频的公网 HTTP(S) 链接。不会抓取 YouTube 等网页，也不会绕过登录、版权、付费或 DRM 限制。")}
          </small>
        </label>
      )}

      <div className="upload-actions">
        <label>
          <span>{t("歌词语言")}</span>
          <select value={language} onChange={(event) => onLanguage(event.target.value)} disabled={busy}>
            <option value="auto">{t("自动识别")}</option>
            <option value="zh">{t("中文")}</option>
            <option value="en">English</option>
          </select>
        </label>
        <button
          className="primary-button"
          disabled={
            busy
            || (inputSource === "file" ? !file : !remoteUrl.trim())
          }
          onClick={onAnalyze}
        >
          {busy ? t("分析进行中") : t("开始分析")}
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
  const { t } = useI18n();
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
          detail: cause instanceof Error ? cause.message : t("模型连接测试失败"),
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
      <summary aria-label={t("模型设置")}>
        <span className="settings-icon" aria-hidden="true">⌘</span>
        <span><strong>{t("模型")}</strong><small>{modelSource === "local" ? t("本地权重") : activeLocation.replace(/^https?:\/\//, "")}</small></span>
        <span className="settings-chevron" aria-hidden="true">⌄</span>
      </summary>
      <div className="model-popover">
        <header>
          <div><strong>{t("模型设置")}</strong><small>{t("影响后续新分析")}</small></div>
          <span className={`runner-dot ${modelSource === "network" || localRunnerAvailable ? "ready" : "missing"}`} />
        </header>
        <div className="model-source-tabs" role="group" aria-label={t("模型来源")}>
          <button disabled={busy} className={modelSource === "network" ? "active" : ""} onClick={() => changeModelSource("network")} type="button">{t("模型接口")}</button>
          <button disabled={busy} className={modelSource === "local" ? "active" : ""} onClick={() => changeModelSource("local")} type="button">{t("本地权重")}</button>
        </div>
        {modelSource === "network" ? (
          <>
            <div className="model-presets" role="group" aria-label={t("模型预设")}>
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
                  <strong>{t(profile.name)}</strong>
                  <small>{t(profile.note)}</small>
                </button>
              ))}
            </div>
            <label className="model-field">
              <span>{t("模型服务地址")}</span>
              <input value={modelEndpoint} onChange={(event) => changeModelEndpoint(event.target.value)} placeholder={defaultEndpoint} disabled={busy} />
              <small>
                {activeProfile.id === "minicpm-8005"
                  ? t("8005 使用 Comni WebSocket；服务会自动选择专用音频协议。")
                  : t("留空使用后端默认地址；其他地址会自动探测 OpenAI 或专用 Gateway 协议。")}
              </small>
            </label>
            <button
              type="button"
              className="model-probe-button"
              disabled={busy || probing}
              onClick={testModel}
            >
              {probing ? t("正在测试…") : t("测试模型连接")}
            </button>
            {probe && (
              <div className={`model-probe-result ${
                !probe.online
                  ? "error"
                  : probe.analysis_supported === false
                    ? "warning"
                    : "ready"
              }`}>
                <strong>{probe.online ? probe.service : t("连接失败")}</strong>
                <span>{t(probe.detail)}</span>
                {(probe.model || probe.protocol) && (
                  <small>{[probe.protocol, probe.model].filter(Boolean).join(" · ")}</small>
                )}
              </div>
            )}
          </>
        ) : (
          <label className="model-field">
            <span>{t("本地模型目录或主 GGUF 路径")}</span>
            <input value={localModelPath} onChange={(event) => onLocalModelPath(event.target.value)} placeholder={localModelRoot || "src/model"} disabled={busy} />
            <small>
              {t("允许目录：")}{localModelRoot || "src/model"} · {t("自动配对 mmproj。")}
              {!localRunnerAvailable && ` ${t("当前未检测到 llama-server。")}`}
            </small>
          </label>
        )}
        {busy && <p className="model-locked">{t("分析进行中，模型设置暂时锁定")}</p>}
      </div>
    </details>
  );
}

export function ProgressPanel({ job, onCancel }: { job: JobSnapshot; onCancel: () => void }) {
  const { t, locale } = useI18n();
  const running = job.state === "queued" || job.state === "running";
  const progressPercent = Math.max(
    0,
    Math.min(100, Math.round(job.progress * 100)),
  );
  const stageLabel = t(stageLabels[job.stage] || job.stage);
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
        aria-label={t("音乐分析进度")}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progressPercent}
        aria-valuetext={`${stageLabel}${locale === "en" ? ", " : "，"}${progressPercent}%`}
      >
        <span style={{ width: `${progressPercent}%` }} />
      </div>
      <div className="progress-meta">
        <p>{t(job.error || job.message)}</p>
        {running && <button onClick={onCancel}>{t("取消任务")}</button>}
      </div>
      <div className="stage-row">
        {["音频预处理", "声学计算", "模型聆听", "证据融合"].map((item, index) => (
          <span key={item} className={job.progress >= [0.08, 0.18, 0.36, 0.94][index] ? "active" : ""}>{t(item)}</span>
        ))}
      </div>
    </section>
  );
}
