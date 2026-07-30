import {
  useCallback,
  useEffect,
  useReducer,
  useState,
} from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { api, API_BASE } from "../../api";
import { announceAuthChange } from "../../authSession";
import {
  ModelSettings,
  ProgressPanel,
  UploadPanel,
} from "../analysis/AnalysisControls";
import { ResultPanel } from "../analysis/ResultPanel";
import {
  ComparisonPanel,
  HistorySidebar,
  UserMenu,
} from "../history/HistoryViews";
import {
  LeaderboardPanel,
  StandaloneSingingComparison,
} from "../singing/SingingViews";
import { SingingAttemptsPanel } from "../singing/SingingAttemptsPanel";
import { LatestRequest } from "../../hooks/latestRequest";
import { LanguageSwitcher, useI18n } from "../../i18n";
import { tabIndexAfterKey } from "../../hooks/tabKeyboard";
import { useObjectUrl } from "../../hooks/useObjectUrl";
import type {
  HealthResult,
  RuntimeConfig,
  User,
} from "../../types";
import { useAnalysisSubmission } from "./useAnalysisSubmission";
import { useHistoryNavigation } from "./useHistoryNavigation";
import { useWorkspaceJob } from "./useWorkspaceJob";
import {
  activeHistoryId,
  comparisonEntries,
  historyAudioUrl,
  initialWorkspaceState,
  selectedFile,
  selectedFileName,
  selectedRevisionCount,
  sidebarHistoryId,
  workspaceReducer,
} from "./workspaceState";

