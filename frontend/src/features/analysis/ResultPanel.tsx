import { useMemo } from "react";

import { SignalMark } from "../../components/SignalMark";
import { percent } from "../../format";
import type {
  AnalysisResult,
  LyricsSegment,
  VocalPresence,
} from "../../types";
import { LyricsPanel } from "../lyrics/LyricsPanel";
import { InsightPlayer } from "../player/InsightPlayer";
import { PlayerProvider } from "../player/PlayerContext";
import { SingingComparison } from "../singing/SingingViews";
import { TeachingWorkspace } from "../teaching/TeachingWorkspace";
import { useTeachingExperience } from "../teaching/useTeachingExperience";
import { AnalysisEvidencePanels } from "./AnalysisEvidencePanels";

function analysisDuration(result: AnalysisResult): number {
  const ends = [
    ...result.lyrics.map((item) => item.span?.end_s || 0),
    ...result.emotion_timeline.map((item) => item.span?.end_s || 0),
    ...result.sound_events.map((item) => item.span?.end_s || 0),
    ...result.technical_metrics.evidence.map((item) => {
      const value = item.metadata.duration_s;
      return typeof value === "number" ? value : 0;
    }),
  ];
  return Math.max(...ends, 1);
}

export function ResultPanel({
  result,
  audioUrl,
  fileName,
  historyId,
  revisionCount,
  onSaveLyrics,
}: {
  result: AnalysisResult;
  audioUrl: string;
  fileName: string;
  historyId: string | null;
  revisionCount: number;
  onSaveLyrics: (lyrics: LyricsSegment[]) => Promise<void>;
}) {
  const durationFallback = useMemo(() => analysisDuration(result), [result]);
  const title = result.title || fileName.replace(/\.[^.]+$/, "");
  const teaching = useTeachingExperience(historyId);
  const vocalPresence: VocalPresence = result.vocal_presence ?? {
    status: "unknown",
    confidence: null,
    reason: "这份旧分析没有保存人声状态，需要重新分析后确认。",
    evidence_ids: [],
  };
  const instrumental = vocalPresence.status === "instrumental";
  const modeLabel = instrumental
    ? "纯器乐"
    : vocalPresence.status === "vocals"
      ? "有人声"
      : "未确认";

  return (
    <PlayerProvider durationFallback={durationFallback}>
      <div className="results">
        <section className="result-hero panel">
          <div className="record-art"><SignalMark /></div>
          <div className="track-info">
            <div className="section-kicker">GUIDED LISTENING READY</div>
            <h2>{title}</h2>
            <p>{result.summary}</p>
            <InsightPlayer
              audioUrl={audioUrl}
              title={title}
              historyId={historyId}
              vocalPresence={vocalPresence}
              sections={teaching.guide?.understanding_map?.sections}
            />
          </div>
          <div className="metric-strip">
            <div>
              <span>BPM</span>
              <strong>{result.technical_metrics.bpm?.toFixed(1) || "—"}</strong>
              <small>
                可信度 {percent(result.technical_metrics.bpm_confidence)}
                {result.technical_metrics.bpm_ambiguous
                  && result.technical_metrics.bpm_candidates.length > 1
                  ? ` · 候选 ${result.technical_metrics.bpm_candidates
                    .slice(1).join(" / ")}`
                  : ""}
              </small>
            </div>
            <div>
              <span>KEY</span>
              <strong>{result.technical_metrics.key || "—"}</strong>
              <small>
                可信度 {percent(result.technical_metrics.key_confidence)}
              </small>
            </div>
            <div>
              <span>MODE</span>
              <strong>{modeLabel}</strong>
              <small>
                可信度 {percent(vocalPresence.confidence)}
                {result.lyrics.length > 0 ? ` · ${result.lyrics.length} 段歌词` : ""}
              </small>
            </div>
          </div>
        </section>

        {result.warnings.length > 0 && (
          <section className="warning-box">
            <strong>分析提醒</strong>
            {result.warnings.map((warning) => <p key={warning}>{warning}</p>)}
          </section>
        )}

        <TeachingWorkspace
          historyId={historyId}
          guide={teaching.guide}
          profile={teaching.profile}
          loading={teaching.loading}
          generating={teaching.generating}
          error={teaching.error}
          instrumental={instrumental}
          onGenerate={teaching.generate}
          onLevelChange={teaching.updateLevel}
          onConceptToggle={teaching.toggleConcept}
        />

        <LyricsPanel
          result={result}
          historyId={historyId}
          revisionCount={revisionCount}
          duration={durationFallback}
          onSaveLyrics={onSaveLyrics}
        />

        <AnalysisEvidencePanels
          result={result}
          duration={durationFallback}
        />
        {!instrumental && (
          <SingingComparison
            key={historyId || "unsaved"}
            historyId={historyId}
          />
        )}
      </div>
    </PlayerProvider>
  );
}
