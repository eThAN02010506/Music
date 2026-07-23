import assert from "node:assert/strict";
import test from "node:test";

import { profileForEndpoint } from "../src/modelProfiles.ts";

test("selects known LAN model profiles", () => {
  assert.equal(
    profileForEndpoint("", "http://192.168.1.97:8004").id,
    "qwen-8004",
  );
  assert.equal(
    profileForEndpoint("http://192.168.1.97:8005/", "unused").id,
    "minicpm-8005",
  );
  assert.equal(
    profileForEndpoint("http://127.0.0.1:9000", "unused").id,
    "custom",
  );
});
