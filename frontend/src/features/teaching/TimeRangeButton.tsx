import { seconds } from "../../format";
import { useI18n } from "../../i18n";
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
  const { t } = useI18n();
  const player = usePlayer();
  return (
    <button
      type="button"
      className="time-range-button"
      title={loop ? t("循环播放这段音频") : t("跳转并播放这段音频")}
      onClick={() => player.playRange(range, loop)}
    >
      <span aria-hidden="true">▶</span>
      {label && <strong>{label}</strong>}
      <span>{seconds(range.start_s)}–{seconds(range.end_s)}</span>
    </button>
  );
}
