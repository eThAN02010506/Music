export const HEALTH_RETRY_DELAYS_MS = [1_000, 3_000, 10_000] as const;

export type HealthCheckSnapshot<T> = {
  value: T | null;
  checked: boolean;
  checking: boolean;
};

type TimerHandle = unknown;

export type HealthCheckDependencies<T> = {
  probe: (signal: AbortSignal) => Promise<T>;
  setTimer: (callback: () => void, delay: number) => TimerHandle;
  clearTimer: (handle: TimerHandle) => void;
};

export class HealthCheckController<T> {
  private readonly dependencies: HealthCheckDependencies<T>;
  private readonly onSnapshot: (snapshot: HealthCheckSnapshot<T>) => void;
  private snapshot: HealthCheckSnapshot<T> = {
    value: null,
    checked: false,
    checking: false,
  };
  private retryIndex = 0;
  private generation = 0;
  private timer: TimerHandle | null = null;
  private controller: AbortController | null = null;
  private disposed = false;

  constructor(
    dependencies: HealthCheckDependencies<T>,
    onSnapshot: (snapshot: HealthCheckSnapshot<T>) => void,
  ) {
    this.dependencies = dependencies;
    this.onSnapshot = onSnapshot;
  }

  start(): void {
    if (this.disposed || this.snapshot.checking || this.snapshot.checked) return;
    void this.check();
  }

  retryNow(): void {
    if (this.disposed) return;
    this.retryIndex = 0;
    this.clearRetry();
    this.controller?.abort();
    void this.check();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.generation += 1;
    this.clearRetry();
    this.controller?.abort();
    this.controller = null;
  }

  private async check(): Promise<void> {
    if (this.disposed) return;
    const generation = ++this.generation;
    const controller = new AbortController();
    this.controller = controller;
    this.emit({ ...this.snapshot, checking: true });
    try {
      const value = await this.dependencies.probe(controller.signal);
      if (!this.isCurrent(generation)) return;
      this.retryIndex = 0;
      this.emit({ value, checked: true, checking: false });
    } catch (cause) {
      if (!this.isCurrent(generation) || isAbortFailure(cause)) return;
      this.emit({ value: null, checked: true, checking: false });
      this.scheduleRetry();
    } finally {
      if (this.isCurrent(generation)) this.controller = null;
    }
  }

  private scheduleRetry(): void {
    if (this.disposed || this.timer !== null) return;
    const delay = HEALTH_RETRY_DELAYS_MS[this.retryIndex];
    if (delay === undefined) return;
    this.retryIndex += 1;
    this.timer = this.dependencies.setTimer(() => {
      this.timer = null;
      void this.check();
    }, delay);
  }

  private clearRetry(): void {
    if (this.timer === null) return;
    this.dependencies.clearTimer(this.timer);
    this.timer = null;
  }

  private isCurrent(generation: number): boolean {
    return !this.disposed && generation === this.generation;
  }

  private emit(snapshot: HealthCheckSnapshot<T>): void {
    if (this.disposed) return;
    this.snapshot = snapshot;
    this.onSnapshot(snapshot);
  }
}

function isAbortFailure(cause: unknown): boolean {
  return (
    cause instanceof DOMException && cause.name === "AbortError"
  ) || (
    typeof cause === "object"
    && cause !== null
    && "name" in cause
    && cause.name === "AbortError"
  );
}
