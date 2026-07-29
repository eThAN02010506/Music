import assert from "node:assert/strict";
import test from "node:test";

import {
  AnalysisJobStreamController,
  type EventSourceLike,
  type JobStreamDependencies,
  type StreamEventLike,
  type TimerHandle,
} from "../src/hooks/analysisJobStreamController.ts";
import type { JobSnapshot } from "../src/types.ts";

class FakeSource implements EventSourceLike {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  progress: ((event: StreamEventLike) => void) | null = null;
  closes = 0;

  addEventListener(_type: "progress", listener: (event: StreamEventLike) => void) {
    this.progress = listener;
  }

  close() {
    this.closes += 1;
  }

  emit(snapshot: unknown) {
    this.progress?.({ data: JSON.stringify(snapshot) });
  }
}

class FakeTimers {
  private sequence = 0;
  readonly pending = new Map<number, { callback: () => void; delay: number }>();

  set = (callback: () => void, delay: number): TimerHandle => {
    const id = ++this.sequence;
    this.pending.set(id, { callback, delay });
    return id;
  };

  clear = (handle: TimerHandle) => {
    this.pending.delete(handle as number);
  };

  runDelay(delay: number) {
    const match = [...this.pending].find(([, timer]) => timer.delay === delay);
    assert.ok(match, `missing timer with delay ${delay}`);
    this.pending.delete(match[0]);
    match[1].callback();
  }

  delays() {
    return [...this.pending.values()].map((timer) => timer.delay).sort((a, b) => a - b);
  }
}

function snapshot(
  revision: number,
  state: JobSnapshot["state"] = "running",
  id = "job-1",
): JobSnapshot {
  return {
    id,
    state,
    stage: state,
    progress: state === "completed" ? 1 : 0.5,
    message: state,
    result_url: state === "completed" ? "/result" : null,
    error: null,
    persistence_error: null,
    revision,
  };
}

function harness(fetchSnapshot: () => Promise<JobSnapshot> = async () => snapshot(2)) {
  const timers = new FakeTimers();
  const sources: FakeSource[] = [];
  let now = 10_000;
  const dependencies: JobStreamDependencies = {
    createEventSource: () => {
      const source = new FakeSource();
      sources.push(source);
      return source;
    },
    fetchSnapshot: async () => fetchSnapshot(),
    setTimer: timers.set,
    clearTimer: timers.clear,
    now: () => now,
  };
  return {
    timers,
    sources,
    dependencies,
    advance: (amount: number) => { now += amount; },
  };
}

test("stream accepts increasing revisions for its job and ignores duplicates", () => {
  const testbed = harness();
  const accepted: number[] = [];
  const controller = new AnalysisJobStreamController(
    "job-1",
    "/events",
    testbed.dependencies,
    { onSnapshot: (item) => accepted.push(item.revision) },
  );
  controller.start();
  const source = testbed.sources[0];

  source.emit(snapshot(1));
  source.emit(snapshot(1));
  source.emit(snapshot(0));
  source.emit(snapshot(2, "running", "other"));
  source.emit(snapshot(2));

  assert.deepEqual(accepted, [1, 2]);
});

test("terminal snapshot closes once and invokes terminal/history once", () => {
  const testbed = harness();
  let terminals = 0;
  let refreshes = 0;
  const controller = new AnalysisJobStreamController(
    "job-1",
    "/events",
    testbed.dependencies,
    {
      onTerminal: () => { terminals += 1; },
      onHistoryRefresh: () => { refreshes += 1; },
    },
  );
  controller.start();
  const source = testbed.sources[0];
  source.emit(snapshot(3, "completed"));
  source.emit(snapshot(4, "completed"));

  assert.equal(terminals, 1);
  assert.equal(refreshes, 1);
  assert.equal(source.closes, 1);
  assert.deepEqual(testbed.timers.delays(), []);
});

test("connection error starts immediate polling and bounded reconnect", async () => {
  const testbed = harness(async () => snapshot(2));
  const accepted: number[] = [];
  const warnings: string[] = [];
  const controller = new AnalysisJobStreamController(
    "job-1",
    "/events",
    testbed.dependencies,
    {
      onSnapshot: (item) => accepted.push(item.revision),
      onWarning: (warning) => warnings.push(warning),
    },
  );
  controller.start();
  testbed.sources[0].onerror?.();
  assert.deepEqual(testbed.timers.delays(), [0, 1_000]);

  testbed.timers.runDelay(0);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(accepted, [2]);
  assert.ok(warnings.some(Boolean));
  assert.equal(warnings.at(-1), "");
  assert.deepEqual(testbed.timers.delays(), [1_000, 2_000]);
});

test("reconnect replaces the source and stale source events are ignored", () => {
  const testbed = harness();
  const accepted: number[] = [];
  const controller = new AnalysisJobStreamController(
    "job-1",
    "/events",
    testbed.dependencies,
    { onSnapshot: (item) => accepted.push(item.revision) },
  );
  controller.start();
  const oldSource = testbed.sources[0];
  oldSource.onerror?.();
  testbed.timers.runDelay(1_000);
  const newSource = testbed.sources[1];
  oldSource.emit(snapshot(4));
  newSource.emit(snapshot(1));

  assert.deepEqual(accepted, [1]);
  assert.equal(oldSource.closes, 1);
});

test("dispose closes the source, clears timers, and ignores a late poll", async () => {
  let resolvePoll!: (value: JobSnapshot) => void;
  const pendingPoll = new Promise<JobSnapshot>((resolve) => { resolvePoll = resolve; });
  const testbed = harness(() => pendingPoll);
  const accepted: number[] = [];
  const controller = new AnalysisJobStreamController(
    "job-1",
    "/events",
    testbed.dependencies,
    { onSnapshot: (item) => accepted.push(item.revision) },
  );
  controller.start();
  testbed.sources[0].onerror?.();
  testbed.timers.runDelay(0);
  controller.dispose();
  resolvePoll(snapshot(8));
  await Promise.resolve();
  await Promise.resolve();

  assert.deepEqual(accepted, []);
  assert.deepEqual(testbed.timers.delays(), []);
  assert.equal(testbed.sources[0].closes, 1);
});

test("malformed progress warns and schedules an immediate authoritative poll", () => {
  const testbed = harness();
  const warnings: string[] = [];
  const controller = new AnalysisJobStreamController(
    "job-1",
    "/events",
    testbed.dependencies,
    { onWarning: (warning) => warnings.push(warning) },
  );
  controller.start();
  testbed.sources[0].progress?.({ data: "not-json" });

  assert.match(warnings[0], /无法解析/);
  assert.deepEqual(testbed.timers.delays(), [0]);
});
