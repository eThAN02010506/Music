import type {
  ListenerLevel,
  ListenerProfile,
  TeachingGuideResponse,
} from "../../types";
import { ListeningChat } from "../chat/ListeningChat";
import { TeachingOverview } from "./TeachingOverview";
import { UnderstandingMap } from "./UnderstandingMap";

export function TeachingWorkspace({
  historyId,
  guide,
  profile,
  loading,
  generating,
  error,
  onGenerate,
  onLevelChange,
}: {
  historyId: string | null;
  guide: TeachingGuideResponse | null;
  profile: ListenerProfile;
  loading: boolean;
  generating: boolean;
  error: string;
  onGenerate: (force?: boolean) => Promise<unknown>;
  onLevelChange: (level: ListenerLevel) => Promise<void>;
}) {
  if (!historyId) {
    return (
      <section className="panel teaching-gate">
        <span className="section-kicker">INTERACTIVE MUSIC TEACHER</span>
        <h2>保存分析后，开始可交互导赏</h2>
        <p>
          导赏地图和歌曲对话需要一个稳定的歌曲 ID；分析保存后即可生成，
          不会重复分析整首歌曲。
        </p>
      </section>
    );
  }

  const map = guide?.understanding_map ?? null;
  if (loading && !map) {
    return (
      <section className="panel teaching-gate" aria-busy="true">
        <span className="section-kicker">INTERACTIVE MUSIC TEACHER</span>
        <h2>正在读取这首歌的导赏地图…</h2>
        <p>基础分析仍可正常查看和播放。</p>
      </section>
    );
  }

  if (!map) {
    return (
      <section className="panel teaching-gate">
        <span className="section-kicker">INTERACTIVE MUSIC TEACHER</span>
        <h2>把分析变成一堂可复听的音乐导赏课</h2>
        <p>
          系统会用现有歌词、DSP 与带时间的听觉证据生成结构化理解地图；
          失败时不会影响原有分析。
        </p>
        {error && <p className="teaching-error">{error}</p>}
        <button
          type="button"
          disabled={generating}
          onClick={() => void onGenerate()}
        >
          {generating ? "正在整理时间证据…" : "生成教学式导赏"}
        </button>
      </section>
    );
  }

  return (
    <div className="teaching-experience">
      {(guide?.stale || error) && (
        <div className="teaching-status">
          <div>
            <strong>{guide?.stale ? "导赏地图需要更新" : "部分功能暂不可用"}</strong>
            <p>
              {guide?.stale
                ? "歌词或基础分析已经变化，旧地图仍可查看，但不再作为最新证据。"
                : error}
            </p>
          </div>
          {guide?.stale && (
            <button
              type="button"
              disabled={generating}
              onClick={() => void onGenerate(true)}
            >
              {generating ? "正在重建…" : "按最新证据重建"}
            </button>
          )}
        </div>
      )}
      <TeachingOverview map={map} />
      <div className="teaching-main-grid">
        <UnderstandingMap map={map} />
        <ListeningChat
          historyId={historyId}
          map={map}
          profile={profile}
          onLevelChange={onLevelChange}
        />
      </div>
    </div>
  );
}
