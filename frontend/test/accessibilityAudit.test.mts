import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Static accessibility audit: follow the pattern of the existing
// leaderboardAccessibility.test.mts and assert the a11y invariants we care
// about directly from source, so a regression is caught without a browser.

const player = readFileSync(
  new URL("../src/features/player/WaveformView.tsx", import.meta.url),
  "utf8",
);
const insightPlayer = readFileSync(
  new URL("../src/features/player/InsightPlayer.tsx", import.meta.url),
  "utf8",
);
const analysis = readFileSync(
  new URL("../src/features/analysis/AnalysisControls.tsx", import.meta.url),
  "utf8",
);
const chat = readFileSync(
  new URL("../src/features/chat/ChatAnswer.tsx", import.meta.url),
  "utf8",
);
const listeningChat = readFileSync(
  new URL("../src/features/chat/ListeningChat.tsx", import.meta.url),
  "utf8",
);
const workspace = readFileSync(
  new URL("../src/features/workspace/AuthenticatedWorkspace.tsx", import.meta.url),
  "utf8",
);
const i18n = readFileSync(
  new URL("../src/i18n.tsx", import.meta.url),
  "utf8",
);

test("waveform is exposed to assistive tech with an accessible name", () => {
  assert.match(player, /role="img"/);
  assert.match(player, /aria-label=\{t\("歌曲波形；拖动可选择最多 30 秒"\)\}/);
  // Selection has a keyboard-visible number input fallback, not only drag.
  assert.match(insightPlayer, /type="number"/);
});

test("analysis progress is a real progressbar with a valuetext", () => {
  assert.match(analysis, /role="progressbar"/);
  assert.match(analysis, /aria-valuenow=\{progressPercent\}/);
  assert.match(analysis, /aria-valuetext=/);
});

test("chat answers announce generation and errors in live regions", () => {
  assert.match(listeningChat, /role="status"/);
  assert.match(listeningChat, /role="log"/);
  assert.match(listeningChat, /aria-live="polite"/);
  // The answer body itself is plain text (no dangerouslySetInnerHTML).
  assert.doesNotMatch(chat, /dangerouslySetInnerHTML/);
});

test("status colors always carry a text label", () => {
  // The pulse dot is decorative; the adjacent stageLabel is the semantic
  // signal, so color is never the only representation of job state.
  assert.match(analysis, /className=\{`status-pulse \$\{job\.state\}`\}/);
  assert.match(analysis, /<span>\{stageLabel\}<\/span>/);
});

test("language switch has an accessible group and pressed state", () => {
  assert.match(i18n, /aria-label=\{t\("切换界面语言"\)\}/);
  assert.match(i18n, /aria-pressed=\{locale === "zh-CN"\}/);
  assert.match(i18n, /aria-pressed=\{locale === "en"\}/);
});

test("workspace controls expose accessible names", () => {
  assert.match(workspace, /aria-label=\{t\("打开我的演唱记录"\)\}/);
  assert.match(workspace, /aria-label=\{t\("打开演唱排行榜"\)\}/);
});
