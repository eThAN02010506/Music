import type {
  AnalysisResult,
  HealthResult,
  HistoryDetail,
  HistoryRevision,
  HistorySummary,
  JobSnapshot,
  Leaderboard,
  LyricsRetryResult,
  ModelProbeResult,
  RuntimeConfig,
  SingingAttempt,
  SingingScore,
  User,
} from "./types";

export const API_BASE = (
  import.meta.env.VITE_API_BASE_URL || "/api"
).replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail: string,
  ) {
    super(message);
  }
}

export function isAbortError(cause: unknown): boolean {
  return (
    cause instanceof DOMException && cause.name === "AbortError"
  ) || (
    typeof cause === "object"
    && cause !== null
    && "name" in cause
    && cause.name === "AbortError"
  );
}

export const AUTH_EXPIRED_EVENT = "music-insight:auth-expired";

type RequestOptions = {
  ignoreAuthExpiry?: boolean;
  signal?: AbortSignal;
};

async function request<T>(
  path: string,
  init?: RequestInit,
  options: RequestOptions = {},
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "include",
    signal: options.signal ?? init?.signal,
  });
  if (!response.ok) {
    const detail = await response.text();
    if (
      response.status === 401
      && !options.ignoreAuthExpiry
      && typeof window !== "undefined"
    ) {
      window.dispatchEvent(new CustomEvent(AUTH_EXPIRED_EVENT));
    }
    throw new ApiError(`后端返回 HTTP ${response.status}: ${detail}`, response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  health: (signal?: AbortSignal) =>
    request<HealthResult>("/health", undefined, { signal }),
  runtimeConfig: (signal?: AbortSignal) =>
    request<RuntimeConfig>("/runtime-config", undefined, { signal }),
  authMe: () =>
    request<User>("/auth/me", undefined, { ignoreAuthExpiry: true }),
  register: (username: string, password: string) =>
    request<User>(
      "/auth/register",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      },
      { ignoreAuthExpiry: true },
    ),
  login: (username: string, password: string) =>
    request<User>(
      "/auth/login",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      },
      { ignoreAuthExpiry: true },
    ),
  logout: () =>
    request<void>("/auth/logout", { method: "POST" }),
  leaderboard: () =>
    request<Leaderboard>("/leaderboard"),
  singingAttempts: () =>
    request<SingingAttempt[]>("/singing/attempts"),
  probeModel: (endpoint: string, signal?: AbortSignal) =>
    request<ModelProbeResult>("/models/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint }),
    }, { signal }),
  history: (signal?: AbortSignal) =>
    request<HistorySummary[]>("/history", undefined, { signal }),
  historyDetail: (id: string) =>
    request<HistoryDetail>(`/history/${encodeURIComponent(id)}`),
  createJob: (form: FormData) =>
    request<JobSnapshot>("/jobs", { method: "POST", body: form }),
  job: (id: string) =>
    request<JobSnapshot>(`/jobs/${encodeURIComponent(id)}`),
  jobResult: (id: string) =>
    request<AnalysisResult>(`/jobs/${encodeURIComponent(id)}/result`),
  cancelJob: (id: string) =>
    request<JobSnapshot>(`/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  deleteHistory: (id: string) =>
    request<void>(`/history/${encodeURIComponent(id)}`, { method: "DELETE" }),
  renameHistory: (id: string, title: string) =>
    request<HistoryDetail>(`/history/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }),
  updateLyrics: (id: string, lyrics: AnalysisResult["lyrics"]) =>
    request<HistoryDetail>(`/history/${encodeURIComponent(id)}/lyrics`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lyrics }),
    }),
  historyRevisions: (id: string) =>
    request<HistoryRevision[]>(
      `/history/${encodeURIComponent(id)}/revisions`,
    ),
  retryLyrics: (id: string, start_s: number, end_s: number) =>
    request<LyricsRetryResult>(
      `/history/${encodeURIComponent(id)}/lyrics/retry`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ start_s, end_s }),
      },
    ),
  scoreSinging: (id: string, file: Blob, fileName: string) => {
    const form = new FormData();
    form.append("file", file, fileName);
    return request<SingingScore>(
      `/history/${encodeURIComponent(id)}/singing/score`,
      { method: "POST", body: form },
    );
  },
  compareSinging: (
    reference: Blob,
    referenceName: string,
    performance: Blob,
    performanceName: string,
  ) => {
    const form = new FormData();
    form.append("reference", reference, referenceName);
    form.append("performance", performance, performanceName);
    return request<SingingScore>("/singing/compare", {
      method: "POST",
      body: form,
    });
  },
};
