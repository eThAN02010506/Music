import type {
  AnalysisResult,
  HistoryDetail,
  JobSnapshot,
} from "../../types";

export type StartView = {
  kind: "start";
  file: File | null;
  fileName: string;
  historyId: string | null;
  revisionCount: number;
};

export type HistoryView = {
  kind: "history";
  id: string;
  fileName: string;
  audioUrl: string;
  revisionCount: number;
};

export type ComparisonView = {
  kind: "comparison";
  entries: [HistoryDetail, HistoryDetail];
};

export type WorkspaceView = StartView | HistoryView | ComparisonView;

export type PendingNavigation =
  | { kind: "history"; requestId: number; historyId: string }
  | { kind: "comparison"; requestId: number };

export type WorkspaceState = {
  view: WorkspaceView;
  pendingNavigation: PendingNavigation | null;
  job: JobSnapshot | null;
  result: AnalysisResult | null;
  error: string;
};

export const initialWorkspaceState: WorkspaceState = {
  view: {
    kind: "start",
    file: null,
    fileName: "",
    historyId: null,
    revisionCount: 0,
  },
  pendingNavigation: null,
  job: null,
  result: null,
  error: "",
};

export type WorkspaceAction =
  | { type: "file-chosen"; file: File }
  | { type: "remote-chosen"; fileName: string }
  | { type: "new-analysis" }
  | { type: "analysis-started" }
  | { type: "job-created"; snapshot: JobSnapshot }
  | { type: "job-snapshot"; snapshot: JobSnapshot }
  | { type: "job-result"; jobId: string; result: AnalysisResult }
  | {
      type: "navigation-started";
      navigation: PendingNavigation;
    }
  | {
      type: "history-loaded";
      requestId: number;
      historyId: string;
      entry: HistoryDetail;
      job: JobSnapshot | null;
      result: AnalysisResult | null;
      audioUrl: string;
    }
  | {
      type: "comparison-loaded";
      requestId: number;
      entries: [HistoryDetail, HistoryDetail];
    }
  | { type: "navigation-failed"; requestId: number; error: string }
  | { type: "history-deleted"; historyId: string }
  | { type: "comparison-selection-changed" }
  | {
      type: "lyrics-saved";
      historyId: string;
      result: AnalysisResult;
      revisionCount: number;
    }
  | { type: "scoped-error"; historyId: string; error: string }
  | { type: "set-error"; error: string }
  | { type: "clear-error" };

export function activeHistoryId(state: WorkspaceState): string | null {
  if (state.view.kind === "history") return state.view.id;
  if (state.view.kind === "start") return state.view.historyId;
  return null;
}

export function sidebarHistoryId(state: WorkspaceState): string | null {
  return state.pendingNavigation?.kind === "history"
    ? state.pendingNavigation.historyId
    : activeHistoryId(state);
}

export function selectedFile(state: WorkspaceState): File | null {
  return state.view.kind === "start" ? state.view.file : null;
}

export function selectedFileName(state: WorkspaceState): string {
  return state.view.kind === "comparison" ? "" : state.view.fileName;
}

export function selectedRevisionCount(state: WorkspaceState): number {
  return state.view.kind === "comparison" ? 0 : state.view.revisionCount;
}

export function historyAudioUrl(state: WorkspaceState): string {
  return state.view.kind === "history" ? state.view.audioUrl : "";
}

export function comparisonEntries(state: WorkspaceState): HistoryDetail[] {
  return state.view.kind === "comparison" ? state.view.entries : [];
}

function resetState(error = ""): WorkspaceState {
  return {
    ...initialWorkspaceState,
    error,
  };
}

function withRevisionCount(
  view: WorkspaceView,
  revisionCount: number,
): WorkspaceView {
  if (view.kind === "comparison") return view;
  return { ...view, revisionCount };
}

function isCurrentNavigation(
  state: WorkspaceState,
  requestId: number,
  kind?: PendingNavigation["kind"],
): boolean {
  return Boolean(
    state.pendingNavigation
    && state.pendingNavigation.requestId === requestId
    && (!kind || state.pendingNavigation.kind === kind),
  );
}

