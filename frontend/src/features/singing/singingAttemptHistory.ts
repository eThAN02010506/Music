import type {
  SingingAttempt,
  SingingAttemptCursor,
} from "../../types";

export const SINGING_ATTEMPT_PAGE_SIZE = 10;

export type SingingAttemptPage = {
  items: SingingAttempt[];
  hasMore: boolean;
};

export function singingAttemptCursor(
  attempts: SingingAttempt[],
): SingingAttemptCursor | null {
  const last = attempts.at(-1);
  return last ? { created_at: last.created_at, id: last.id } : null;
}

export function isIdempotentSingingAttemptDelete(cause: unknown): boolean {
  return (
    typeof cause === "object"
    && cause !== null
    && "status" in cause
    && cause.status === 404
  );
}

export function mergeSingingAttemptPage(
  current: SingingAttempt[],
  response: SingingAttempt[],
  {
    reset,
    pageSize = SINGING_ATTEMPT_PAGE_SIZE,
  }: {
    reset: boolean;
    pageSize?: number;
  },
): SingingAttemptPage {
  const page = response.slice(0, pageSize);
  const combined = reset ? page : [...current, ...page];
  const seen = new Set<string>();
  const items = combined.filter((attempt) => {
    if (seen.has(attempt.id)) return false;
    seen.add(attempt.id);
    return true;
  });
  return {
    items,
    hasMore: response.length > pageSize,
  };
}

export function singingAttemptSource(source: string): string {
  if (source === "history") return "分析歌曲演唱";
  if (source === "standalone") return "独立演唱对比";
  return "演唱评分";
}
