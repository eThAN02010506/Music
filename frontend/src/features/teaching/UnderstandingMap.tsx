import { percent } from "../../format";
import { localizedProse, matchesUiLanguage, useI18n } from "../../i18n";
import type {
  AnalysisEvidenceRef,
  MusicUnderstandingMap,
  UnderstandingEvent,
} from "../../types";
import {
  CLAIM_LABELS,
  DIMENSION_LABELS,
} from "./teachingLabels";
import { TimeRangeButton } from "./TimeRangeButton";

function EvidenceList({ values }: { values: AnalysisEvidenceRef[] }) {
  const { locale, t } = useI18n();
  return (
    <ul className="teaching-evidence-list">
      {values.map((evidence) => (
        <li
          key={`${evidence.source_type}-${evidence.source_id}`}
          className={`claim-${evidence.claim_type}`}
        >
          <span>{t(CLAIM_LABELS[evidence.claim_type])}</span>
          <strong>{t(DIMENSION_LABELS[evidence.dimension])}</strong>
          <p>
            {matchesUiLanguage(evidence.statement, locale)
              ? evidence.statement
              : t("原始证据使用另一种语言；这里保留其类型、时间与支持度。")}
          </p>
          {evidence.confidence != null && (
            <small>{t("支持度")} {percent(evidence.confidence)}</small>
          )}
        </li>
      ))}
    </ul>
  );
}

function UnderstandingEventCard({
  event,
  index,
}: {
  event: UnderstandingEvent;
  index: number;
}) {
  const { t, locale } = useI18n();
  return (
    <article className="understanding-event">
      <header>
        <div>
          <span>{event.section}</span>
          <small>{t("理解节点")} {String(index + 1).padStart(2, "0")}</small>
        </div>
        <TimeRangeButton range={event} />
      </header>
      <div className="event-felt">
        <span>{t("感受到什么")}</span>
        <h3>
          {localizedProse(
            event.interpretation,
            locale,
            t("回答未通过界面语言一致性检查，暂不展示原文。"),
          )}
        </h3>
      </div>
      <div className="event-step">
        <span>{t("听到了什么")}</span>
        <p>
          {localizedProse(
            event.observation,
            locale,
            t("回答未通过界面语言一致性检查，暂不展示原文。"),
          )}
        </p>
        <EvidenceList values={event.audio_evidence} />
      </div>
      <div className="event-step">
        <span>{t("它在表达中起什么作用")}</span>
        <p>
          {localizedProse(
            event.expressive_role,
            locale,
            t("回答未通过界面语言一致性检查，暂不展示原文。"),
          )}
        </p>
      </div>
      {event.lyrics_context.length > 0 && (
        <div className="event-lyrics">
          <span>{t("附近歌词")}</span>
          {event.lyrics_context.map((line) => (
            <blockquote key={line.source_id}>{line.text}</blockquote>
          ))}
        </div>
      )}
      <div className="listening-task">
        <span>{t("立即复听任务")}</span>
        <p>{event.listening_task}</p>
        <TimeRangeButton range={event} label={t("循环完成任务")} loop />
      </div>
      {event.alternative_readings.length > 0 && (
        <details className="alternative-readings">
          <summary>{t("其他可能理解")}</summary>
          {event.alternative_readings.map((reading) => (
            <p key={reading}>{reading}</p>
          ))}
        </details>
      )}
      <footer>
        <span>{t("解释的证据支持度")}</span>
        <strong>{percent(event.confidence)}</strong>
      </footer>
    </article>
  );
}

export function UnderstandingMap({
  map,
}: {
  map: MusicUnderstandingMap;
}) {
  const { t } = useI18n();
  return (
    <section className="panel understanding-map">
      <header className="understanding-map-header">
        <div>
          <span className="section-number">{t("地图")}</span>
          <div>
            <h2>{t("音乐理解地图")}</h2>
            <p>{t("按时间复听：感受、声音事实、表达作用与开放理解")}</p>
          </div>
        </div>
        <small>{map.events.length} {t("个理解节点")}</small>
      </header>
      {map.events.length > 0 ? (
        <div className="understanding-event-list">
          {map.events.map((event, index) => (
            <UnderstandingEventCard
              key={event.id}
              event={event}
              index={index}
            />
          ))}
        </div>
      ) : (
        <p className="empty-copy">
          {t("当前歌曲缺少足够的时间证据，暂时没有生成详细理解节点。")}
        </p>
      )}
    </section>
  );
}
