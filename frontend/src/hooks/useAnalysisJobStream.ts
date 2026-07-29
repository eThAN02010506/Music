import { useEffect, useRef, useState } from "react";
import { api, API_BASE } from "../api";
import type { JobSnapshot } from "../types";
import {
  AnalysisJobStreamController,
  type EventSourceLike,
} from "./analysisJobStreamController";

type JobStreamOptions = {
  jobId: string | null;
  enabled: boolean;
  onSnapshot: (snapshot: JobSnapshot) => void;
  onTerminal: (snapshot: JobSnapshot) => void;
  onHistoryRefresh: () => void;
};

export function useAnalysisJobStream({
  jobId,
  enabled,
  onSnapshot,
  onTerminal,
  onHistoryRefresh,
}: JobStreamOptions) {
  const callbacksRef = useRef({
    onSnapshot,
    onTerminal,
    onHistoryRefresh,
  });
  const [connectionWarning, setConnectionWarning] = useState("");

  useEffect(() => {
    callbacksRef.current = { onSnapshot, onTerminal, onHistoryRefresh };
  }, [onHistoryRefresh, onSnapshot, onTerminal]);

  useEffect(() => {
    if (!jobId || !enabled) {
      setConnectionWarning("");
      return;
    }

    const controller = new AnalysisJobStreamController(
      jobId,
      `${API_BASE}/jobs/${encodeURIComponent(jobId)}/events`,
      {
        createEventSource: (url) => new EventSource(
          url,
          { withCredentials: true },
        ) as unknown as EventSourceLike,
        fetchSnapshot: api.job,
        setTimer: (callback, delay) => window.setTimeout(callback, delay),
        clearTimer: (handle) => window.clearTimeout(handle as number),
        now: Date.now,
      },
      {
        onSnapshot: (snapshot) => callbacksRef.current.onSnapshot(snapshot),
        onTerminal: (snapshot) => callbacksRef.current.onTerminal(snapshot),
        onHistoryRefresh: () => callbacksRef.current.onHistoryRefresh(),
        onWarning: setConnectionWarning,
      },
    );
    controller.start();
    return () => controller.dispose();
  }, [enabled, jobId]);

  return connectionWarning;
}
