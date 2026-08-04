import type { Span } from "../../types";
import type { PlayerAction } from "./playerTypes";

const MIN_RANGE_SECONDS = 0.05;

export function clampTime(value: number, duration: number): number {
  const safeDuration = Number.isFinite(duration) && duration > 0
    ? duration
    : Number.MAX_SAFE_INTEGER;
  if (!Number.isFinite(value)) return 0;
  return Math.min(safeDuration, Math.max(0, value));
}

export function normalizeRange(
  range: Span,
  duration: number,
): Span | null {
  const start_s = clampTime(range.start_s, duration);
  const end_s = clampTime(range.end_s, duration);
  if (end_s - start_s < MIN_RANGE_SECONDS) return null;
  return { start_s, end_s };
}

export function limitSelectionRange(
  range: Span,
  duration: number,
  maxSeconds = 30,
): Span | null {
  const normalized = normalizeRange(range, duration);
  if (!normalized) return null;
  if (normalized.end_s - normalized.start_s <= maxSeconds) return normalized;
  return {
    start_s: normalized.start_s,
    end_s: normalized.start_s + maxSeconds,
  };
}

export function rangeAround(
  currentTime: number,
  duration: number,
  windowSeconds = 15,
): Span {
  const safeWindow = Number.isFinite(windowSeconds)
    ? Math.max(MIN_RANGE_SECONDS, windowSeconds)
    : 15;
  const centre = clampTime(currentTime, duration);
  let start_s = Math.max(0, centre - safeWindow / 2);
  let end_s = Math.min(Math.max(duration, 0), start_s + safeWindow);
  if (end_s - start_s < safeWindow && duration >= safeWindow) {
    start_s = Math.max(0, end_s - safeWindow);
  }
  if (end_s <= start_s) {
    end_s = start_s + Math.min(safeWindow, Math.max(duration, MIN_RANGE_SECONDS));
  }
  return { start_s, end_s };
}

export function adjacentComparisonRanges(
  currentTime: number,
  duration: number,
  windowSeconds = 15,
): [Span, Span] {
  const safeDuration = Number.isFinite(duration)
    ? Math.max(duration, MIN_RANGE_SECONDS * 2)
    : 30;
  const safeWindow = Math.min(
    Math.max(
      Number.isFinite(windowSeconds) ? windowSeconds : 15,
      MIN_RANGE_SECONDS,
    ),
    safeDuration / 2,
  );
  const centre = clampTime(currentTime, safeDuration);
  let firstStart = centre - safeWindow;
  let secondStart = centre;
  if (firstStart < 0) {
    firstStart = 0;
    secondStart = safeWindow;
  } else if (secondStart + safeWindow > safeDuration) {
    secondStart = safeDuration - safeWindow;
    firstStart = secondStart - safeWindow;
  }
  return [
    { start_s: firstStart, end_s: firstStart + safeWindow },
    { start_s: secondStart, end_s: secondStart + safeWindow },
  ];
}

export function sanitizePlayerAction(
  action: PlayerAction,
  duration: number,
): PlayerAction | null {
  if (action.type === "seek") {
    return {
      ...action,
      time_s: clampTime(action.time_s, duration),
    };
  }
  if (action.type === "set_ab") {
    const a = normalizeRange(action.a, duration);
    const b = normalizeRange(action.b, duration);
    return a && b ? { ...action, a, b } : null;
  }
  const range = normalizeRange(action, duration);
  if (!range) return null;
  return { ...action, ...range };
}

export function spanOverlaps(first: Span, second: Span): boolean {
  return first.start_s < second.end_s && second.start_s < first.end_s;
}
