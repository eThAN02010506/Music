import { seconds } from "../../format";
import { useI18n } from "../../i18n";
import type { AnalysisResult, Evidence } from "../../types";
import { Confidence } from "../../components/Confidence";
import { usePlayer } from "../player/PlayerContext";

function TagList({
  items,
  empty,
}: {
  items: string[];
  empty?: string;
}) {
  const { t } = useI18n();
  if (!items.length) {
    return <p className="empty-copy">{empty || t("暂无可靠结果")}</p>;
  }
  return (
    <div className="tag-list">
      {items.map((item) => <span key={item}>{item}</span>)}
    </div>
  );
}

export function AnalysisEvidencePanels({
  result,
  duration,
}: {
  result: AnalysisResult;
  duration: number;
}) {
  const { t } = useI18n();
  const player = usePlayer();
  return (
    <>
      <section className="panel evidence-appendix">
        <details>
          <summary>
            <span>
              <strong>{t("技术证据附录")}</strong>
              <small>{t("乐器、主题、情绪和氛围推断")}</small>
            </span>
            <span aria-hidden="true">{t("展开")}</span>
          </summary>
          <div className="result-grid">
            <section className="compact-panel evidence-subpanel">
              <header><h3>{t("乐器与声源")}</h3></header>
              <TagList items={result.instruments} empty={t("没有确认到具体乐器")} />
              {result.sound_events.length > 0 && (
                <div className="event-list">
                  {result.sound_events.map((event) => (
                    <button
                      key={event.id}
                      onClick={() => player.seek(event.span?.start_s ?? 0)}
                    >
                      <span>{event.text}</span>
                      <small>
                        {seconds(event.span?.start_s)}–
                        {seconds(event.span?.end_s)}
                      </small>
                    </button>
                  ))}
                </div>
              )}
            </section>
            <section className="compact-panel evidence-subpanel">
              <header><h3>{t("主题")}</h3></header>
              <TagList items={result.themes} />
            </section>
          </div>

          <section className="emotion-panel evidence-subpanel">
            <header>
              <h3>{t("直接情绪证据")}</h3>
              <small>{t("来自音色、力度与演唱方式")}</small>
            </header>
            {result.emotion_timeline.length ? (
              <div className="timeline">
                <div className="timeline-ruler">
                  <span>0:00</span>
                  <span>{seconds(duration / 2)}</span>
                  <span>{seconds(duration)}</span>
                </div>
                <div className="timeline-track">
                  {result.emotion_timeline.map((item: Evidence) => (
                    <button
                      key={item.id}
                      style={{
                        left: `${((item.span?.start_s || 0) / duration) * 100}%`,
                        width: `${Math.max(((
                          (item.span?.end_s || duration)
                          - (item.span?.start_s || 0)
                        ) / duration) * 100, 7)}%`,
                      }}
                      onClick={() => player.seek(item.span?.start_s ?? 0)}
                    >
                      <span>{item.text}</span>
                      <Confidence value={item.confidence} />
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <p className="empty-copy">
                {t("模型没有确认直接情绪；这不等于音乐没有氛围。")}
              </p>
            )}
          </section>

          <section className="atmosphere-panel evidence-subpanel">
            <header>
              <h3>{t("推断氛围")}</h3>
              <small>{t("非直接听觉证据")}</small>
            </header>
            {result.inferred_atmosphere.length ? (
              <div className="atmosphere-grid">
                {result.inferred_atmosphere.map((item) => (
                  <article key={item.id}>
                    <div>
                      <strong>{item.text}</strong>
                      <Confidence value={item.confidence} />
                    </div>
                    <p>
                      {String(
                        item.metadata.basis
                        || t("由歌词、节奏和声音描述综合推断"),
                      )}
                    </p>
                  </article>
                ))}
              </div>
            ) : (
              <p className="empty-copy">{t("证据不足，未生成推断氛围")}</p>
            )}
          </section>
        </details>
      </section>
    </>
  );
}
