import { seconds } from "../../format";
import type { Span } from "../../types";
import { usePlayer } from "../player/PlayerContext";

export function TimeRangeButton({
  range,
  label,
  loop = false,
}: {
  range: Span;
  label?: string;
  loop?: boolean;
}) {
  const player = usePlayer();
  return (
    <button
      type="button"
      className="time-range-button"
      title={loop ? "循环播放这段音频" : "跳转并播放这段音频"}
      onClick={() => player.playRange(range, loop)}
    >
      <span aria-hidden="true">▶</span>
      {label && <strong>{label}</strong>}
      <span>{seconds(range.start_s)}–{seconds(range.end_s)}</span>
    </button>
  );
}
