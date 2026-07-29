import assert from "node:assert/strict";
import test from "node:test";

import {
  announceAuthChange,
  AUTH_SYNC_STORAGE_KEY,
  isAuthSyncStorageKey,
} from "../src/authSession.ts";

test("auth changes publish a storage token without user data", () => {
  const writes: Array<[string, string]> = [];
  announceAuthChange({
    setItem: (key, value) => {
      writes.push([key, value]);
    },
  }, "revision-2");

  assert.deepEqual(writes, [[AUTH_SYNC_STORAGE_KEY, "revision-2"]]);
});

test("only the auth synchronization key suspends another tab", () => {
  assert.equal(isAuthSyncStorageKey(AUTH_SYNC_STORAGE_KEY), true);
  assert.equal(isAuthSyncStorageKey("unrelated"), false);
  assert.equal(isAuthSyncStorageKey(null), false);
});
