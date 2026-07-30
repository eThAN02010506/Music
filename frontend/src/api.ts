import type {
  AnalysisResult,
  HealthResult,
  HistoryDetail,
  HistoryRevision,
  HistorySummary,
  HistoryWaveform,
  JobSnapshot,
  Leaderboard,
  LyricsRetryResult,
  ModelProbeResult,
  RuntimeConfig,
  SingingAttempt,
  SingingAttemptCursor,
  SingingScore,
  StemStatus,
  ListenerProfile,
  ListenerLevel,
  TeachingChatRequest,
  TeachingConversation,
  TeachingGuideResponse,
  TeachingGuideStrategy,
  TeachingMessage,
  User,
} from "./types";
import { formatApiErrorDetail } from "./apiError";

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
    const detail = formatApiErrorDetail(
      await response.text(),
      response.statusText,
    );
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
  updateLeaderboardVisibility: (leaderboardVisible: boolean) =>
    request<User>("/auth/me", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ leaderboard_visible: leaderboardVisible }),
    }),
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
  leaderboard: (signal?: AbortSignal) =>
    request<Leaderboard>("/leaderboard", undefined, { signal }),
  singingAttempts: (
    limit = 50,
    cursor: SingingAttemptCursor | null = null,
    signal?: AbortSignal,
  ) => {
    const query = new URLSearchParams({ limit: String(limit) });
    if (cursor) {
      query.set("before_created_at", cursor.created_at);
      query.set("before_id", cursor.id);
    }
    return request<SingingAttempt[]>(
      `/singing/attempts?${query}`,
      undefined,
      { signal },
    );
  },
  deleteSingingAttempt: (id: string) =>
    request<void>(
      `/singing/attempts/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),
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
  createJobFromUrl: (payload: {
    url: string;
    language: string | null;
    model_source: "network" | "local";
    model_endpoint: string | null;
    local_model_path: string | null;
  }) =>
    request<JobSnapshot>("/jobs/from-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
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
  historyWaveform: (id: string, signal?: AbortSignal) =>
    request<HistoryWaveform>(
      `/history/${encodeURIComponent(id)}/waveform`,
      undefined,
      { signal },
    ),
  historyStems: (id: string, signal?: AbortSignal) =>
    request<StemStatus>(
      `/history/${encodeURIComponent(id)}/stems`,
      undefined,
      { signal },
    ),
  generateHistoryStems: (id: string) =>
    request<StemStatus>(
      `/history/${encodeURIComponent(id)}/stems`,
      { method: "POST" },
    ),
  listenerProfile: (signal?: AbortSignal) =>
    request<ListenerProfile>("/listener-profile", undefined, { signal }),
  updateListenerProfile: (
    level: ListenerLevel,
    preferences: Record<string, string>,
    learned_concepts: string[],
  ) =>
    request<ListenerProfile>("/listener-profile", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level, preferences, learned_concepts }),
    }),
  teachingGuide: (id: string, signal?: AbortSignal) =>
    request<TeachingGuideResponse>(
      `/history/${encodeURIComponent(id)}/teaching-guide`,
      undefined,
      { signal },
    ),
  generateTeachingGuide: (
    id: string,
    options: {
      force?: boolean;
      strategy?: TeachingGuideStrategy;
    } = {},
  ) =>
    request<TeachingGuideResponse>(
      `/history/${encodeURIComponent(id)}/teaching-guide`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          force: options.force ?? false,
          strategy: options.strategy ?? "model",
        }),
      },
    ),
  teachingConversations: (id: string, signal?: AbortSignal) =>
    request<TeachingConversation[]>(
      `/history/${encodeURIComponent(id)}/conversations`,
      undefined,
      { signal },
    ),
  createTeachingConversation: (id: string, title?: string) =>
    request<TeachingConversation>(
      `/history/${encodeURIComponent(id)}/conversations`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title || null }),
      },
    ),
  teachingConversation: (
    id: string,
    conversationId: string,
    signal?: AbortSignal,
  ) =>
    request<TeachingConversation>(
      `/history/${encodeURIComponent(id)}/conversations/${
        encodeURIComponent(conversationId)
      }`,
      undefined,
      { signal },
    ),
  deleteTeachingConversation: (id: string, conversationId: string) =>
    request<void>(
      `/history/${encodeURIComponent(id)}/conversations/${
        encodeURIComponent(conversationId)
      }`,
      { method: "DELETE" },
    ),
  teachingMessages: (
    id: string,
    conversationId: string,
    signal?: AbortSignal,
  ) =>
    request<TeachingMessage[]>(
      `/history/${encodeURIComponent(id)}/conversations/${
        encodeURIComponent(conversationId)
      }/messages`,
      undefined,
      { signal },
    ),
  sendTeachingMessage: (
    id: string,
    conversationId: string,
    payload: TeachingChatRequest,
    signal?: AbortSignal,
  ) =>
    request<TeachingMessage>(
      `/history/${encodeURIComponent(id)}/conversations/${
        encodeURIComponent(conversationId)
      }/messages`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
      { signal },
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
