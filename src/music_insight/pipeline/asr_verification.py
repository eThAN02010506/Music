from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
import math
import re
from typing import Any, Literal

from music_insight.schemas import (
    AsrResult,
    AsrVerificationResult,
    Evidence,
    EvidenceType,
    LyricsSegment,
)


@dataclass(slots=True)
class AsrVerificationDecision:
    result: AsrResult
    primary: AsrResult
    verification: AsrVerificationResult
    status: Literal[
        "replacement_candidate",
        "corroboration_candidate",
        "verified_silence",
        "inconclusive",
    ]
    metrics: dict[str, Any]

    @property
    def candidate_applied(self) -> bool:
        return self.status in {
            "replacement_candidate",
            "corroboration_candidate",
        }

    @property
    def replaces_primary(self) -> bool:
        return self.status == "replacement_candidate"

    @property
    def requires_consistency_refresh(self) -> bool:
        return self.status in {"replacement_candidate", "verified_silence"}


class AsrVerificationFusion:
    """Conservatively replace primary lyrics with timestamped verifier output."""

    MAX_LYRICS_SEGMENTS = 240
    MAX_SEGMENT_SECONDS = 45.0
    MAX_UNITS_PER_SECOND = 9.0
    REPETITION_WINDOW_SECONDS = 45.0
    MAX_REPETITIONS_PER_WINDOW = 3
    MIN_VALID_SEGMENT_RATIO = 0.8
    MIN_TIMELINE_COVERAGE = 0.05
    MIN_CONFIDENCE_COVERAGE = 0.6
    MIN_SEGMENT_CONFIDENCE = 0.35
    MIN_MEAN_CONFIDENCE = 0.45
    MIN_NO_VOCALS_CONFIDENCE = 0.8
    MIN_TEXT_AGREEMENT = 0.88

    def evaluate(
        self,
        primary: AsrResult,
        verification: AsrVerificationResult,
        *,
        duration_s: float | None = None,
    ) -> AsrVerificationDecision:
        evidence = [*primary.evidence, *verification.evidence]
        if not verification.segments:
            if self._confirmed_no_vocals(verification):
                return self._verified_silence(
                    primary,
                    verification,
                    evidence=evidence,
                    reason="二次 ASR 明确返回了高置信无人声证据。",
                )
            return self._inconclusive(
                primary,
                verification,
                evidence=evidence,
                reasons=["空转写缺少高置信无人声证据"],
                metrics={"segments_received": 0},
            )

        if self._confirmed_no_vocals(verification):
            return self._verified_silence(
                primary,
                verification,
                evidence=evidence,
                reason=(
                    "二次 ASR 的文字片段与高置信无人声证据冲突；"
                    "已将这些文字视为幻觉。"
                ),
            )

        received = verification.segments_received
        if received > self.MAX_LYRICS_SEGMENTS:
            return self._inconclusive(
                primary,
                verification,
                evidence=evidence,
                reasons=[
                    f"片段数量超过安全上限 {self.MAX_LYRICS_SEGMENTS}"
                ],
                metrics={"segments_received": received},
            )

        effective_duration = (
            duration_s
            if duration_s is not None
            else verification.duration_s
        )
        verified, issues = self._filter_segments(
            verification.segments,
            duration_s=effective_duration,
        )
        globally_repetitive = self._globally_repetitive(verified)
        if globally_repetitive:
            issues.append("时间轴中少数文本被异常高频重复")

        valid_ratio = len(verified) / received
        high_confidence = [
            item.confidence
            for item in verified
            if item.confidence is not None
            and item.confidence >= self.MIN_SEGMENT_CONFIDENCE
        ]
        known_confidence = [
            item.confidence
            for item in verified
            if item.confidence is not None
        ]
        confidence_coverage = (
            len(high_confidence) / len(verified) if verified else 0.0
        )
        mean_confidence = (
            sum(known_confidence) / len(known_confidence)
            if known_confidence
            else 0.0
        )
        covered_seconds = self._covered_seconds(verified)
        timeline_coverage = (
            covered_seconds / effective_duration
            if effective_duration is not None and effective_duration > 0
            else 0.0
        )
        metrics = {
            "segments_received": received,
            "segments_valid": len(verified),
            "valid_ratio": valid_ratio,
            "confidence_coverage": confidence_coverage,
            "mean_known_confidence": mean_confidence,
            "covered_seconds": covered_seconds,
            "timeline_coverage": timeline_coverage,
            "text_agreement": self._text_agreement(
                primary.lyrics,
                verified,
            ),
            "issues": issues[:30],
        }
        failures: list[str] = []
        if not verified:
            failures.append("没有片段通过时间轴与文字质量检查")
        if valid_ratio < self.MIN_VALID_SEGMENT_RATIO:
            failures.append("有效片段比例不足")
        if timeline_coverage < self.MIN_TIMELINE_COVERAGE:
            failures.append("有效时间轴覆盖不足")
        if globally_repetitive:
            failures.append("时间轴存在全局异常重复")
        confidence_sufficient = (
            confidence_coverage >= self.MIN_CONFIDENCE_COVERAGE
            and mean_confidence >= self.MIN_MEAN_CONFIDENCE
        )
        all_confidence_missing = not known_confidence
        text_corroborated = (
            all_confidence_missing
            and metrics["text_agreement"] >= self.MIN_TEXT_AGREEMENT
        )
        if not confidence_sufficient and not text_corroborated:
            failures.append(
                "可信度不足，且未与主模型歌词形成强文本一致性"
            )
        if failures:
            return self._inconclusive(
                primary,
                verification,
                evidence=evidence,
                reasons=failures,
                metrics=metrics,
            )

        return AsrVerificationDecision(
            result=primary.model_copy(
                update={
                    "lyrics": verified,
                    "evidence": self._without_primary_transcript(evidence),
                }
            ),
            primary=primary,
            verification=verification,
            status=(
                "replacement_candidate"
                if confidence_sufficient
                else "corroboration_candidate"
            ),
            metrics=metrics,
        )

    def finalize_candidate(
        self,
        decision: AsrVerificationDecision,
    ) -> AsrResult:
        if not decision.candidate_applied:
            return decision.result
        replaces_primary = decision.replaces_primary
        verified = Evidence(
            id="asr.verifier.verified",
            source=decision.verification.model,
            kind=EvidenceType.COMPUTED,
            text=(
                (
                    "二次 ASR 已通过语言与证据质量门，并以充分置信证据"
                    "提供歌词文本与时间轴。"
                )
                if replaces_primary
                else (
                    "二次 ASR 缺少独立置信度，但与主模型歌词高度一致；"
                    "仅记录交叉印证，不用它改写主歌词。"
                )
            ),
            confidence=decision.verification.transcript_confidence,
            metadata={
                "status": "verified",
                "mode": (
                    "replacement" if replaces_primary else "corroboration"
                ),
                **decision.metrics,
                "primary_model": decision.primary.model,
                "verifier_model": decision.verification.model,
            },
        )
        base = decision.result if replaces_primary else decision.primary
        combined_evidence = [
            *base.evidence,
            *(
                decision.verification.evidence
                if not replaces_primary
                else []
            ),
            verified,
        ]
        return base.model_copy(
            update={
                "evidence": combined_evidence,
            }
        )

    def reject_candidate_language(
        self,
        decision: AsrVerificationDecision,
        language_checked: AsrResult,
    ) -> AsrResult:
        rejection_evidence = [
            item
            for item in language_checked.evidence
            if item.id == "asr.verifier.transcript.rejected"
        ]
        evidence = [
            *decision.primary.evidence,
            *decision.verification.evidence,
            *rejection_evidence,
        ]
        return self._inconclusive(
            decision.primary,
            decision.verification,
            evidence=evidence,
            reasons=["二次 ASR 歌词与指定语言不匹配"],
            metrics=decision.metrics,
        ).result

    @staticmethod
    def mark_unavailable(
        primary: AsrResult,
        *,
        source: str,
        error: BaseException,
    ) -> AsrResult:
        status_code = getattr(error, "status_code", None)
        evidence = Evidence(
            id="asr.verifier.unavailable",
            source=source,
            kind=EvidenceType.OBSERVED,
            text="歌词二次验证不可用，已保留统一模型歌词。",
            confidence=0.0,
            metadata={
                "status": "unverified",
                "error_type": error.__class__.__name__,
                **(
                    {"upstream_status": status_code}
                    if isinstance(status_code, int)
                    else {}
                ),
            },
        )
        return primary.model_copy(
            update={"evidence": [*primary.evidence, evidence]}
        )

    def _verified_silence(
        self,
        primary: AsrResult,
        verification: AsrVerificationResult,
        *,
        evidence: list[Evidence],
        reason: str,
    ) -> AsrVerificationDecision:
        final_evidence = Evidence(
            id="asr.verifier.verified_silence",
            source=verification.model,
            kind=EvidenceType.COMPUTED,
            text=f"{reason} 已移除主模型歌词候选。",
            confidence=verification.vocal_confidence,
            metadata={
                "status": "verified_silence",
                "primary_segments_removed": len(primary.lyrics),
                "segments_received": verification.segments_received,
                "vocals_detected": verification.vocals_detected,
            },
        )
        return AsrVerificationDecision(
            result=primary.model_copy(
                update={
                    "lyrics": [],
                    "evidence": [
                        *self._without_primary_transcript(evidence),
                        final_evidence,
                    ],
                }
            ),
            primary=primary,
            verification=verification,
            status="verified_silence",
            metrics=final_evidence.metadata,
        )

    @staticmethod
    def _inconclusive(
        primary: AsrResult,
        verification: AsrVerificationResult,
        *,
        evidence: list[Evidence],
        reasons: list[str],
        metrics: dict[str, Any],
    ) -> AsrVerificationDecision:
        inconclusive = Evidence(
            id="asr.verifier.inconclusive",
            source=verification.model,
            kind=EvidenceType.COMPUTED,
            text=(
                "歌词二次验证证据不足，已保留统一模型歌词："
                + "；".join(reasons[:6])
            ),
            confidence=0.0,
            metadata={
                "status": "inconclusive",
                "reasons": reasons[:12],
                **metrics,
            },
        )
        return AsrVerificationDecision(
            result=primary.model_copy(
                update={"evidence": [*evidence, inconclusive]}
            ),
            primary=primary,
            verification=verification,
            status="inconclusive",
            metrics=metrics,
        )

    def _confirmed_no_vocals(
        self,
        verification: AsrVerificationResult,
    ) -> bool:
        return (
            verification.vocals_detected is False
            and verification.vocal_confidence is not None
            and verification.vocal_confidence
            >= self.MIN_NO_VOCALS_CONFIDENCE
        )

    @staticmethod
    def _without_primary_transcript(
        evidence: list[Evidence],
    ) -> list[Evidence]:
        return [
            item
            for item in evidence
            if not item.id.startswith("omni.transcript")
        ]

    def _filter_segments(
        self,
        segments: list[LyricsSegment],
        *,
        duration_s: float | None,
    ) -> tuple[list[LyricsSegment], list[str]]:
        ordered = sorted(
            segments,
            key=lambda item: (
                item.span.start_s if item.span else math.inf,
                item.span.end_s if item.span else math.inf,
            ),
        )
        kept: list[LyricsSegment] = []
        issues: list[str] = []
        recent_repetitions: defaultdict[str, list[float]] = defaultdict(list)
        for item in ordered:
            span = item.span
            text = " ".join(item.text.split()).strip()
            if span is None:
                issues.append(f"缺少时间戳：{text[:24]}")
                continue
            segment_duration = span.end_s - span.start_s
            if segment_duration <= 0.08:
                issues.append(f"时间片段过短：{text[:24]}")
                continue
            if segment_duration > self.MAX_SEGMENT_SECONDS:
                issues.append(f"单个时间片段过长：{text[:24]}")
                continue
            if (
                duration_s is not None
                and span.end_s > duration_s + 0.25
            ):
                issues.append(f"时间片段超出音频长度：{text[:24]}")
                continue

            units = self._lyric_units(text)
            if not text or units == 0:
                issues.append("空白或不可识别的歌词片段")
                continue
            if (
                units >= 6
                and units / segment_duration > self.MAX_UNITS_PER_SECOND
            ):
                issues.append(f"文字密度异常：{text[:24]}")
                continue

            previous = kept[-1] if kept else None
            if previous is not None and previous.span is not None:
                overlap = previous.span.end_s - span.start_s
                shorter_duration = min(
                    previous.span.end_s - previous.span.start_s,
                    segment_duration,
                )
                if (
                    overlap > 0.5
                    and overlap / max(shorter_duration, 0.01) > 0.45
                ):
                    issues.append(f"时间明显重叠：{text[:24]}")
                    continue
                if (
                    self._normalized_text(previous.text)
                    == self._normalized_text(text)
                    and span.start_s - previous.span.end_s <= 3.0
                ):
                    issues.append(f"相邻片段异常重复：{text[:24]}")
                    continue

            normalized = self._normalized_text(text)
            recent = [
                value
                for value in recent_repetitions[normalized]
                if span.start_s - value <= self.REPETITION_WINDOW_SECONDS
            ]
            if len(recent) >= self.MAX_REPETITIONS_PER_WINDOW:
                issues.append(f"短时间内异常重复：{text[:24]}")
                recent_repetitions[normalized] = recent
                continue
            recent.append(span.start_s)
            recent_repetitions[normalized] = recent
            kept.append(item.model_copy(update={"text": text}))

        return kept, issues

    @staticmethod
    def _covered_seconds(segments: list[LyricsSegment]) -> float:
        spans = sorted(
            (
                (item.span.start_s, item.span.end_s)
                for item in segments
                if item.span is not None
            ),
        )
        if not spans:
            return 0.0
        total = 0.0
        start, end = spans[0]
        for next_start, next_end in spans[1:]:
            if next_start <= end:
                end = max(end, next_end)
                continue
            total += end - start
            start, end = next_start, next_end
        return total + end - start

    @classmethod
    def _text_agreement(
        cls,
        primary: list[LyricsSegment],
        verification: list[LyricsSegment],
    ) -> float:
        primary_text = cls._canonical_transcript(primary)
        verification_text = cls._canonical_transcript(verification)
        if not primary_text or not verification_text:
            return 0.0
        return SequenceMatcher(
            None,
            primary_text,
            verification_text,
            autojunk=False,
        ).ratio()

    @staticmethod
    def _canonical_transcript(segments: list[LyricsSegment]) -> str:
        tokens: list[str] = []
        for item in segments:
            tokens.extend(
                re.findall(
                    (
                        r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]"
                        r"|[a-z0-9]+(?:['’][a-z]+)?"
                    ),
                    item.text.casefold(),
                )
            )
        return " ".join(tokens)

    @classmethod
    def _globally_repetitive(cls, segments: list[LyricsSegment]) -> bool:
        if len(segments) < 8:
            return False
        normalized = [
            cls._normalized_text(item.text)
            for item in segments
            if cls._normalized_text(item.text)
        ]
        if len(normalized) < 8:
            return False
        counts = Counter(normalized)
        distinct_ratio = len(counts) / len(normalized)
        dominant_ratio = counts.most_common(1)[0][1] / len(normalized)
        return distinct_ratio <= 0.25 or dominant_ratio >= 0.6

    @staticmethod
    def _normalized_text(text: str) -> str:
        return "".join(
            re.findall(
                r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]|[a-z0-9]+",
                text.casefold(),
            )
        )

    @staticmethod
    def _lyric_units(text: str) -> int:
        cjk = len(
            re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text)
        )
        latin_words = len(
            re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?", text)
        )
        return cjk + latin_words
