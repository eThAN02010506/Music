import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const i18nSource = readFileSync(
  new URL("../src/i18n.tsx", import.meta.url),
  "utf8",
);
const mainSource = readFileSync(
  new URL("../src/main.tsx", import.meta.url),
  "utf8",
);
const workspaceSource = readFileSync(
  new URL(
    "../src/features/workspace/AuthenticatedWorkspace.tsx",
    import.meta.url,
  ),
  "utf8",
);

test("the interface locale is global, persistent, and reflected in document language", () => {
  assert.match(mainSource, /<I18nProvider>/);
  assert.match(i18nSource, /music-insight\.ui-locale/);
  assert.match(i18nSource, /document\.documentElement\.lang = locale/);
  assert.match(i18nSource, /window\.localStorage\.setItem\(STORAGE_KEY, locale\)/);
});

test("the topbar exposes a two-language switch with pressed state", () => {
  assert.match(workspaceSource, /<LanguageSwitcher compact \/>/);
  assert.match(i18nSource, /aria-pressed=\{locale === "zh-CN"\}/);
  assert.match(i18nSource, /aria-pressed=\{locale === "en"\}/);
});
