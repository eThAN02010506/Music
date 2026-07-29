import assert from "node:assert/strict";
import test from "node:test";

import {
  isIdempotentSingingAttemptDelete,
  mergeSingingAttemptPage,
  singingAttemptCursor,
  singingAttemptSource,
} from "../src/features/singing/singingAttemptHistory.ts";
import type { SingingAttempt } from "../src/types.ts";

function attempt(id: string): SingingAttempt {
  return {
    id,
    user_id: "current-user",
    source: "standalone",
    category: "entertainment",
    history_id: null,
    reference_name: "reference.wav",
    performance_name: `${id}.wav`,
    created_at: "2026-07-30T09:00:00Z",
    score: {
      total: 80,
      pitch: 80,
      rhythm: 80,
      completeness: 80,
      stability: 80,
      median_pitch_error: null,
      in_tune_ratio: null,
      reference_duration_s: 30,
      performance_duration_s: 30,
      pitch_curve: [],
      notes: [],
    },
  };
}

test("ten visible singing attempts use the eleventh as lookahead", () => {
  const response = Array.from(
    { length: 11 },
    (_, index) => attempt(`attempt-${String(index).padStart(2, "0")}`),
  );
  const page = mergeSingingAttemptPage(
    [attempt("stale")],
    response,
    { reset: true, pageSize: 10 },
  );

  assert.deepEqual(
    page.items.map((item) => item.id),
    response.slice(0, 10).map((item) => item.id),
  );
  assert.equal(page.hasMore, true);
});

test("singing attempt pages append without duplicate records", () => {
  const page = mergeSingingAttemptPage(
    [attempt("first"), attempt("second")],
    [attempt("second"), attempt("third")],
    { reset: false, pageSize: 2 },
  );

  assert.deepEqual(
    page.items.map((item) => item.id),
    ["first", "second", "third"],
  );
  assert.equal(page.hasMore, false);
});

test("the next page cursor is the exact created_at and id tie-break", () => {
  const page = Array.from(
    { length: 10 },
    (_, index) => attempt(`same-time-${String(20 - index).padStart(2, "0")}`),
  );

  assert.deepEqual(singingAttemptCursor(page), {
    created_at: "2026-07-30T09:00:00Z",
    id: "same-time-11",
  });
  assert.equal(singingAttemptCursor([]), null);
});

test("singing attempt source labels stay user-facing", () => {
  assert.equal(singingAttemptSource("history"), "分析歌曲演唱");
  assert.equal(singingAttemptSource("standalone"), "独立演唱对比");
  assert.equal(singingAttemptSource("future"), "演唱评分");
});

test("a missing attempt is an idempotent delete result", () => {
  assert.equal(isIdempotentSingingAttemptDelete({ status: 404 }), true);
  assert.equal(isIdempotentSingingAttemptDelete({ status: 403 }), false);
  assert.equal(isIdempotentSingingAttemptDelete(new Error("network")), false);
});
