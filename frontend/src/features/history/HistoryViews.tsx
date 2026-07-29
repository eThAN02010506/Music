import type {
  HistoryDetail,
  HistorySummary,
  User,
} from "../../types";
import { seconds } from "../../format";
import { SignalMark } from "../../components/SignalMark";

const historyStateLabels: Record<string, string> = {
  queued: "排队中",
  running: "分析中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export function historyTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function UserMenu({
  user,
  onLeaderboard,
  onLogout,
}: {
  user: User;
  onLeaderboard: () => void;
  onLogout: () => void;
}) {
  const initial = Array.from(user.username)[0]?.toUpperCase() || "U";
  return (
    <details className="user-menu">
      <summary aria-label={`${user.username} 的账号菜单`}>
        <span className="user-avatar">{initial}</span>
        <span className="user-name"><strong>{user.username}</strong><small>本地账号</small></span>
        <span className="settings-chevron" aria-hidden="true">⌄</span>
      </summary>
      <div className="user-popover">
        <div>
          <span className="user-avatar">{initial}</span>
          <p><strong>{user.username}</strong><small>独立本机工作区</small></p>
        </div>
        <button type="button" onClick={onLeaderboard}><span>♜</span> 演唱最高分榜</button>
        <button type="button" className="logout" onClick={onLogout}><span>↪</span> 退出登录</button>
      </div>
    </details>
  );
}

export function HistorySidebar({
  items,
  username,
  activeId,
  compareIds,
  deletingIds,
  onNew,
  onSelect,
  onDelete,
  onRename,
  onToggleCompare,
  onCompare,
}: {
  items: HistorySummary[];
  username: string;
  activeId: string | null;
  compareIds: string[];
  deletingIds: ReadonlySet<string>;
  onNew: () => void;
  onSelect: (id: string) => void;
  onDelete: (item: HistorySummary) => void;
  onRename: (item: HistorySummary) => void;
  onToggleCompare: (id: string) => void;
  onCompare: () => void;
}) {
  return (
    <aside className="history-sidebar">
      <div className="history-brand"><SignalMark /><span>Music Insight</span></div>
      <button className="new-analysis" onClick={onNew}><span>＋</span> 新分析</button>
      <div className="history-heading">
        <span>分析历史</span><small>{items.length}</small>
      </div>
      <div className="history-list">
        {items.length ? items.map((item) => (
          <article key={item.id} className={`history-item ${activeId === item.id ? "active" : ""}`}>
            <button className="history-open" onClick={() => onSelect(item.id)}>
              <strong>{item.title}</strong>
              <span>{historyTime(item.created_at)} · {historyStateLabels[item.state] || item.state}</span>
              {item.state === "completed" && (
                <small>{item.lyrics_count} 段歌词{item.bpm ? ` · ${item.bpm.toFixed(1)} BPM` : ""}</small>
              )}
              <small>{item.model_source === "local" ? "本地权重" : item.model_location || "默认 8004"}</small>
            </button>
            <div className="history-actions">
              <label title="加入对比">
                <input
                  type="checkbox"
                  checked={compareIds.includes(item.id)}
                  disabled={item.state !== "completed" || (!compareIds.includes(item.id) && compareIds.length >= 2)}
                  onChange={() => onToggleCompare(item.id)}
                />
                对比
              </label>
              <button onClick={() => onRename(item)} aria-label={`重命名 ${item.title}`}>✎</button>
              <button
                type="button"
                disabled={deletingIds.has(item.id)}
                onClick={() => onDelete(item)}
                aria-label={deletingIds.has(item.id)
                  ? `正在删除 ${item.title}`
                  : `删除 ${item.title}`}
              >
                {deletingIds.has(item.id) ? "…" : "×"}
              </button>
            </div>
          </article>
        )) : <p className="history-empty">分析完成后会保存在这里</p>}
      </div>
      <button className="compare-button" disabled={compareIds.length !== 2} onClick={onCompare}>
        对比分析 {compareIds.length}/2
      </button>
      <p className="local-note">仅显示 {username} 的本机记录</p>
    </aside>
  );
}

export function ComparisonPanel({ entries }: { entries: HistoryDetail[] }) {
  const rows: Array<[string, (entry: HistoryDetail) => string]> = [
    ["时长", (entry) => seconds(entry.duration_s)],
    ["BPM", (entry) => entry.result?.technical_metrics.bpm?.toFixed(1) || "—"],
    ["调性", (entry) => entry.result?.technical_metrics.key || "—"],
    ["歌词", (entry) => `${entry.lyrics_count} 个片段`],
    ["乐器", (entry) => entry.instruments.join("、") || "未确认"],
    ["主题", (entry) => entry.result?.themes.join("、") || "未确认"],
    ["直接情绪", (entry) => Array.from(new Set(entry.result?.emotion_timeline.map((item) => item.text) || [])).join("、") || "未确认"],
    ["推断氛围", (entry) => entry.result?.inferred_atmosphere.map((item) => item.text).join("、") || "未确认"],
  ];
  return (
    <section className="comparison panel">
      <div className="section-kicker">COMPARE ANALYSES</div>
      <h2>并排比较</h2>
      <div className="comparison-grid comparison-head">
        <span>指标</span>
        {entries.map((entry) => <strong key={entry.id}>{entry.title}</strong>)}
      </div>
      {rows.map(([label, read]) => (
        <div className="comparison-grid" key={label}>
          <span>{label}</span>
          {entries.map((entry) => <p key={entry.id}>{read(entry)}</p>)}
        </div>
      ))}
      <div className="comparison-summaries">
        {entries.map((entry) => (
          <article key={entry.id}><strong>{entry.title}</strong><p>{entry.summary || "暂无摘要"}</p></article>
        ))}
      </div>
    </section>
  );
}
