import assert from "node:assert/strict";
import test from "node:test";

import { confidenceClass, percent, seconds } from "../src/format.ts";

test("formats timeline values", () => {
  assert.equal(seconds(0), "0:00");
  assert.equal(seconds(125.9), "2:05");
  assert.equal(seconds(null), "--:--");
});

test("formats confidence values consistently", () => {
  assert.equal(percent(0.756), "76%");
  assert.equal(percent(null), "—");
  assert.equal(confidenceClass(null), "neutral");
  assert.equal(confidenceClass(0.2), "low");
  assert.equal(confidenceClass(0.5), "medium");
  assert.equal(confidenceClass(0.8), "good");
});
