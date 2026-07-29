import assert from "node:assert/strict";
import test from "node:test";
import {
  HEALTH_RETRY_DELAYS_MS,
  HealthCheckController,
  type HealthCheckSnapshot,
} from "../src/hooks/healthCheckController.ts";

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (cause: unknown) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
}

test("health checks use finite bounded retries and expose checked state", async () => {
  const timers: Array<{ callback: () => void; delay: number }> = [];
  const snapshots: Array<HealthCheckSnapshot<string>> = [];
  const controller = new HealthCheckController<string>(
    {
      probe: async () => {
        throw new Error("offline");
      },
      setTimer: (callback, delay) => {
        const timer = { callback, delay };
        timers.push(timer);
        return timer;
      },
      clearTimer: () => {},
    },
    (snapshot) => snapshots.push(snapshot),
  );

  controller.start();
  await flushPromises();
  assert.deepEqual(
    snapshots.at(-1),
    { value: null, checked: true, checking: false },
  );

  for (const expectedDelay of HEALTH_RETRY_DELAYS_MS) {
    const timer = timers.shift();
    assert.equal(timer?.delay, expectedDelay);
    timer?.callback();
    await flushPromises();
  }
  assert.equal(timers.length, 0);
  controller.dispose();
});

test("manual retry aborts and ignores an obsolete health response", async () => {
  const requests: Array<{ signal: AbortSignal; response: Deferred<string> }> = [];
  const snapshots: Array<HealthCheckSnapshot<string>> = [];
  const controller = new HealthCheckController<string>(
    {
      probe: (signal) => {
        const response = deferred<string>();
        requests.push({ signal, response });
        return response.promise;
      },
      setTimer: () => 0,
      clearTimer: () => {},
    },
    (snapshot) => snapshots.push(snapshot),
  );

  controller.start();
  controller.retryNow();
  assert.equal(requests.length, 2);
  assert.equal(requests[0].signal.aborted, true);

  requests[0].response.resolve("obsolete");
  await flushPromises();
  assert.notEqual(snapshots.at(-1)?.value, "obsolete");

  requests[1].response.resolve("healthy");
  await flushPromises();
  assert.deepEqual(
    snapshots.at(-1),
    { value: "healthy", checked: true, checking: false },
  );
  controller.dispose();
});
