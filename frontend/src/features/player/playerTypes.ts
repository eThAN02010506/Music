import type { Span } from "../../types";

export type PlayerRangeSlot = "selection" | "a" | "b";

export type PlayerAction =
  | { type: "seek"; time_s: number; label?: string }
  | { type: "play_range"; start_s: number; end_s: number; label?: string }
  | { type: "loop_range"; start_s: number; end_s: number; label?: string }
  | {
    type: "set_ab";
    a: Span;
    b: Span;
    label?: string;
  };

export type ActivePlayback =
  | { mode: "once"; range: Span }
  | { mode: "loop"; range: Span }
  | {
    mode: "ab";
    rangeA: Span;
    rangeB: Span;
    phase: "a" | "b";
  };

export interface PlayerSnapshot {
  currentTime: number;
  duration: number;
  playing: boolean;
  selectedRange: Span | null;
  rangeA: Span | null;
  rangeB: Span | null;
  activePlayback: ActivePlayback | null;
}