export function AuthenticatedWorkspace({
  user,
  health,
  onUserUpdated,
  onLoggedOut,
}: {
  user: User;
  health: HealthResult | null;
  onUserUpdated: (user: User) => void;
  onLoggedOut: () => void;
}) {
  const { t } = useI18n();
  const [workspace, dispatch] = useReducer(
    workspaceReducer,
    initialWorkspaceState,
  );
  const {
    url: uploadAudioUrl,
    setBlob: setUploadBlob,
    clear: clearUploadAudio,
  } = useObjectUrl();
  const [language, setLanguage] = useState("auto");
  const [inputSource, setInputSource] = useState<"file" | "url">("file");
  const [remoteUrl, setRemoteUrl] = useState("");
  const [modelSource, setModelSource] = useState<"network" | "local">("network");
  const [modelEndpoint, setModelEndpoint] = useState("");
  const [localModelPath, setLocalModelPath] = useState("");
  const [startMode, setStartMode] = useState<"analysis" | "singing">("analysis");
  const [showSingingAttempts, setShowSingingAttempts] = useState(false);
  const [showLeaderboard, setShowLeaderboard] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [runtimeConfig, setRuntimeConfig] = useState<RuntimeConfig | null>(null);
  const [viewRequests] = useState(() => new LatestRequest());
  const file = selectedFile(workspace);
  const fileName = selectedFileName(workspace);
  const activeId = activeHistoryId(workspace);
  const { job, result, error } = workspace;
  const sidebarActiveId = sidebarHistoryId(workspace);
  const activeRevisionCount = selectedRevisionCount(workspace);
  const comparison = comparisonEntries(workspace);
  const storedAudioUrl = historyAudioUrl(workspace);

  useEffect(() => {
    if (!file) clearUploadAudio();
  }, [clearUploadAudio, file]);

  useEffect(() => {
    const controller = new AbortController();
    void api.runtimeConfig(controller.signal)
      .then(setRuntimeConfig)
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        dispatch({
          type: "set-error",
          error: cause instanceof Error
            ? cause.message
            : "无法读取后端运行配置",
        });
      });
    return () => controller.abort();
  }, []);

  const {
    history,
    compareIds,
    deletingIds,
    refreshHistory,
    newAnalysis,
    selectHistory,
    deleteHistory,
    renameHistory,
    toggleCompare,
    compareHistory,
    invalidateRequests,
  } = useHistoryNavigation({
    dispatch,
    viewRequests,
  });

  const { analyze, creatingJob } = useAnalysisSubmission({
    file,
    inputSource,
    remoteUrl,
    language,
    modelSource,
    modelEndpoint,
    localModelPath,
    runtimeConfig,
    dispatch,
    viewRequests,
    refreshHistory,
  });

  const {
    jobBusy,
    connectionWarning,
    cancel,
    saveLyrics,
  } = useWorkspaceJob({
    job,
    activeHistoryId: activeId,
    dispatch,
    refreshHistory,
  });
  const busy = creatingJob || jobBusy;

  const chooseFile = (next: File) => {
    viewRequests.invalidate();
    setUploadBlob(next);
    dispatch({ type: "file-chosen", file: next });
    setInputSource("file");
    setStartMode("analysis");
  };

  const closeLeaderboard = useCallback(() => setShowLeaderboard(false), []);
  const closeSingingAttempts = useCallback(
    () => setShowSingingAttempts(false),
    [],
  );

  const handleStartModeKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
  ) => {
    const modes = ["analysis", "singing"] as const;
    const currentIndex = modes.indexOf(startMode);
    const nextIndex = tabIndexAfterKey(currentIndex, modes.length, event.key);
    if (nextIndex === null) return;
    event.preventDefault();
    setStartMode(modes[nextIndex]);
    const tabs = event.currentTarget.parentElement
      ?.querySelectorAll<HTMLButtonElement>('[role="tab"]');
    tabs?.[nextIndex]?.focus();
  };

  const logout = async () => {
    if (loggingOut) return;
    setLoggingOut(true);
    try {
      await api.logout();
      announceAuthChange();
      invalidateRequests();
      onLoggedOut();
    } catch (cause) {
      dispatch({
        type: "set-error",
        error: cause instanceof Error ? cause.message : "退出失败，请稍后再试",
      });
      setLoggingOut(false);
    }
  };

  const audioUrl = file
    ? uploadAudioUrl
    : storedAudioUrl
      || (activeId
        ? `${API_BASE}/history/${encodeURIComponent(activeId)}/audio`
        : "");
  const workspaceTitle = comparison.length === 2
    ? t("双曲对比")
    : activeId
      ? fileName || t("分析详情")
      : startMode === "analysis"
        ? t("新建音乐导赏")
        : t("演唱对比");

  return (
    <div className="app-shell">
      <HistorySidebar
        items={history}
        username={user.username}
        activeId={sidebarActiveId}
        compareIds={compareIds}
        deletingIds={deletingIds}
        onNew={newAnalysis}
        onSelect={(id) => void selectHistory(id)}
        onDelete={(item) => void deleteHistory(item)}
        onRename={(item) => void renameHistory(item)}
        onToggleCompare={toggleCompare}
        onCompare={() => void compareHistory()}
      />
      <div className="app-main">
        <header className="topbar">
          <a className="workspace-context" href="#top">
            <span>LISTENING STUDIO</span>
            <strong>{workspaceTitle}</strong>
          </a>
          <div className="topbar-actions">
            <div className="service-status">
              <span className={health ? "online" : "offline"} />
              <div>
                <strong>{health ? t("分析服务在线") : t("后端未连接")}</strong>
                <small>{runtimeConfig?.model_endpoint || API_BASE}</small>
              </div>
            </div>
            <LanguageSwitcher compact />
            <ModelSettings
              modelSource={modelSource}
              modelEndpoint={modelEndpoint}
              localModelPath={localModelPath}
              defaultEndpoint={runtimeConfig?.model_endpoint || "http://192.168.1.97:8004"}
              localModelRoot={runtimeConfig?.local_model_root || "src/model"}
              localRunnerAvailable={runtimeConfig?.local_runner_available ?? false}
              busy={busy}
              onModelSource={setModelSource}
              onModelEndpoint={setModelEndpoint}
              onLocalModelPath={setLocalModelPath}
            />
            <button
              type="button"
              className="attempt-history-trigger"
              aria-label={t("打开我的演唱记录")}
              onClick={() => setShowSingingAttempts(true)}
            >
              <span aria-hidden="true">♫</span>
              <span><strong>{t("演唱记录")}</strong><small>{t("个人评分历史")}</small></span>
            </button>
            <button
              type="button"
              className="leaderboard-trigger"
              aria-label={t("打开演唱排行榜")}
              onClick={() => setShowLeaderboard(true)}
            >
              <span aria-hidden="true">♜</span>
              <span><strong>{t("排行榜")}</strong><small>{t("演唱最高分")}</small></span>
            </button>
            <UserMenu
              user={user}
              onSingingAttempts={() => setShowSingingAttempts(true)}
              onLeaderboard={() => setShowLeaderboard(true)}
              onLogout={() => void logout()}
            />
          </div>
        </header>

        <main id="top">
          {!activeId && comparison.length === 0 && (
            <>
              <div className="start-mode-tabs" role="tablist" aria-label={t("开始方式")}>
                <button
                  type="button"
                  role="tab"
                  id="start-tab-analysis"
                  aria-controls="start-panel"
                  aria-selected={startMode === "analysis"}
                  tabIndex={startMode === "analysis" ? 0 : -1}
                  className={startMode === "analysis" ? "active" : ""}
                  onClick={() => setStartMode("analysis")}
                  onKeyDown={handleStartModeKeyDown}
                >
                  {t("音乐分析")}
                  <small>{t("上传歌曲并生成完整报告")}</small>
                </button>
                <button
                  type="button"
                  role="tab"
                  id="start-tab-singing"
                  aria-controls="start-panel"
                  aria-selected={startMode === "singing"}
                  tabIndex={startMode === "singing" ? 0 : -1}
                  className={startMode === "singing" ? "active" : ""}
                  onClick={() => setStartMode("singing")}
                  onKeyDown={handleStartModeKeyDown}
                >
                  {t("独立演唱对比")}
                  <small>{t("参考音频与个人录音直接打分")}</small>
                </button>
              </div>
              <div
                id="start-panel"
                role="tabpanel"
                aria-labelledby={`start-tab-${startMode}`}
              >
                {startMode === "analysis" ? (
                  <UploadPanel
                    file={file}
                    inputSource={inputSource}
                    remoteUrl={remoteUrl}
                    language={language}
                    busy={busy}
                    onFile={chooseFile}
                    onInputSource={(source) => {
                      viewRequests.invalidate();
                      setInputSource(source);
                      dispatch({ type: "new-analysis" });
                    }}
                    onRemoteUrl={setRemoteUrl}
                    onLanguage={setLanguage}
                    onAnalyze={() => void analyze()}
                  />
                ) : <StandaloneSingingComparison />}
              </div>
            </>
          )}
          {comparison.length === 2 && <ComparisonPanel entries={comparison} />}
          {(error || connectionWarning) && (
            <div className="error-banner">
              <strong>{error ? t("出现问题") : t("连接恢复中")}</strong>
              <span>{t(error || connectionWarning || "")}</span>
            </div>
          )}
          {job && <ProgressPanel job={job} onCancel={() => void cancel()} />}
          {result && fileName && (
            <ResultPanel
              key={activeId || fileName}
              result={result}
              audioUrl={audioUrl}
              fileName={fileName}
              historyId={activeId}
              revisionCount={activeRevisionCount}
              onSaveLyrics={saveLyrics}
            />
          )}
        </main>

        <footer><span>Music Insight · {t("本地优先的音乐证据分析")}</span><span>FastAPI + React</span></footer>
      </div>
      {showSingingAttempts && (
        <SingingAttemptsPanel
          onClose={closeSingingAttempts}
          onOpenAnalysis={(historyId) => {
            closeSingingAttempts();
            void selectHistory(historyId);
          }}
        />
      )}
      {showLeaderboard && (
        <LeaderboardPanel
          user={user}
          onUserUpdated={onUserUpdated}
          onClose={closeLeaderboard}
        />
      )}
    </div>
  );
}
