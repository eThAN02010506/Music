export type JobState = "queued" | "running" | "completed" | "failed" | "cancelled";

export interface JobSnapshot {
  id: string;
  state: JobState;
  stage: string;
  progress: number;
  message: string;
  result_url: string | null;
  error: string | null;
  revision: number;
}

export interface Span {
  start_s: number;
  end_s: number;
}

export interface Evidence {
  id: string;
  source: string;
  kind: "observed" | "inferred" | "interpretive" | "computed";
  text: string;
  confidence: number | null;
  span: Span | null;
  metadata: Record<string, unknown>;
}

export interface LyricsSegment {
  text: string;
  span: Span | null;
  language: string | null;
  confidence: number | null;
}

export interface DspResult {
  bpm: number | null;
  bpm_confidence: number | null;
  bpm_candidates: number[];
  bpm_ambiguous: boolean;
  key: string | null;
  key_confidence: number | null;
  energy_curve: Evidence[];
  evidence: Evidence[];
}

export interface AnalysisResult {
  title: string | null;
  summary: string;
  lyrics: LyricsSegment[];
  instruments: string[];
  sound_events: Evidence[];
  emotion_timeline: Evidence[];
  inferred_atmosphere: Evidence[];
  themes: string[];
  technical_metrics: DspResult;
  evidence: Evidence[];
  warnings: string[];
}

export interface HealthResult {
  status: string;
  model_endpoint: string;
  mode: string;
  local_model_root: string;
  local_runner_available: boolean;
}

export interface HistorySummary {
  id: string;
  title: string;
  file_name: string;
  language: string | null;
  state: string;
  created_at: string;
  updated_at: string;
  error: string | null;
  summary: string | null;
  duration_s: number | null;
  lyrics_count: number;
  instruments: string[];
  bpm: number | null;
  model_source: "network" | "local";
  model_location: string | null;
}

export interface HistoryDetail extends HistorySummary {
  result: AnalysisResult | null;
  audio_url: string | null;
}
