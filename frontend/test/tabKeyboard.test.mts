import assert from "node:assert/strict";
import test from "node:test";
import { tabIndexAfterKey } from "../src/hooks/tabKeyboard.ts";

test("tab keyboard navigation wraps and supports Home and End", () => {
  assert.equal(tabIndexAfterKey(0, 2, "ArrowRight"), 1);
  assert.equal(tabIndexAfterKey(1, 2, "ArrowRight"), 0);
  assert.equal(tabIndexAfterKey(0, 2, "ArrowLeft"), 1);
  assert.equal(tabIndexAfterKey(1, 2, "Home"), 0);
  assert.equal(tabIndexAfterKey(0, 2, "End"), 1);
  assert.equal(tabIndexAfterKey(0, 2, "Enter"), null);
});
