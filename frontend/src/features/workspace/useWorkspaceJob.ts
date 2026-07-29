import { useCallback, type Dispatch } from "react";
import { api } from "../../api";
import { isTerminalJob } from "../../hooks/jobStreamPolicy";
import { useAnalysisJobStream } from "../../hooks/useAnalysisJobStream";
import type { JobSnapshot, LyricsSegment } from "../../types";
import type { WorkspaceAction } from "./workspaceState";

type WorkspaceJobOptions = {
  job: JobSnapshot | null;
  activeHistoryId: string | null;
  dispatch: Dispatch<WorkspaceAction>;
  refreshHistory: () => void;
};

export function useWorkspaceJob({
  job,
  activeHistoryId,
  dispatch,
  refreshHistory,
}: WorkspaceJobOptions) {
  const handleSnapshot = useCallback((snapshot: JobSnapshot) => {
    dispatch({ type: "job-snapshot", snapshot });
  }, [dispatch]);

  const handleTerminal = useCallback((snapshot: JobSnapshot) => {
    if (snapshot.state !== "completed") return;
    void api.jobResult(snapshot.id)
      .then((result) => {
        dispatch({ type: "job-result", jobId: snapshot.id, result });
      })
      .catch((cause) => {
        dispatch({
          type: "scoped-error",
          historyId: snapshot.id,
          error: cause instanceof Error ? cause.message : "无法读取分析结果",
        });
      });
  }, [dispatch]);

  const jobBusy = Boolean(job && !isTerminalJob(job));
  const connectionWarning = useAnalysisJobStream({
    jobId: job?.id || null,
    enabled: jobBusy,
    onSnapshot: handleSnapshot,
    onTerminal: handleTerminal,
    onHistoryRefresh: refreshHistory,
  });

  const cancel = useCallback(async () => {
    if (!job) return;
    const jobId = job.id;
    try {
      const snapshot = await api.cancelJob(jobId);
      dispatch({ type: "job-snapshot", snapshot });
    } catch (cause) {
      dispatch({
        type: "scoped-error",
        historyId: jobId,
        error: cause instanceof Error ? cause.message : "取消失败",
      });
    }
  }, [dispatch, job]);

  const saveLyrics = useCallback(async (lyrics: LyricsSegment[]) => {
    if (!activeHistoryId) {
      throw new Error("当前分析尚未保存到历史记录。");
    }
    const historyId = activeHistoryId;
    const entry = await api.updateLyrics(historyId, lyrics);
    if (!entry.result) throw new Error("后端没有返回修订结果。");
    dispatch({
      type: "lyrics-saved",
      historyId,
      result: entry.result,
      revisionCount: entry.revision_count,
    });
    refreshHistory();
  }, [activeHistoryId, dispatch, refreshHistory]);

  return {
    jobBusy,
    connectionWarning,
    cancel,
    saveLyrics,
  };
}
