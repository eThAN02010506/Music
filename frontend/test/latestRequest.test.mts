import assert from "node:assert/strict";
import test from "node:test";

import { LatestRequest } from "../src/hooks/latestRequest.ts";

test("only the newest asynchronous request remains current", () => {
  const requests = new LatestRequest();
  const first = requests.begin();
  const second = requests.begin();

  assert.equal(requests.isCurrent(first), false);
  assert.equal(requests.isCurrent(second), true);
  assert.equal(requests.capture(), second);

  requests.invalidate();
  assert.equal(requests.isCurrent(second), false);
});
