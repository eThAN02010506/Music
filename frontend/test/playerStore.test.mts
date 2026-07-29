import assert from "node:assert/strict";
import test from "node:test";

import {
  PlayerStore,
  type PlayerFrameScheduler,
} from "../src/features/player/PlayerStore.ts";

class FakeScheduler implements PlayerFrameScheduler {
  private callbacks = new Map<number, FrameRequestCallback>();
  private nextHandle = 1;
  readonly cancelled: number[] = [];

  request(callback: FrameRequestCallback): number {
    const handle = this.nextHandle++;
    this.callbacks.set(handle, callback);
    return handle;
  }

  cancel(handle: number): void {
    this.cancelled.push(handle);
    this.callbacks.delete(handle);
  }

  flush(): void {
    const entry = this.callbacks.entries().next().value;
    assert.ok(entry, "expected a scheduled player frame");
    const [handle, callback] = entry;
    this.callbacks.delete(handle);
    callback(0);
  }

  get pending(): number {
    return this.callbacks.size;
  }
}

class FakeMedia {
  currentTime = 0;
  duration = 120;
  paused = true;
  private readonly listeners = new Map<string, Set<() => void>>();

  addEventListener(type: string, listener: () => void): void {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: () => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  play(): Promise<void> {
    this.paused = false;
    this.emit("play");
    return Promise.resolve();
  }

  pause(): void {
    if (this.paused) return;
    this.paused = true;
    this.emit("pause");
  }

  emit(type: string): void {
    for (const listener of this.listeners.get(type) ?? []) listener();
  }
}

function fixture() {
  const scheduler = new FakeScheduler();
  const store = new PlayerStore(120, scheduler);
  const media = new FakeMedia();
  const detach = store.attach(media as unknown as HTMLMediaElement);
  return { scheduler, store, media, detach };
}

test("one-shot range playback stops at the exact end boundary", () => {
  const { scheduler, store, media } = fixture();
  store.playRange({ start_s: 10, end_s: 20 });

  assert.equal(media.currentTime, 10);
  assert.equal(store.getSnapshot().activePlayback?.mode, "once");
  media.currentTime = 20.4;
  scheduler.flush();

  assert.equal(media.currentTime, 20);
  assert.equal(media.paused, true);
  assert.equal(store.getSnapshot().currentTime, 20);
  assert.equal(store.getSnapshot().activePlayback, null);
  assert.equal(scheduler.pending, 0);
});

test("native media events also enforce a controlled end boundary", () => {
  const { scheduler, store, media } = fixture();
  store.playRange({ start_s: 4, end_s: 6 });

  media.currentTime = 6.2;
  media.emit("timeupdate");

  assert.equal(media.currentTime, 6);
  assert.equal(store.getSnapshot().activePlayback, null);
  assert.equal(scheduler.pending, 0);
});

test("loop playback clamps both sides back to its start", () => {
  const { scheduler, store, media } = fixture();
  store.playRange({ start_s: 10, end_s: 20 }, true);

  media.currentTime = 20;
  scheduler.flush();
  assert.equal(media.currentTime, 10);
  assert.equal(store.getSnapshot().activePlayback?.mode, "loop");

  media.currentTime = 3;
  scheduler.flush();
  assert.equal(media.currentTime, 10);
  assert.equal(media.paused, false);
});

test("A/B playback moves from A to B and then stops", () => {
  const { scheduler, store, media } = fixture();
  store.playAB(
    { start_s: 8, end_s: 12 },
    { start_s: 30, end_s: 34 },
  );

  media.currentTime = 12;
  scheduler.flush();
  assert.equal(media.currentTime, 30);
  assert.deepEqual(store.getSnapshot().activePlayback, {
    mode: "ab",
    rangeA: { start_s: 8, end_s: 12 },
    rangeB: { start_s: 30, end_s: 34 },
    phase: "b",
  });

  media.currentTime = 34.7;
  scheduler.flush();
  assert.equal(media.currentTime, 34);
  assert.equal(media.paused, true);
  assert.equal(store.getSnapshot().activePlayback, null);
});

test("ranges are normalized to media bounds and invalid ranges are ignored", () => {
  const { store } = fixture();
  store.playRange({ start_s: -10, end_s: 130 });
  assert.deepEqual(store.getSnapshot().activePlayback, {
    mode: "once",
    range: { start_s: 0, end_s: 120 },
  });

  store.clearPlayback();
  store.playRange({ start_s: 130, end_s: 150 });
  assert.equal(store.getSnapshot().activePlayback, null);
});

test("detaching cancels its scheduler and controlled playback", () => {
  const { scheduler, store, detach } = fixture();
  store.playRange({ start_s: 10, end_s: 20 }, true);
  assert.equal(scheduler.pending, 1);

  detach();
  assert.equal(scheduler.pending, 0);
  assert.equal(store.getSnapshot().activePlayback, null);
  assert.equal(store.getSnapshot().playing, false);
  assert.equal(scheduler.cancelled.length, 1);
});

test("a manual seek exits controlled playback without pausing normal playback", () => {
  const { scheduler, store, media } = fixture();
  store.playRange({ start_s: 10, end_s: 20 }, true);

  store.seek(60);
  assert.equal(media.currentTime, 60);
  assert.equal(media.paused, false);
  assert.equal(store.getSnapshot().activePlayback, null);
  assert.equal(scheduler.pending, 0);
});
