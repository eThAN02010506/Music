import { percent } from "../../format";
import type { MusicUnderstandingMap } from "../../types";
import { TimeRangeButton } from "./TimeRangeButton";

export function TeachingOverview({
  map,
}: {
  map: MusicUnderstandingMap;
}) {
  return (
    <section className="panel teaching-overview">
      <div className="teaching-hero">
        <div>
          <span className="section-kicker">MUSIC LISTENING GUIDE</span>
          <h2>这首歌在表达什么？</h2>
          <p className="core-expression">{map.core_expression}</p>
        </div>
        <div className="map-confidence">
          <span>证据支持度</span>
          <strong>{percent(map.confidence)}</strong>
          <small>不是“唯一答案概率”</small>
        </div>
      </div>
      <div className="overall-atmosphere">
        <span>整体意境</span>
        <p>{map.overall_atmosphere}</p>
      </div>

      {map.emotional_arc.length > 0 && (
        <div className="teaching-arc">
          <h3>情绪发展弧线</h3>
          <div>
            {map.emotional_arc.map((point, index) => (
              <article key={`${point.span.start_s}-${index}`}>
                <TimeRangeButton range={point.span} />
                <p>{point.description}</p>
                <small>支持度 {percent(point.confidence)}</small>
              </article>
            ))}
          </div>
        </div>
      )}

      {map.sections.length > 0 && (
        <div className="section-guides">
          <h3>段落在承担什么作用？</h3>
          <div>
            {map.sections.map((section) => (
              <article key={section.id}>
                <TimeRangeButton
                  range={section.span}
                  label={section.label}
                />
                <p>{section.expressive_role}</p>
                {section.alternative_labels.length > 0 && (
                  <small>
                    也可能理解为：{section.alternative_labels.join("、")}
                  </small>
                )}
              </article>
            ))}
          </div>
        </div>
      )}

      {map.key_moments.length > 0 && (
        <div className="key-moments">
          <h3>最值得重听的关键时刻</h3>
          <div>
            {map.key_moments.map((moment, index) => (
              <article key={moment.id}>
                <span className="moment-number">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <div>
                  <TimeRangeButton
                    range={moment}
                    label="重听"
                  />
                  <p>{moment.reason}</p>
                  <small>任务：{moment.listening_task}</small>
                </div>
              </article>
            ))}
          </div>
        </div>
      )}

      {map.warnings.length > 0 && (
        <details className="map-warnings">
          <summary>查看导赏的不确定性说明</summary>
          {map.warnings.map((warning) => <p key={warning}>{warning}</p>)}
        </details>
      )}
    </section>
  );
}
