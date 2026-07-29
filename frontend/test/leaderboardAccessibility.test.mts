import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/features/singing/SingingViews.tsx", import.meta.url),
  "utf8",
);
const workspaceSource = readFileSync(
  new URL(
    "../src/features/workspace/AuthenticatedWorkspace.tsx",
    import.meta.url,
  ),
  "utf8",
);

test("leaderboard loading and failure states are announced", () => {
  assert.match(
    source,
    /className="leaderboard-state" role="status">正在汇总最高成绩…/,
  );
  assert.match(
    source,
    /className="leaderboard-state error" role="alert">\{error\}/,
  );
});

test("compact topbar actions retain accessible names", () => {
  assert.match(workspaceSource, /aria-label="打开我的演唱记录"/);
  assert.match(workspaceSource, /aria-label="打开演唱排行榜"/);
});
