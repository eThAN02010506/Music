import type { JobSnapshot } from "../types";

const TERMINAL_STATES = new Set(["completed", "failed", "cancelled"]);

export function isTerminalJob(job: JobSnapshot): boolean {
  return TERMINAL_STATES.has(job.state);
}

export function reconnectDelay(attempt: number): number {
  return Math.min(1_000 * (2 ** Math.max(0, attempt)), 15_000);
}

export class HistoryRefreshGate {
  private lastRefreshAt = Number.NEGATIVE_INFINITY;
  private readonly intervalMs: number;

  constructor(intervalMs = 5_000) {
    this.intervalMs = intervalMs;
  }

  shouldRefresh(now: number, terminal = false): boolean {
    if (!terminal && now - this.lastRefreshAt < this.intervalMs) return false;
    this.lastRefreshAt = now;
    return true;
  }
}
