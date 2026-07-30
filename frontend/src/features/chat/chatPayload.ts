import type {
  TeachingChatRequest,
} from "../../types";
import type { PlayerSnapshot } from "../player/playerTypes";

export type ChatRangeMode = "current" | "selection" | "compare";

interface ClientRequestCrypto {
  randomUUID?: () => string;
  getRandomValues: Crypto["getRandomValues"];
}

export function createClientRequestId(
  cryptoSource: ClientRequestCrypto = globalThis.crypto,
): string {
  if (typeof cryptoSource.randomUUID === "function") {
    try {
      return cryptoSource.randomUUID();
    } catch {
      // Some non-secure LAN origins expose the method but reject the call.
    }
  }

  const bytes = new Uint8Array(16);
  cryptoSource.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(
    bytes,
    (value) => value.toString(16).padStart(2, "0"),
  );
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10).join(""),
  ].join("-");
}

export function buildTeachingChatRequest({
  message,
  snapshot,
  mode,
  relistenPolicy = "auto",
  requestId,
  outputLanguage = "zh",
}: {
  message: string;
  snapshot: PlayerSnapshot;
  mode: ChatRangeMode;
  relistenPolicy?: TeachingChatRequest["relisten_policy"];
  requestId: string;
  outputLanguage?: "zh" | "en";
}): TeachingChatRequest {
  const selected = mode === "selection" ? snapshot.selectedRange : null;
  const compare = (
    mode === "compare" && snapshot.rangeA && snapshot.rangeB
  )
    ? [snapshot.rangeA, snapshot.rangeB]
    : [];
  return {
    client_request_id: requestId,
    message: message.trim(),
    current_time_s: snapshot.currentTime,
    selected_range: selected,
    compare_ranges: compare,
    relisten_policy: relistenPolicy,
    output_language: outputLanguage,
  };
}
