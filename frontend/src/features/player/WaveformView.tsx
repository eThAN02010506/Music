import { useEffect, useRef } from "react";

import WaveSurfer from "wavesurfer.js";
import RegionsPlugin, {
  type Region,
} from "wavesurfer.js/dist/plugins/regions.esm.js";

import type { HistoryWaveform, Span } from "../../types";
import { useI18n } from "../../i18n";
import { usePlayer, usePlayerSnapshot } from "./PlayerContext";

const USER_REGION_ID = "user-selection";
const EMPTY_SECTIONS: Array<Span & { id: string; label: string }> = [];

function updatePlayerSelection(
  player: ReturnType<typeof usePlayer>,
  region: Region,
) {
  player.setRange("selection", {
    start_s: region.start,
    end_s: region.end,
  });
}

export function WaveformView({
  media,
  waveform,
  sections,
}: {
  media: HTMLMediaElement;
  waveform: HistoryWaveform;
  sections?: Array<Span & { id: string; label: string }>;
}) {
  const { t } = useI18n();
  const containerRef = useRef<HTMLDivElement>(null);
  const player = usePlayer();
  const { selectedRange } = usePlayerSnapshot();
  const regionsRef = useRef<RegionsPlugin | null>(null);
  const visibleSections = sections ?? EMPTY_SECTIONS;

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const regions = RegionsPlugin.create();
    const wavesurfer = WaveSurfer.create({
      container,
      media,
      peaks: waveform.peaks,
      duration: waveform.duration_s,
      waveColor: "#42554b",
      progressColor: "#a7f07b",
      cursorColor: "#f0cb7b",
      cursorWidth: 1,
      height: 92,
      normalize: true,
      dragToSeek: false,
      plugins: [regions],
    });
    regionsRef.current = regions;
    for (const section of visibleSections) {
      regions.addRegion({
        id: `section-${section.id}`,
        start: section.start_s,
        end: section.end_s,
        content: section.label,
        color: "rgba(240, 203, 123, 0.08)",
        drag: false,
        resize: false,
      });
    }
    const disableDragSelection = regions.enableDragSelection(
      {
        id: USER_REGION_ID,
        color: "rgba(167, 240, 123, 0.18)",
        drag: true,
        resize: true,
        minLength: 0.25,
        maxLength: 30,
      },
      4,
    );
    const stopCreated = regions.on("region-created", (region) => {
      if (region.id.startsWith("section-")) return;
      for (const existing of regions.getRegions()) {
        if (
          existing !== region
          && existing.id === USER_REGION_ID
        ) existing.remove();
      }
      region.setOptions({ id: USER_REGION_ID });
      updatePlayerSelection(player, region);
    });
    const stopUpdated = regions.on("region-updated", (region) => {
      if (region.id === USER_REGION_ID) {
        updatePlayerSelection(player, region);
      }
    });
    const stopClicked = regions.on("region-clicked", (region, event) => {
      event.stopPropagation();
      if (region.id.startsWith("section-")) {
        player.seek(region.start);
      } else {
        updatePlayerSelection(player, region);
      }
    });
    return () => {
      disableDragSelection();
      stopCreated();
      stopUpdated();
      stopClicked();
      regionsRef.current = null;
      wavesurfer.destroy();
    };
  }, [media, player, visibleSections, waveform]);

  useEffect(() => {
    const regions = regionsRef.current;
    if (!regions) return;
    const existing = regions.getRegions().find(
      (region) => region.id === USER_REGION_ID,
    );
    if (!selectedRange) {
      existing?.remove();
      return;
    }
    if (existing) {
      const changed = (
        Math.abs(existing.start - selectedRange.start_s) > 0.02
        || Math.abs(existing.end - selectedRange.end_s) > 0.02
      );
      if (changed) {
        existing.setOptions({
          start: selectedRange.start_s,
          end: selectedRange.end_s,
        });
      }
      return;
    }
    regions.addRegion({
      id: USER_REGION_ID,
      start: selectedRange.start_s,
      end: selectedRange.end_s,
      color: "rgba(167, 240, 123, 0.18)",
      drag: true,
      resize: true,
      minLength: 0.25,
      maxLength: 30,
    });
  }, [selectedRange]);

  return (
    <div className="waveform-shell">
      <div
        ref={containerRef}
        className="waveform-canvas"
        role="img"
        aria-label={t("歌曲波形；拖动可选择最多 30 秒")}
      />
      <small>{t("拖动波形框选，点击段落或回答中的时间可跳转")}</small>
    </div>
  );
}
