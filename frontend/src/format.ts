export function seconds(value: number | undefined | null) {
  if (value == null || Number.isNaN(value)) return "--:--";
  const minutes = Math.floor(value / 60);
  const rest = Math.floor(value % 60);
  return `${minutes}:${rest.toString().padStart(2, "0")}`;
}

export function percent(value: number | null | undefined) {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

export function confidenceClass(value: number | null | undefined) {
  if (value == null) return "neutral";
  if (value >= 0.75) return "good";
  if (value >= 0.4) return "medium";
  return "low";
}
