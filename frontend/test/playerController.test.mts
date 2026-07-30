import assert from "node:assert/strict";
import test from "node:test";

import {
  adjacentComparisonRanges,
  clampTime,
  normalizeRange,
  rangeAround,
  sanitizePlayerAction,
  spanOverlaps,
} from "../src/features/player/playerController.ts";

test("clampTime rejects non-finite and out-of-duration positions", () => {
  assert.equal(clampTime(Number.NaN, 120), 0);
  assert.equal(clampTime(-2, 120), 0);
  assert.equal(clampTime(200, 120), 120);
  assert.equal(clampTime(42, 120), 42);
});

test("normalizeRange rejects reversed and zero-length ranges", () => {
  assert.deepEqual(normalizeRange({ start_s: -2, end_s: 3 }, 10), {
    start_s: 0,
    end_s: 3,
  });
  assert.equal(normalizeRange({ start_s: 5, end_s: 5 }, 10), null);
  assert.equal(normalizeRange({ start_s: 8, end_s: 2 }, 10), null);
});

test("rangeAround keeps a fifteen-second task inside the media duration", () => {
  assert.deepEqual(rangeAround(2, 100), { start_s: 0, end_s: 15 });
  assert.deepEqual(rangeAround(98, 100), { start_s: 85, end_s: 100 });
  assert.deepEqual(rangeAround(5, 8), { start_s: 0, end_s: 8 });
});

test("adjacent comparison ranges stay distinct and inside the track", () => {
  assert.deepEqual(adjacentComparisonRanges(0, 100), [
    { start_s: 0, end_s: 15 },
    { start_s: 15, end_s: 30 },
  ]);
  assert.deepEqual(adjacentComparisonRanges(98, 100), [
    { start_s: 70, end_s: 85 },
    { start_s: 85, end_s: 100 },
  ]);
  assert.deepEqual(adjacentComparisonRanges(4, 8), [
    { start_s: 0, end_s: 4 },
    { start_s: 4, end_s: 8 },
  ]);
});

test("model-provided player actions are clamped and invalid ranges rejected", () => {
  assert.deepEqual(
    sanitizePlayerAction({ type: "seek", time_s: 500 }, 120),
    { type: "seek", time_s: 120 },
  );
  assert.equal(
    sanitizePlayerAction({ type: "loop_range", start_s: 12, end_s: 3 }, 120),
    null,
  );
  assert.deepEqual(
    sanitizePlayerAction({
      type: "set_ab",
      a: { start_s: -4, end_s: 3 },
      b: { start_s: 200, end_s: 240 },
    }, 210),
    {
      type: "set_ab",
      a: { start_s: 0, end_s: 3 },
      b: { start_s: 200, end_s: 210 },
    },
  );
});

test("overlap checks use half-open time ranges", () => {
  assert.equal(
    spanOverlaps({ start_s: 0, end_s: 5 }, { start_s: 5, end_s: 8 }),
    false,
  );
  assert.equal(
    spanOverlaps({ start_s: 0, end_s: 5 }, { start_s: 4.9, end_s: 8 }),
    true,
  );
});
