import type {
  AnalysisResult,
  HealthResult,
  HistoryDetail,
  HistorySummary,
  JobSnapshot,
} from "./types";

export const API_BASE = (
  import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000"
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    const detail = await response.text();
    throw new ApiError(`后端返回 HTTP ${response.status}: ${detail}`, response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<HealthResult>("/health"),
  history: () => request<HistorySummary[]>("/history"),
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
};
