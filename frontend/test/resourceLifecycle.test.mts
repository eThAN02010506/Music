import assert from "node:assert/strict";
import test from "node:test";

import {
  ObjectUrlSlot,
  type ObjectUrlApi,
} from "../src/hooks/useObjectUrl.ts";
import {
  stopMediaStream,
} from "../src/hooks/audioRecorderController.ts";

test("object URL slots replace and dispose only their own active URL", () => {
  const revoked: string[] = [];
  let sequence = 0;
  const api: ObjectUrlApi = {
    createObjectURL: () => `blob:${++sequence}`,
    revokeObjectURL: (url) => revoked.push(url),
  };
  const reference = new ObjectUrlSlot(api);
  const performance = new ObjectUrlSlot(api);

  assert.equal(reference.replace(new Blob(["reference"])), "blob:1");
  assert.equal(performance.replace(new Blob(["performance"])), "blob:2");
  assert.equal(reference.replace(new Blob(["new reference"])), "blob:3");
  assert.deepEqual(revoked, ["blob:1"]);

  performance.dispose();
  assert.deepEqual(revoked, ["blob:1", "blob:2"]);
  reference.dispose();
  reference.dispose();
  assert.deepEqual(revoked, ["blob:1", "blob:2", "blob:3"]);
});

test("stopMediaStream releases every track", () => {
  const stopped: number[] = [];
  stopMediaStream({
    getTracks: () => [
      { stop: () => stopped.push(1) },
      { stop: () => stopped.push(2) },
    ],
  });
  assert.deepEqual(stopped, [1, 2]);
});
