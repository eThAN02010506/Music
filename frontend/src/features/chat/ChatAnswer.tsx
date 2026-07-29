import { percent } from "../../format";
import type {
  AnswerTimeRange,
  TeachingChatResponse,
  TeachingPlayerAction,
} from "../../types";
import { usePlayer } from "../player/PlayerContext";
import type { PlayerAction } from "../player/playerTypes";
import {
  CLAIM_LABELS,
  DIMENSION_LABELS,
} from "../teaching/teachingLabels";
import { TimeRangeButton } from "../teaching/TimeRangeButton";

function findRange(
  response: TeachingChatResponse,
  id: string,
): AnswerTimeRange | undefined {
  return response.time_ranges.find((range) => range.id === id);
}

function playerAction(
  response: TeachingChatResponse,
  action: TeachingPlayerAction,
): PlayerAction | null {
  const primary = findRange(response, action.time_range_id);
  if (!primary) return null;
  if (action.type === "seek") {
    return { type: "seek", time_s: primary.start_s, label: action.label };
  }
  if (action.type === "play_range") {
    return { type: "play_range", ...primary, label: action.label };
  }
  if (action.type === "loop_range") {
    return { type: "loop_range", ...primary, label: action.label };
  }
  if (!action.comparison_time_range_id) return null;
  const comparison = findRange(response, action.comparison_time_range_id);
  if (!comparison) return null;
  return {
    type: "set_ab",
    a: primary,
    b: comparison,
    label: action.label,
  };
}

export function ChatAnswer({
  response,
  onSuggestedQuestion,
}: {
  response: TeachingChatResponse;
  onSuggestedQuestion: (question: string) => void;
}) {
  const player = usePlayer();
  const listeningRange = findRange(
    response,
    response.listening_task.time_range_id,
  );
  return (
    <article className="chat-answer">
      <div className="answer-heading">
        <span>导赏老师</span>
        <small>
          支持度 {percent(response.confidence)}
          {response.insufficient_evidence
            ? " · 证据不足模式"
            : response.relistened
              ? " · 已局部重听"
              : " · 基于已有证据"}
        </small>
      </div>
      <p className="answer-copy">{response.answer}</p>

      <div className="answer-time-ranges">
        {response.time_ranges.map((range) => (
          <TimeRangeButton
            key={range.id}
            range={range}
            label={range.label}
          />
        ))}
      </div>

      {response.evidence.length > 0 && (
        <details className="answer-evidence" open>
          <summary>听觉证据</summary>
          {response.evidence.map((evidence) => {
            const rangeIds = [...new Set(evidence.time_range_ids)];
            const ranges = rangeIds.flatMap((id) => {
              const range = findRange(response, id);
              return range ? [range] : [];
            });
            return (
              <div
                key={evidence.id}
                className={`claim-${evidence.claim_type}`}
              >
                <div className="answer-evidence-labels">
                  <span className="answer-claim-label">
                    {CLAIM_LABELS[evidence.claim_type]}
                  </span>
                  <strong>{DIMENSION_LABELS[evidence.dimension]}</strong>
                </div>
                <p>{evidence.statement}</p>
                {ranges.length > 0 && (
                  <div className="answer-evidence-ranges">
                    {ranges.map((range) => (
                      <TimeRangeButton
                        key={range.id}
                        range={range}
                        label={range.label}
                      />
                    ))}
                  </div>
                )}
                <small>支持度 {percent(evidence.confidence)}</small>
              </div>
            );
          })}
        </details>
      )}

      <div className="answer-listening-task">
        <span>马上复听</span>
        <p>{response.listening_task.instruction}</p>
        {listeningRange && (
          <TimeRangeButton
            range={listeningRange}
            label="循环这个任务"
            loop
          />
        )}
      </div>

      {response.player_actions.length > 0 && (
        <div className="answer-player-actions">
          {response.player_actions.map((action, index) => {
            const safe = playerAction(response, action);
            if (!safe) return null;
            return (
              <button
                type="button"
                key={`${action.type}-${action.time_range_id}-${index}`}
                onClick={() => player.execute(safe)}
              >
                {action.label}
              </button>
            );
          })}
        </div>
      )}

      {response.alternative_readings.length > 0 && (
        <details className="alternative-readings">
          <summary>其他可能理解</summary>
          {response.alternative_readings.map((reading) => (
            <p key={reading}>{reading}</p>
          ))}
        </details>
      )}

      {response.warnings.length > 0 && (
        <details className="answer-warnings">
          <summary>回答的不确定性与降级说明</summary>
          {response.warnings.map((warning) => (
            <p key={warning}>{warning}</p>
          ))}
        </details>
      )}

      {response.suggested_questions.length > 0 && (
        <div className="suggested-questions">
          <span>可以继续问</span>
          {response.suggested_questions.map((question) => (
            <button
              type="button"
              key={question}
              onClick={() => onSuggestedQuestion(question)}
            >
              {question}
            </button>
          ))}
        </div>
      )}
    </article>
  );
}
