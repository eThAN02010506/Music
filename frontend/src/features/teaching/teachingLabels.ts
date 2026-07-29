import type {
  AudioDimension,
  EvidenceClaimType,
} from "../../types";

export const CLAIM_LABELS: Readonly<Record<EvidenceClaimType, string>> = {
  observed_fact: "直接听觉事实",
  computed_fact: "DSP 计算事实",
  grounded_interpretation: "有依据的解释",
  possible_reading: "可能的理解",
};

export const DIMENSION_LABELS: Readonly<Record<AudioDimension, string>> = {
  melody: "旋律",
  harmony: "和声",
  rhythm: "节奏",
  timbre: "音色",
  dynamics: "力度",
  instrumentation: "配器",
  space: "空间感",
  lyrics: "歌词",
  structure: "曲式",
  other: "其他",
};
