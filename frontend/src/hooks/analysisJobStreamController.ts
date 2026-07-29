import type { JobSnapshot } from "../types";
import {
  HistoryRefreshGate,
  isTerminalJob,
  reconnectDelay,
} from "./jobStreamPolicy.ts";

export interface StreamEventLike {
  data: string;
}

export interface EventSourceLike {
  onopen: (() => void) | null;
  onerror: (() => void) | null;
  addEventListener(
    type: "progress",
    listener: (event: StreamEventLike) => void,
  ): void;
  close(): void;
}

export type TimerHandle = unknown;

export interface JobStreamDependencies {
  createEventSource: (url: string) => EventSourceLike;
  fetchSnapshot: (jobId: string) => Promise<JobSnapshot>;
  setTimer: (callback: () => void, delay: number) => TimerHandle;
  clearTimer: (handle: TimerHandle) => void;
  now: () => number;
}

export interface JobStreamCallbacks {
  onSnapshot: (snapshot: JobSnapshot) => void;
  onTerminal: (snapshot: JobSnapshot) => void;
  onHistoryRefresh: () => void;
  onWarning: (warning: string) => void;
}

const NOOP_CALLBACKS: JobStreamCallbacks = {
  onSnapshot: () => {},
  onTerminal: () => {},
  onHistoryRefresh: () => {},
  onWarning: () => {},
};

export class AnalysisJobStreamController {
  private readonly jobId: string;
  private readonly streamUrl: string;
  private readonly dependencies: JobStreamDependencies;
  private callbacks: JobStreamCallbacks;
  private source: EventSourceLike | null = null;
  private reconnectTimer: TimerHandle | null = null;
  private pollTimer: TimerHandle | null = null;
  private reconnectAttempt = 0;
  private fallbackPolling = false;
  private lastRevision = -1;
  private disposed = false;
  private terminal = false;
  private readonly historyGate = new HistoryRefreshGate();

  constructor(
    jobId: string,
    streamUrl: string,
    dependencies: JobStreamDependencies,
    callbacks: Partial<JobStreamCallbacks> = {},
  ) {
    this.jobId = jobId;
    this.streamUrl = streamUrl;
    this.dependencies = dependencies;
    this.callbacks = { ...NOOP_CALLBACKS, ...callbacks };
  }

  setCallbacks(callbacks: Partial<JobStreamCallbacks>): void {
    this.callbacks = { ...this.callbacks, ...callbacks };
  }

  start(): void {
    if (this.disposed || this.terminal || this.source) return;
    this.connect();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.source?.close();
    this.source = null;
    this.clearTimers();
  }

  private connect(): void {
    if (this.disposed || this.terminal) return;
    this.source?.close();
    const next = this.dependencies.createEventSource(this.streamUrl);
    this.source = next;
    next.onopen = () => {
      if (!this.isCurrentSource(next)) return;
      this.reconnectAttempt = 0;
      this.fallbackPolling = false;
      this.clearPoll();
      this.callbacks.onWarning("");
    };
    next.addEventListener("progress", (event) => {
      if (!this.isCurrentSource(next)) return;
      try {
        this.accept(JSON.parse(event.data) as JobSnapshot);
      } catch {
        this.callbacks.onWarning("收到无法解析的进度消息，正在重新同步…");
        this.schedulePoll(0, true);
      }
    });
    next.onerror = () => {
      if (!this.isCurrentSource(next) || this.terminal) return;
      next.close();
      this.source = null;
      this.fallbackPolling = true;
      this.callbacks.onWarning("进度连接暂时中断，正在自动恢复…");
      this.schedulePoll(0, true);
      const delay = reconnectDelay(this.reconnectAttempt);
      this.reconnectAttempt += 1;
      this.clearReconnect();
      this.reconnectTimer = this.dependencies.setTimer(() => {
        this.reconnectTimer = null;
        this.connect();
      }, delay);
    };
  }

  private accept(snapshot: JobSnapshot): void {
    if (
      this.disposed
      || this.terminal
      || snapshot.id !== this.jobId
      || snapshot.revision <= this.lastRevision
    ) return;
    this.lastRevision = snapshot.revision;
    this.callbacks.onSnapshot(snapshot);
    const done = isTerminalJob(snapshot);
    if (this.historyGate.shouldRefresh(this.dependencies.now(), done)) {
      this.callbacks.onHistoryRefresh();
    }
    if (!done) return;
    this.terminal = true;
    this.source?.close();
    this.source = null;
    this.clearTimers();
    this.callbacks.onWarning("");
    this.callbacks.onTerminal(snapshot);
  }

  private schedulePoll(delay = 800, replace = false): void {
    if (this.disposed || this.terminal) return;
    if (replace) this.clearPoll();
    if (this.pollTimer !== null) return;
    this.pollTimer = this.dependencies.setTimer(() => {
      this.pollTimer = null;
      void this.poll();
    }, delay);
  }

  private async poll(): Promise<void> {
    if (this.disposed || this.terminal) return;
    try {
      const snapshot = await this.dependencies.fetchSnapshot(this.jobId);
      if (this.disposed || this.terminal) return;
      this.callbacks.onWarning("");
      this.accept(snapshot);
    } catch {
      if (!this.disposed && !this.terminal) {
        this.callbacks.onWarning("进度连接暂时中断，正在自动恢复…");
      }
    }
    if (!this.disposed && !this.terminal && this.fallbackPolling) {
      this.schedulePoll(2_000);
    }
  }

  private isCurrentSource(source: EventSourceLike): boolean {
    return !this.disposed && this.source === source;
  }

  private clearPoll(): void {
    if (this.pollTimer === null) return;
    this.dependencies.clearTimer(this.pollTimer);
    this.pollTimer = null;
  }

  private clearReconnect(): void {
    if (this.reconnectTimer === null) return;
    this.dependencies.clearTimer(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private clearTimers(): void {
    this.clearPoll();
    this.clearReconnect();
  }
}
