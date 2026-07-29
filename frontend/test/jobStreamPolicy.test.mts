import assert from "node:assert/strict";
import test from "node:test";

import {
  HistoryRefreshGate,
  reconnectDelay,
} from "../src/hooks/jobStreamPolicy.ts";

test("history refresh is throttled during progress and always allowed at terminal", () => {
  const gate = new HistoryRefreshGate(5_000);
  assert.equal(gate.shouldRefresh(1_000), true);
  assert.equal(gate.shouldRefresh(1_001), false);
  assert.equal(gate.shouldRefresh(5_999), false);
  assert.equal(gate.shouldRefresh(6_000), true);
  assert.equal(gate.shouldRefresh(6_001, true), true);
});

test("stream reconnect delay backs off and remains capped", () => {
  assert.equal(reconnectDelay(0), 1_000);
  assert.equal(reconnectDelay(1), 2_000);
  assert.equal(reconnectDelay(4), 15_000);
  assert.equal(reconnectDelay(20), 15_000);
});
