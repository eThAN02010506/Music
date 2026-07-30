import assert from "node:assert/strict";
import test from "node:test";

import { formatApiErrorDetail } from "../src/apiError.ts";

test("API errors extract FastAPI string details", () => {
  assert.equal(
    formatApiErrorDetail('{"detail":"模型服务繁忙，请稍后重试"}'),
    "模型服务繁忙，请稍后重试",
  );
});

test("API errors summarize bounded validation details", () => {
  assert.equal(
    formatApiErrorDetail(JSON.stringify({
      detail: [
        { loc: ["body", "password"], msg: "至少需要 8 个字符" },
        { loc: ["body", "username"], msg: "字段不能为空" },
      ],
    })),
    "password：至少需要 8 个字符；username：字段不能为空",
  );
});

test("API errors do not expose a reverse proxy HTML document", () => {
  assert.equal(
    formatApiErrorDetail("<html><body>proxy failure</body></html>", "Bad Gateway"),
    "Bad Gateway",
  );
});

test("API errors bound unexpectedly large plain-text responses", () => {
  const detail = formatApiErrorDetail("x".repeat(3_000));
  assert.equal(detail.length, 2_000);
  assert.ok(detail.endsWith("…"));
});
