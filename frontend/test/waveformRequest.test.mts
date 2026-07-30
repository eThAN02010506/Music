import assert from "node:assert/strict";
import test from "node:test";

import type { HistoryWaveform } from "../src/types.ts";
import {
  loadWaveformOnce,
} from "../src/features/player/waveformRequest.ts";

const WAVEFORM: HistoryWaveform = {
  duration_s: 10,
  peaks: [[-1, 1]],
  points_per_channel: 2,
};

test("strict-mode duplicate waveform loads share one request", async () => {
  let calls = 0;
  let release!: (value: HistoryWaveform) => void;
  const pending = new Promise<HistoryWaveform>((resolve) => {
    release = resolve;
  });
  const loader = () => {
    calls += 1;
    return pending;
  };

  const first = loadWaveformOnce("song-a", loader);
  const second = loadWaveformOnce("song-a", loader);
  assert.equal(first, second);
  assert.equal(calls, 1);

  release(WAVEFORM);
  assert.deepEqual(await first, WAVEFORM);
  assert.deepEqual(await second, WAVEFORM);
});

test("settled waveform requests do not become a stale client cache", async () => {
  let calls = 0;
  const loader = async () => {
    calls += 1;
    return WAVEFORM;
  };

  await loadWaveformOnce("song-b", loader);
  await loadWaveformOnce("song-b", loader);

  assert.equal(calls, 2);
});
