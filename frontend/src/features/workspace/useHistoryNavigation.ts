import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type Dispatch,
} from "react";
import {
  api,
  ApiError,
  API_BASE,
} from "../../api";
import { AbortableLatestRequest } from "../../hooks/abortableLatestRequest";
import { LatestRequest } from "../../hooks/latestRequest";
import { useI18n } from "../../i18n";
import type { HistorySummary, JobSnapshot } from "../../types";
import type { WorkspaceAction } from "./workspaceState";

type HistoryNavigationOptions = {
  dispatch: Dispatch<WorkspaceAction>;
  viewRequests: LatestRequest;
};

export function useHistoryNavigation({
  dispatch,
  viewRequests,
}: HistoryNavigationOptions) {
  const { t } = useI18n();
  const [history, setHistory] = useState<HistorySummary[]>([]);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [deletingIds, setDeletingIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const deletingIdsRef = useRef(new Set<string>());
  const historyRequests = useRef(new AbortableLatestRequest());

  const refreshHistory = useCallback(() => {
    const request = historyRequests.current.begin();
    void api.history(request.signal)
      .then((next) => {
        if (historyRequests.current.isCurrent(request.id)) setHistory(next);
      })
      .catch(() => {
        // Preserve the last known-good list on cancellation or transient failure.
      })
      .finally(() => {
        historyRequests.current.settle(request.id);
      });
  }, []);

  useEffect(() => {
    const requests = historyRequests.current;
    refreshHistory();
    return () => {
      requests.invalidate();
    };
  }, [refreshHistory]);

  const newAnalysis = useCallback(() => {
    viewRequests.invalidate();
    dispatch({ type: "new-analysis" });
  }, [dispatch, viewRequests]);

  const selectHistory = useCallback(async (id: string) => {
    const requestId = viewRequests.begin();
    dispatch({
      type: "navigation-started",
      navigation: { kind: "history", requestId, historyId: id },
    });
    try {
      const entry = await api.historyDetail(id);
      let nextJob: JobSnapshot | null = null;
      let nextResult = entry.result;
      if (entry.state === "running" || entry.state === "queued") {
        try {
          nextJob = await api.job(id);
          if (nextJob.state === "completed" && !nextResult) {
            nextResult = await api.jobResult(id);
          }
        } catch {
          nextJob = null;
        }
      }
      if (!viewRequests.isCurrent(requestId)) return;
      dispatch({
        type: "history-loaded",
        requestId,
        historyId: id,
        entry,
        job: nextJob,
        result: nextResult,
        audioUrl: entry.audio_url ? `${API_BASE}${entry.audio_url}` : "",
      });
    } catch (cause) {
      if (!viewRequests.isCurrent(requestId)) return;
      dispatch({
        type: "navigation-failed",
        requestId,
        error: cause instanceof Error ? cause.message : "无法读取历史分析",
      });
    }
  }, [dispatch, viewRequests]);

  const deleteHistory = useCallback(async (item: HistorySummary) => {
    const { id, title } = item;
    if (deletingIdsRef.current.has(id)) return;
    const confirmed = window.confirm(
      `${t("确定永久删除“{{title}}”吗？", { title })}\n\n${t("分析记录和关联的源音频将被删除，此操作无法撤销。")}`,
    );
    if (!confirmed) return;
    deletingIdsRef.current.add(id);
    setDeletingIds(new Set(deletingIdsRef.current));
    dispatch({ type: "comparison-selection-changed" });
    try {
      await api.deleteHistory(id);
      dispatch({ type: "history-deleted", historyId: id });
      setCompareIds((items) => items.filter((item) => item !== id));
      refreshHistory();
    } catch (cause) {
      dispatch({
        type: "set-error",
        error: cause instanceof ApiError && cause.status === 409
          ? "请先取消正在运行的任务"
          : cause instanceof Error ? cause.message : "删除失败",
      });
    } finally {
      deletingIdsRef.current.delete(id);
      setDeletingIds(new Set(deletingIdsRef.current));
    }
  }, [dispatch, refreshHistory, t]);

  const renameHistory = useCallback(async (item: HistorySummary) => {
    const title = window.prompt(t("重命名分析"), item.title)?.trim();
    if (!title || title === item.title) return;
    try {
      await api.renameHistory(item.id, title);
      refreshHistory();
    } catch {
      dispatch({ type: "set-error", error: "重命名失败" });
    }
  }, [dispatch, refreshHistory, t]);

  const toggleCompare = useCallback((id: string) => {
    dispatch({ type: "comparison-selection-changed" });
    setCompareIds((items) => items.includes(id)
      ? items.filter((item) => item !== id)
      : [...items, id].slice(0, 2));
  }, [dispatch]);

  const compareHistory = useCallback(async () => {
    if (compareIds.length !== 2) return;
    const [firstId, secondId] = compareIds;
    const requestId = viewRequests.begin();
    dispatch({
      type: "navigation-started",
      navigation: { kind: "comparison", requestId },
    });
    try {
      const [first, second] = await Promise.all([
        api.historyDetail(firstId),
        api.historyDetail(secondId),
      ]);
      if (!viewRequests.isCurrent(requestId)) return;
      dispatch({
        type: "comparison-loaded",
        requestId,
        entries: [first, second],
      });
    } catch (cause) {
      if (!viewRequests.isCurrent(requestId)) return;
      dispatch({
        type: "navigation-failed",
        requestId,
        error: cause instanceof Error ? cause.message : "无法读取对比结果",
      });
    }
  }, [compareIds, dispatch, viewRequests]);

  const invalidateRequests = useCallback(() => {
    viewRequests.invalidate();
    historyRequests.current.invalidate();
  }, [viewRequests]);

  return {
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
  };
}
