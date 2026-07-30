import assert from "node:assert/strict";
import test from "node:test";

import type {
  AnalysisResult,
  HistoryDetail,
  JobSnapshot,
} from "../src/types.ts";
import {
  activeHistoryId,
  comparisonEntries,
  initialWorkspaceState,
  selectedFile,
  selectedFileName,
  workspaceReducer,
} from "../src/features/workspace/workspaceState.ts";

const result: AnalysisResult = {
  title: "Sample",
  summary: "summary",
  lyrics: [],
  instruments: [],
  sound_events: [],
  emotion_timeline: [],
  inferred_atmosphere: [],
  themes: [],
  technical_metrics: {
    bpm: null,
    bpm_confidence: null,
    bpm_candidates: [],
    bpm_ambiguous: false,
    key: null,
    key_confidence: null,
    energy_curve: [],
    evidence: [],
  },
  evidence: [],
  vocal_presence: {
    status: "unknown",
    confidence: null,
    reason: "test",
    evidence_ids: [],
  },
  warnings: [],
};

function job(id: string, state: JobSnapshot["state"] = "running"): JobSnapshot {
  return {
    id,
    state,
    stage: state,
    progress: state === "completed" ? 1 : 0.5,
    message: state,
    result_url: state === "completed" ? `/jobs/${id}/result` : null,
    error: null,
    persistence_error: null,
    revision: 1,
  };
}

function history(id: string): HistoryDetail {
  return {
    id,
    title: `Title ${id}`,
    file_name: `${id}.mp3`,
    language: "en",
    state: "completed",
    created_at: "2026-07-28T00:00:00Z",
    updated_at: "2026-07-28T00:00:00Z",
    error: null,
    summary: "summary",
    duration_s: 10,
    lyrics_count: 0,
    instruments: [],
    bpm: 80,
    model_source: "network",
    model_location: "http://localhost:8004",
    result,
    audio_url: `/history/${id}/audio`,
    revision_count: 2,
  };
}

test("new upload and created job form one coherent workspace selection", () => {
  const file = { name: "voice.mp3" } as File;
  let state = workspaceReducer(initialWorkspaceState, {
    type: "file-chosen",
    file,
  });

  assert.equal(selectedFile(state), file);
  assert.equal(selectedFileName(state), "voice.mp3");
  assert.equal(activeHistoryId(state), null);

  state = workspaceReducer(state, { type: "analysis-started" });
  state = workspaceReducer(state, {
    type: "job-created",
    snapshot: job("job-1"),
  });

  assert.equal(activeHistoryId(state), "job-1");
  assert.equal(state.job?.id, "job-1");
  assert.equal(selectedFile(state), file);

  state = workspaceReducer(state, { type: "new-analysis" });
  assert.deepEqual(state, initialWorkspaceState);
});

test("remote URL jobs retain a stable name without a browser File", () => {
  let state = workspaceReducer(initialWorkspaceState, {
    type: "remote-chosen",
    fileName: "remote-song.mp3",
  });

  assert.equal(selectedFile(state), null);
  assert.equal(selectedFileName(state), "remote-song.mp3");

  state = workspaceReducer(state, { type: "analysis-started" });
  state = workspaceReducer(state, {
    type: "job-created",
    snapshot: job("remote-job"),
  });

  assert.equal(activeHistoryId(state), "remote-job");
  assert.equal(selectedFileName(state), "remote-song.mp3");
  assert.equal(state.job?.id, "remote-job");
});

test("only the latest history selection may replace the current view", () => {
  let state = workspaceReducer(initialWorkspaceState, {
    type: "navigation-started",
    navigation: { kind: "history", requestId: 1, historyId: "old" },
  });
  state = workspaceReducer(state, {
    type: "navigation-started",
    navigation: { kind: "history", requestId: 2, historyId: "new" },
  });

  const beforeStaleResponse = state;
  state = workspaceReducer(state, {
    type: "history-loaded",
    requestId: 1,
    historyId: "old",
    entry: history("old"),
    job: null,
    result,
    audioUrl: "/old.mp3",
  });
  assert.equal(state, beforeStaleResponse);

  state = workspaceReducer(state, {
    type: "history-loaded",
    requestId: 2,
    historyId: "new",
    entry: history("new"),
    job: null,
    result,
    audioUrl: "/new.mp3",
  });
  assert.equal(activeHistoryId(state), "new");
  assert.equal(selectedFileName(state), "new.mp3");
  assert.equal(state.result, result);
});

