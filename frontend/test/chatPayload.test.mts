import assert from "node:assert/strict";
import test from "node:test";

import {
  buildTeachingChatRequest,
  createClientRequestId,
} from "../src/features/chat/chatPayload.ts";
import type { PlayerSnapshot } from "../src/features/player/playerTypes.ts";

const snapshot: PlayerSnapshot = {
  currentTime: 42,
  duration: 180,
  playing: true,
  selectedRange: { start_s: 35, end_s: 50 },
  rangeA: { start_s: 10, end_s: 20 },
  rangeB: { start_s: 70, end_s: 80 },
  activePlayback: null,
};

test("client request IDs prefer randomUUID in secure contexts", () => {
  let fallbackCalls = 0;
  const requestId = createClientRequestId({
    randomUUID: () => "12345678-1234-4123-8123-123456789abc",
    getRandomValues: (values) => {
      fallbackCalls += 1;
      return values;
    },
  });

  assert.equal(requestId, "12345678-1234-4123-8123-123456789abc");
  assert.equal(fallbackCalls, 0);
});

test("client request IDs fall back to UUIDv4-shaped random bytes on LAN HTTP", () => {
  const requestId = createClientRequestId({
    getRandomValues: (values) => {
      values.set([
        0x00, 0x11, 0x22, 0x33,
        0x44, 0x55,
        0x66, 0x77,
        0xff, 0x99,
        0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff,
      ]);
      return values;
    },
  });

  assert.equal(requestId, "00112233-4455-4677-bf99-aabbccddeeff");
  assert.match(
    requestId,
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
});

test("client request IDs also fall back when randomUUID rejects the origin", () => {
  const requestId = createClientRequestId({
    randomUUID: () => {
      throw new Error("randomUUID requires a secure context");
    },
    getRandomValues: (values) => {
      values.fill(0);
      return values;
    },
  });

  assert.equal(requestId, "00000000-0000-4000-8000-000000000000");
});

test("current-time chat does not send client-derived evidence", () => {
  assert.deepEqual(buildTeachingChatRequest({
    message: "  这里为什么变亮？ ",
    snapshot,
    mode: "current",
    requestId: "request-123",
  }), {
    client_request_id: "request-123",
    message: "这里为什么变亮？",
    current_time_s: 42,
    selected_range: null,
    compare_ranges: [],
    relisten_policy: "auto",
  });
});

test("selection and A/B modes carry only their requested time ranges", () => {
  assert.deepEqual(buildTeachingChatRequest({
    message: "解释选区",
    snapshot,
    mode: "selection",
    requestId: "request-456",
  }).selected_range, snapshot.selectedRange);
  const compared = buildTeachingChatRequest({
    message: "比较",
    snapshot,
    mode: "compare",
    requestId: "request-789",
  });
  assert.equal(compared.selected_range, null);
  assert.deepEqual(compared.compare_ranges, [snapshot.rangeA, snapshot.rangeB]);
});
