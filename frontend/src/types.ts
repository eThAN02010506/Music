export type JobState = "queued" | "running" | "completed" | "failed" | "cancelled";

export interface JobSnapshot {
  id: string;
  state: JobState;
  stage: string;
  progress: number;
  message: string;
  result_url: string | null;
  error: string | null;
  persistence_error: string | null;
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
  mode: string;
}

export interface RuntimeConfig {
  model_endpoint: string;
  local_model_root: string;
  local_runner_available: boolean;
}

export interface ModelProbeResult {
  endpoint: string;
  online: boolean;
  model: string | null;
  protocol: string | null;
  analysis_supported: boolean | null;
  audio_supported: boolean | null;
  openai_audio_supported: boolean | null;
  service: string;
  detail: string;
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
  revision_count: number;
}

export interface HistoryRevision {
  id: number;
  created_at: string;
  lyrics: LyricsSegment[];
}

export interface HistoryWaveform {
  duration_s: number;
  peaks: number[][];
  points_per_channel: number;
}

export interface LyricsRetryResult {
  start_s: number;
  end_s: number;
  lyrics: LyricsSegment[];
  issues: string[];
  source: string;
}

export interface SingingScore {
  total: number;
  pitch: number;
  rhythm: number;
  completeness: number;
  stability: number;
  median_pitch_error: number | null;
  in_tune_ratio: number | null;
  reference_duration_s: number;
  performance_duration_s: number;
  pitch_curve: Array<{
    progress: number;
    reference_midi: number | null;
    performance_midi: number | null;
    error_semitones: number | null;
  }>;
  notes: string[];
}

export interface User {
  id: string;
  username: string;
  created_at: string;
}

export interface LeaderboardEntry {
  rank: number;
  username: string;
  total: number;
  pitch: number;
  rhythm: number;
  completeness: number;
  stability: number;
  created_at: string;
  attempts: number;
  source: string;
  is_current_user: boolean;
}

export interface Leaderboard {
  category: string;
  period: string;
  generated_at: string;
  entries: LeaderboardEntry[];
}

export interface SingingAttempt {
  id: string;
  user_id: string;
  source: string;
  category: string;
  history_id: string | null;
  reference_name: string | null;
  performance_name: string | null;
  created_at: string;
  score: SingingScore;
}

export interface SingingAttemptCursor {
  created_at: string;
  id: string;
}

export type AudioDimension =
  | "melody"
  | "harmony"
  | "rhythm"
  | "timbre"
  | "dynamics"
  | "instrumentation"
  | "space"
  | "lyrics"
  | "structure"
  | "other";

export type EvidenceClaimType =
  | "observed_fact"
  | "computed_fact"
  | "grounded_interpretation"
  | "possible_reading";

export interface AnalysisEvidenceRef {
  source_type:
    | "analysis_evidence"
    | "lyrics"
    | "metric"
    | "understanding_event"
    | "relisten";
  source_id: string;
  dimension: AudioDimension;
  statement: string;
  claim_type: EvidenceClaimType;
  span: Span | null;
  confidence: number | null;
}

export interface LyricsContext {
  source_id: string;
  text: string;
  span: Span | null;
  language: string | null;
  confidence: number | null;
}

export interface SectionMarker {
  id: string;
  label: string;
  span: Span;
  expressive_role: string;
  confidence: number;
  alternative_labels: string[];
}

export interface EmotionalArcPoint {
  span: Span;
  description: string;
  evidence_refs: AnalysisEvidenceRef[];
  confidence: number;
}

export interface UnderstandingEvent {
  id: string;
  start_s: number;
  end_s: number;
  section: string;
  observation: string;
  interpretation: string;
  expressive_role: string;
  audio_evidence: AnalysisEvidenceRef[];
  lyrics_context: LyricsContext[];
  listening_task: string;
  alternative_readings: string[];
  confidence: number;
}

export interface KeyMoment {
  id: string;
  event_id: string;
  start_s: number;
  end_s: number;
  reason: string;
  listening_task: string;
  confidence: number;
}

export interface MusicUnderstandingMap {
  schema_version: number;
  core_expression: string;
  overall_atmosphere: string;
  emotional_arc: EmotionalArcPoint[];
  sections: SectionMarker[];
  events: UnderstandingEvent[];
  key_moments: KeyMoment[];
  confidence: number;
  warnings: string[];
  generated_at: string;
}

export interface AnswerTimeRange {
  id: string;
  start_s: number;
  end_s: number;
  label: string;
  purpose: string;
}

export interface AnswerEvidence {
  id: string;
  statement: string;
  claim_type: EvidenceClaimType;
  dimension: AudioDimension;
  source_refs: string[];
  time_range_ids: string[];
  confidence: number;
}

export interface TeachingListeningTask {
  instruction: string;
  focus: AudioDimension;
  time_range_id: string;
}

export interface TeachingPlayerAction {
  type: "seek" | "play_range" | "loop_range" | "compare_ab";
  time_range_id: string;
  comparison_time_range_id: string | null;
  label: string;
}

export interface TeachingChatResponse {
  answer: string;
  time_ranges: AnswerTimeRange[];
  evidence: AnswerEvidence[];
  listening_task: TeachingListeningTask;
  suggested_questions: string[];
  player_actions: TeachingPlayerAction[];
  alternative_readings: string[];
  warnings: string[];
  confidence: number;
  relistened: boolean;
  insufficient_evidence: boolean;
}

export type ListenerLevel =
  | "beginner"
  | "curious"
  | "intermediate"
  | "advanced";

export interface ListenerProfile {
  level: ListenerLevel;
  preferences: Record<string, string>;
  learned_concepts: string[];
}

export interface TeachingGuideResponse {
  analysis_id: string;
  schema_version: number;
  source_result_hash: string;
  status: "pending" | "complete" | "stale" | "failed";
  understanding_map: MusicUnderstandingMap | null;
  stale: boolean;
  cached: boolean;
  error: string | null;
  updated_at: string | null;
}

export interface TeachingConversation {
  id: string;
  analysis_id: string;
  title: string | null;
  summary: string | null;
  message_count: number;
  created_at: string;
  updated_at: string;
}

export interface TeachingChatRequest {
  client_request_id: string;
  message: string;
  current_time_s: number;
  selected_range: Span | null;
  compare_ranges: Span[];
  relisten_policy: "never" | "auto" | "always";
}

export interface TeachingMessage {
  id: string;
  conversation_id: string;
  sequence: number;
  status: "pending" | "complete" | "failed";
  client_request_id: string;
  request: TeachingChatRequest;
  response: TeachingChatResponse | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}
