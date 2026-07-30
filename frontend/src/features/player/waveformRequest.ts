import type { HistoryWaveform } from "../../types";

type WaveformLoader = () => Promise<HistoryWaveform>;

const inFlight = new Map<string, Promise<HistoryWaveform>>();

export function loadWaveformOnce(
  historyId: string,
  loader: WaveformLoader,
): Promise<HistoryWaveform> {
  const existing = inFlight.get(historyId);
  if (existing) return existing;
  const request = loader().finally(() => {
    if (inFlight.get(historyId) === request) {
      inFlight.delete(historyId);
    }
  });
  inFlight.set(historyId, request);
  return request;
}