test("choosing a file invalidates an outstanding comparison response", () => {
  let state = workspaceReducer(initialWorkspaceState, {
    type: "navigation-started",
    navigation: { kind: "comparison", requestId: 3 },
  });
  const file = { name: "replacement.wav" } as File;
  state = workspaceReducer(state, { type: "file-chosen", file });
  const fileState = state;

  state = workspaceReducer(state, {
    type: "comparison-loaded",
    requestId: 3,
    entries: [history("one"), history("two")],
  });

  assert.equal(state, fileState);
  assert.equal(selectedFile(state), file);
  assert.deepEqual(comparisonEntries(state), []);
});

test("comparison replaces history, job, result and revision state atomically", () => {
  let state = workspaceReducer(initialWorkspaceState, {
    type: "navigation-started",
    navigation: { kind: "history", requestId: 4, historyId: "one" },
  });
  state = workspaceReducer(state, {
    type: "history-loaded",
    requestId: 4,
    historyId: "one",
    entry: history("one"),
    job: job("one", "completed"),
    result,
    audioUrl: "/one.mp3",
  });
  state = workspaceReducer(state, {
    type: "navigation-started",
    navigation: { kind: "comparison", requestId: 5 },
  });
  state = workspaceReducer(state, {
    type: "comparison-loaded",
    requestId: 5,
    entries: [history("one"), history("two")],
  });

  assert.equal(activeHistoryId(state), null);
  assert.equal(state.job, null);
  assert.equal(state.result, null);
  assert.deepEqual(
    comparisonEntries(state).map((entry) => entry.id),
    ["one", "two"],
  );
});

test("late job and lyric responses cannot mutate a different selection", () => {
  const file = { name: "first.wav" } as File;
  let state = workspaceReducer(initialWorkspaceState, {
    type: "file-chosen",
    file,
  });
  state = workspaceReducer(state, {
    type: "job-created",
    snapshot: job("first"),
  });
  state = workspaceReducer(state, { type: "new-analysis" });

  const cleanState = state;
  state = workspaceReducer(state, {
    type: "job-snapshot",
    snapshot: job("first", "cancelled"),
  });
  state = workspaceReducer(state, {
    type: "lyrics-saved",
    historyId: "first",
    result,
    revisionCount: 9,
  });

  assert.equal(state, cleanState);
});

test("changing compare selection invalidates an in-flight comparison", () => {
  let state = workspaceReducer(initialWorkspaceState, {
    type: "navigation-started",
    navigation: { kind: "comparison", requestId: 10 },
  });
  state = workspaceReducer(state, { type: "comparison-selection-changed" });
  const selectionState = state;

  state = workspaceReducer(state, {
    type: "comparison-loaded",
    requestId: 10,
    entries: [history("old-a"), history("old-b")],
  });

  assert.equal(state, selectionState);
  assert.equal(state.pendingNavigation, null);
  assert.deepEqual(comparisonEntries(state), []);
});

test("resource-scoped results survive failed navigation but not a successful switch", () => {
  const file = { name: "first.wav" } as File;
  let state = workspaceReducer(initialWorkspaceState, {
    type: "file-chosen",
    file,
  });
  state = workspaceReducer(state, {
    type: "job-created",
    snapshot: job("first", "completed"),
  });
  state = workspaceReducer(state, {
    type: "navigation-started",
    navigation: { kind: "history", requestId: 20, historyId: "missing" },
  });
  state = workspaceReducer(state, {
    type: "navigation-failed",
    requestId: 20,
    error: "network error",
  });
  state = workspaceReducer(state, {
    type: "job-result",
    jobId: "first",
    result,
  });
  state = workspaceReducer(state, {
    type: "lyrics-saved",
    historyId: "first",
    result: { ...result, summary: "saved" },
    revisionCount: 3,
  });

  assert.equal(state.result?.summary, "saved");
  assert.equal(state.view.kind === "start" && state.view.revisionCount, 3);

  state = workspaceReducer(state, {
    type: "navigation-started",
    navigation: { kind: "history", requestId: 21, historyId: "second" },
  });
  state = workspaceReducer(state, {
    type: "history-loaded",
    requestId: 21,
    historyId: "second",
    entry: history("second"),
    job: null,
    result,
    audioUrl: "/second.mp3",
  });
  const secondState = state;
  state = workspaceReducer(state, {
    type: "lyrics-saved",
    historyId: "first",
    result: { ...result, summary: "must not cross" },
    revisionCount: 4,
  });

  assert.equal(state, secondState);
  assert.equal(activeHistoryId(state), "second");
});