export function workspaceReducer(
  state: WorkspaceState,
  action: WorkspaceAction,
): WorkspaceState {
  switch (action.type) {
    case "file-chosen":
      return {
        view: {
          kind: "start",
          file: action.file,
          fileName: action.file.name,
          historyId: null,
          revisionCount: 0,
        },
        pendingNavigation: null,
        job: null,
        result: null,
        error: "",
      };
    case "remote-chosen":
      return {
        view: {
          kind: "start",
          file: null,
          fileName: action.fileName,
          historyId: null,
          revisionCount: 0,
        },
        pendingNavigation: null,
        job: null,
        result: null,
        error: "",
      };
    case "new-analysis":
      return resetState();
    case "analysis-started":
      if (state.view.kind !== "start" || !state.view.fileName) return state;
      return {
        ...state,
        view: {
          ...state.view,
          historyId: null,
          revisionCount: 0,
        },
        pendingNavigation: null,
        job: null,
        result: null,
        error: "",
      };
    case "job-created":
      if (state.view.kind !== "start" || !state.view.fileName) return state;
      return {
        ...state,
        view: {
          ...state.view,
          historyId: action.snapshot.id,
          revisionCount: 0,
        },
        job: action.snapshot,
      };
    case "job-snapshot": {
      if (activeHistoryId(state) !== action.snapshot.id) return state;
      return {
        ...state,
        job: action.snapshot,
        error: action.snapshot.state === "failed"
          ? action.snapshot.error || "分析失败"
          : state.error,
      };
    }
    case "job-result":
      if (activeHistoryId(state) !== action.jobId) return state;
      return {
        ...state,
        // Do not reset revisionCount: history-loaded already populated it
        // from entry.revision_count, and a job result must not clear the
        // revision list the user can already see.
        result: action.result,
      };
    case "navigation-started":
      return {
        ...state,
        pendingNavigation: action.navigation,
        error: "",
      };
    case "history-loaded":
      if (
        !isCurrentNavigation(state, action.requestId, "history")
        || state.pendingNavigation?.kind !== "history"
        || state.pendingNavigation.historyId !== action.historyId
      ) {
        return state;
      }
      return {
        view: {
          kind: "history",
          id: action.historyId,
          fileName: action.entry.file_name,
          audioUrl: action.audioUrl,
          revisionCount: action.entry.revision_count,
        },
        pendingNavigation: null,
        job: action.job,
        result: action.result,
        error: "",
      };
    case "comparison-loaded":
      if (!isCurrentNavigation(state, action.requestId, "comparison")) {
        return state;
      }
      return {
        view: { kind: "comparison", entries: action.entries },
        pendingNavigation: null,
        job: null,
        result: null,
        error: "",
      };
    case "navigation-failed":
      if (!isCurrentNavigation(state, action.requestId)) return state;
      return {
        ...state,
        pendingNavigation: null,
        error: action.error,
      };
    case "history-deleted": {
      const viewingDeletedHistory = activeHistoryId(state) === action.historyId;
      const comparingDeletedHistory = state.view.kind === "comparison"
        && state.view.entries.some((entry) => entry.id === action.historyId);
      const loadingDeletedHistory = state.pendingNavigation?.kind === "history"
        && state.pendingNavigation.historyId === action.historyId;
      return viewingDeletedHistory || comparingDeletedHistory || loadingDeletedHistory
        ? resetState()
        : state;
    }
    case "comparison-selection-changed":
      return state.pendingNavigation?.kind === "comparison"
        ? { ...state, pendingNavigation: null }
        : state;
    case "lyrics-saved":
      if (activeHistoryId(state) !== action.historyId) return state;
      return {
        ...state,
        view: withRevisionCount(state.view, action.revisionCount),
        result: action.result,
      };
    case "scoped-error":
      return activeHistoryId(state) === action.historyId
        ? { ...state, error: action.error }
        : state;
    case "set-error":
      return { ...state, error: action.error };
    case "clear-error":
      return state.error ? { ...state, error: "" } : state;
  }
}
