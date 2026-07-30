import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type Dispatch,
} from "react";
import { api } from "../../api";
import { LatestRequest } from "../../hooks/latestRequest";
import type { RuntimeConfig } from "../../types";
import type { WorkspaceAction } from "./workspaceState";

type ModelSource = "network" | "local";

type AnalysisSubmissionOptions = {
  file: File | null;
  inputSource: "file" | "url";
  remoteUrl: string;
  language: string;
  modelSource: ModelSource;
  modelEndpoint: string;
  localModelPath: string;
  runtimeConfig: RuntimeConfig | null;
  dispatch: Dispatch<WorkspaceAction>;
  viewRequests: LatestRequest;
  refreshHistory: () => void;
};

function analysisForm({
  file,
  language,
  modelSource,
  modelEndpoint,
  localModelPath,
  localModelRoot,
}: {
  file: File;
  language: string;
  modelSource: ModelSource;
  modelEndpoint: string;
  localModelPath: string;
  localModelRoot: string;
}): FormData {
  const form = new FormData();
  form.append("file", file);
  if (language !== "auto") form.append("language", language);
  form.append("model_source", modelSource);
  if (modelSource === "network" && modelEndpoint.trim()) {
    form.append("model_endpoint", modelEndpoint.trim());
  }
  if (modelSource === "local") {
    form.append("local_model_path", localModelPath.trim() || localModelRoot);
  }
  return form;
}

export function useAnalysisSubmission({
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
}: AnalysisSubmissionOptions) {
  const [creatingJob, setCreatingJob] = useState(false);
  const creatingJobRef = useRef(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const analyze = useCallback(async () => {
    const remote = remoteUrl.trim();
    if (
      creatingJobRef.current
      || (inputSource === "file" && !file)
      || (inputSource === "url" && !remote)
    ) return;
    creatingJobRef.current = true;
    setCreatingJob(true);
    const requestId = viewRequests.begin();
    if (inputSource === "url") {
      dispatch({
        type: "remote-chosen",
        fileName: remoteFileName(remote),
      });
    }
    dispatch({ type: "analysis-started" });

    try {
      const snapshot = inputSource === "url"
        ? await api.createJobFromUrl({
            url: remote,
            language: language === "auto" ? null : language,
            model_source: modelSource,
            model_endpoint: modelSource === "network" && modelEndpoint.trim()
              ? modelEndpoint.trim()
              : null,
            local_model_path: modelSource === "local"
              ? localModelPath.trim()
                || runtimeConfig?.local_model_root
                || "src/model"
              : null,
          })
        : await api.createJob(analysisForm({
            file: file!,
            language,
            modelSource,
            modelEndpoint,
            localModelPath,
            localModelRoot: runtimeConfig?.local_model_root || "src/model",
          }));
      if (!viewRequests.isCurrent(requestId)) {
        refreshHistory();
        return;
      }
      dispatch({ type: "job-created", snapshot });
      refreshHistory();
    } catch (cause) {
      if (viewRequests.isCurrent(requestId)) {
        dispatch({
          type: "set-error",
          error: cause instanceof Error ? cause.message : "无法创建分析任务",
        });
      }
    } finally {
      creatingJobRef.current = false;
      if (mountedRef.current) setCreatingJob(false);
    }
  }, [
    dispatch,
    file,
    inputSource,
    remoteUrl,
    runtimeConfig?.local_model_root,
    language,
    localModelPath,
    modelEndpoint,
    modelSource,
    refreshHistory,
    viewRequests,
  ]);

  return { analyze, creatingJob };
}

function remoteFileName(value: string): string {
  try {
    const name = decodeURIComponent(
      new URL(value).pathname.split("/").filter(Boolean).pop() || "",
    );
    return name || "远程音频";
  } catch {
    return "远程音频";
  }
}
