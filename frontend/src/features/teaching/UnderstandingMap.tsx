import { percent } from "../../format";
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
  return (
    <ul className="teaching-evidence-list">
      {values.map((evidence) => (
        <li
          key={`${evidence.source_type}-${evidence.source_id}`}
          className={`claim-${evidence.claim_type}`}
        >
          <span>{CLAIM_LABELS[evidence.claim_type]}</span>
          <strong>{DIMENSION_LABELS[evidence.dimension]}</strong>
          <p>{evidence.statement}</p>
          {evidence.confidence != null && (
            <small>支持度 {percent(evidence.confidence)}</small>
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
  return (
    <article className="understanding-event">
      <header>
        <div>
          <span>{event.section}</span>
          <small>理解节点 {String(index + 1).padStart(2, "0")}</small>
        </div>
        <TimeRangeButton range={event} />
      </header>
      <div className="event-felt">
        <span>感受到什么</span>
        <h3>{event.interpretation}</h3>
      </div>
      <div className="event-step">
        <span>听到了什么</span>
        <p>{event.observation}</p>
        <EvidenceList values={event.audio_evidence} />
      </div>
      <div className="event-step">
        <span>它在表达中起什么作用</span>
        <p>{event.expressive_role}</p>
      </div>
      {event.lyrics_context.length > 0 && (
        <div className="event-lyrics">
          <span>附近歌词</span>
          {event.lyrics_context.map((line) => (
            <blockquote key={line.source_id}>{line.text}</blockquote>
          ))}
        </div>
      )}
      <div className="listening-task">
        <span>立即复听任务</span>
        <p>{event.listening_task}</p>
        <TimeRangeButton range={event} label="循环完成任务" loop />
      </div>
      {event.alternative_readings.length > 0 && (
        <details className="alternative-readings">
          <summary>其他可能理解</summary>
          {event.alternative_readings.map((reading) => (
            <p key={reading}>{reading}</p>
          ))}
        </details>
      )}
      <footer>
        <span>解释的证据支持度</span>
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
  return (
    <section className="panel understanding-map">
      <header className="understanding-map-header">
        <div>
          <span className="section-number">地图</span>
          <div>
            <h2>音乐理解地图</h2>
            <p>按时间复听：感受、声音事实、表达作用与开放理解</p>
          </div>
        </div>
        <small>{map.events.length} 个理解节点</small>
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
          当前歌曲缺少足够的时间证据，暂时没有生成详细理解节点。
        </p>
      )}
    </section>
  );
}
