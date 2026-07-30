const MAX_ERROR_DETAIL_LENGTH = 2_000;
const MAX_VALIDATION_ERRORS = 5;

export function formatApiErrorDetail(
  rawBody: string,
  statusText = "",
): string {
  const fallback = statusText.trim() || "请求失败";
  const raw = rawBody.trim();
  if (!raw) return fallback;

  try {
    const payload = JSON.parse(raw) as unknown;
    const detail = detailFromPayload(payload);
    if (detail) return bounded(detail);
  } catch {
    // A reverse proxy may return plain text or HTML instead of JSON.
  }

  if (/^\s*</.test(raw)) return fallback;
  return bounded(raw);
}

function detailFromPayload(payload: unknown): string {
  if (typeof payload === "string") return payload.trim();
  if (!isRecord(payload)) return "";
  const detail = payload.detail;
  if (typeof detail === "string") return detail.trim();
  if (!Array.isArray(detail)) return "";
  return detail
    .slice(0, MAX_VALIDATION_ERRORS)
    .map((item) => validationMessage(item))
    .filter(Boolean)
    .join("；");
}

function validationMessage(value: unknown): string {
  if (!isRecord(value) || typeof value.msg !== "string") return "";
  const message = value.msg.trim();
  const location = Array.isArray(value.loc)
    ? value.loc
      .filter((part) => typeof part === "string" || typeof part === "number")
      .map(String)
      .filter((part) => !["body", "query", "path"].includes(part))
      .join(".")
    : "";
  return location ? `${location}：${message}` : message;
}

function bounded(value: string): string {
  const normalized = value.trim();
  if (normalized.length <= MAX_ERROR_DETAIL_LENGTH) return normalized;
  return `${normalized.slice(0, MAX_ERROR_DETAIL_LENGTH - 1)}…`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
