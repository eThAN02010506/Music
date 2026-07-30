import type {
  ListenerLevel,
  ListenerProfile,
  TeachingGuideResponse,
} from "../../types";
import { useI18n } from "../../i18n";
import { ListeningChat } from "../chat/ListeningChat";
import { TeachingOverview } from "./TeachingOverview";
import { UnderstandingMap } from "./UnderstandingMap";
import type { TeachingGenerationOptions } from "./useTeachingExperience";

export function TeachingWorkspace({
  historyId,
  guide,
  profile,
  loading,
  generating,
  error,
  instrumental,
  onGenerate,
  onLevelChange,
  onConceptToggle,
}: {
  historyId: string | null;
  guide: TeachingGuideResponse | null;
  profile: ListenerProfile;
  loading: boolean;
  generating: boolean;
  error: string;
  instrumental: boolean;
  onGenerate: (options?: TeachingGenerationOptions) => Promise<unknown>;
  onLevelChange: (level: ListenerLevel) => Promise<void>;
  onConceptToggle: (concept: string) => Promise<void>;
}) {
  const { t } = useI18n();
  if (!historyId) {
    return (
      <section className="panel teaching-gate">
        <span className="section-kicker">INTERACTIVE MUSIC TEACHER</span>
        <h2>{t("保存分析后，开始可交互导赏")}</h2>
        <p>
          {t("导赏地图和歌曲对话需要一个稳定的歌曲 ID；分析保存后即可生成，不会重复分析整首歌曲。")}
        </p>
      </section>
    );
  }

  const map = guide?.understanding_map ?? null;
  if (loading && !map) {
    return (
      <section className="panel teaching-gate" aria-busy="true">
        <span className="section-kicker">INTERACTIVE MUSIC TEACHER</span>
        <h2>{t("正在读取这首歌的导赏地图…")}</h2>
        <p>{t("基础分析仍可正常查看和播放。")}</p>
      </section>
    );
  }

  if (!map) {
    const pending = guide?.status === "pending" || generating;
    return (
      <section className="panel teaching-gate" aria-busy={pending}>
        <span className="section-kicker">INTERACTIVE MUSIC TEACHER</span>
        <h2>
          {pending
            ? t("正在准备可交互的音乐导赏")
            : t("把分析变成一堂可复听的音乐导赏课")}
        </h2>
        <p>
          {pending
            ? t("正在整理现有歌词、DSP 与时间证据；完成后会自动打开边听边问，不会重复提交。")
            : t("系统会先用现有歌词、DSP 与带时间的听觉证据快速建立基础地图；失败时不会影响原有分析。")}
        </p>
        {error && <p className="teaching-error">{t(error)}</p>}
        <button
          type="button"
          disabled={pending}
          onClick={() => void onGenerate({ strategy: "evidence" })}
        >
          {pending ? t("正在整理时间证据…") : t("立即准备基础导赏")}
        </button>
      </section>
    );
  }

  return (
    <div className="teaching-experience">
      {(guide?.status === "pending" || guide?.stale || error) && (
        <div className="teaching-status">
          <div>
            <strong>
              {guide?.status === "pending"
                ? t("当前地图可用，模型正在增强")
                : guide?.stale
                  ? t("导赏地图需要更新")
                  : t("部分功能暂不可用")}
            </strong>
            <p>
              {guide?.status === "pending"
                ? t("边听边问会继续使用现有时间证据，增强完成后自动采用新地图。")
                : guide?.stale
                  ? t("歌词或基础分析已经变化，旧地图仍可查看，但不再作为最新证据。")
                  : t(error)}
            </p>
          </div>
          {guide?.stale && (
            <button
              type="button"
              disabled={generating}
              onClick={() => void onGenerate({
                force: true,
                strategy: "model",
              })}
            >
              {generating ? t("正在重建…") : t("按最新证据重建")}
            </button>
          )}
        </div>
      )}
      {guide?.status === "complete" && (
        <div className="teaching-enhance">
          <span>{t("基础证据地图与边听边问已经可用。")}</span>
          <button
            type="button"
            disabled={generating}
            onClick={() => void onGenerate({
              force: true,
              strategy: "model",
            })}
          >
            {generating ? t("模型正在增强…") : t("用当前模型增强导赏")}
          </button>
        </div>
      )}
      <TeachingOverview map={map} />
      <LearningProgress
        profile={profile}
        instrumental={instrumental}
        onConceptToggle={onConceptToggle}
      />
      <div className="teaching-main-grid">
        <ListeningChat
          historyId={historyId}
          map={map}
          profile={profile}
          onLevelChange={onLevelChange}
        />
        <UnderstandingMap map={map} />
      </div>
    </div>
  );
}

const TRAINING_CONCEPTS = [
  "段落结构",
  "节奏与律动",
  "旋律走向",
  "和声色彩",
  "音色",
  "力度变化",
  "配器层次",
  "空间感",
  "歌词与音乐关系",
];

function LearningProgress({
  profile,
  instrumental,
  onConceptToggle,
}: {
  profile: ListenerProfile;
  instrumental: boolean;
  onConceptToggle: (concept: string) => Promise<void>;
}) {
  const { t } = useI18n();
  const concepts = instrumental
    ? TRAINING_CONCEPTS.filter((concept) => concept !== "歌词与音乐关系")
    : TRAINING_CONCEPTS;
  const learned = new Set(profile.learned_concepts);
  const learnedVisible = concepts.filter((concept) => learned.has(concept));
  const nextConcept = concepts.find((concept) => !learned.has(concept));
  return (
    <section className="panel learning-progress">
      <header>
        <div>
          <span className="section-kicker">LISTENING PRACTICE</span>
          <h3>{t("我的听觉训练进度")}</h3>
        </div>
        <small>{learnedVisible.length}/{concepts.length} {t("个核心概念")}</small>
      </header>
      <p>
        {t("只有你确认“已经能听出”后才会记录；系统不会因为回答过一次就自动判定学会。")}
        {nextConcept && ` ${t("下一项建议关注：")}${t(nextConcept)}。`}
      </p>
      <div className="concept-progress-list">
        {concepts.map((concept) => (
          <button
            key={concept}
            type="button"
            className={learned.has(concept) ? "learned" : ""}
            aria-pressed={learned.has(concept)}
            onClick={() => void onConceptToggle(concept)}
          >
            <span aria-hidden="true">{learned.has(concept) ? "✓" : "+"}</span>
            {t(concept)}
          </button>
        ))}
      </div>
    </section>
  );
}
