import assert from "node:assert/strict";
import test from "node:test";
import { AbortableLatestRequest } from "../src/hooks/abortableLatestRequest.ts";

test("starting or invalidating a latest request aborts obsolete work", () => {
  const requests = new AbortableLatestRequest();
  const first = requests.begin();
  const second = requests.begin();

  assert.equal(first.signal.aborted, true);
  assert.equal(second.signal.aborted, false);
  assert.equal(requests.isCurrent(first.id), false);
  assert.equal(requests.isCurrent(second.id), true);

  requests.invalidate();
  assert.equal(second.signal.aborted, true);
  assert.equal(requests.isCurrent(second.id), false);
});

test("settling the current request releases ownership without aborting it", () => {
  const requests = new AbortableLatestRequest();
  const current = requests.begin();

  requests.settle(current.id);

  assert.equal(current.signal.aborted, false);
  assert.equal(requests.isCurrent(current.id), true);
});
