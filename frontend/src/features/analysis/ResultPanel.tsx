import { useEffect, useMemo, useRef, useState } from "react";
import { api, API_BASE } from "../../api";
import { SignalMark } from "../../components/SignalMark";
import { confidenceClass, percent, seconds } from "../../format";
import { LatestRequest } from "../../hooks/latestRequest";
import type {
  AnalysisResult,
  Evidence,
  HistoryRevision,
  LyricsRetryResult,
  LyricsSegment,
} from "../../types";
import { SingingComparison } from "../singing/SingingViews";

function TagList({ items, empty = "暂无可靠结果" }: { items: string[]; empty?: string }) {
  if (!items.length) return <p className="empty-copy">{empty}</p>;
  return <div className="tag-list">{items.map((item) => <span key={item}>{item}</span>)}</div>;
}

function Confidence({ value }: { value: number | null }) {
  return <span className={`confidence ${confidenceClass(value)}`}>{percent(value)}</span>;
}

export function ResultPanel({
  result,
  audioUrl,
  fileName,
  historyId,
  revisionCount,
  onSaveLyrics,
}: {
  result: AnalysisResult;
  audioUrl: string;
  fileName: string;
  historyId: string | null;
  revisionCount: number;
  onSaveLyrics: (lyrics: LyricsSegment[]) => Promise<void>;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [editingLyrics, setEditingLyrics] = useState(false);
  const [draftLyrics, setDraftLyrics] = useState<LyricsSegment[]>(result.lyrics);
  const [savingLyrics, setSavingLyrics] = useState(false);
  const [lyricError, setLyricError] = useState("");
  const [revisions, setRevisions] = useState<HistoryRevision[]>([]);
  const [selectedRevision, setSelectedRevision] = useState("current");
  const [loadingRevisions, setLoadingRevisions] = useState(false);
  const [retryingChunk, setRetryingChunk] = useState("");
  const [retryPreview, setRetryPreview] = useState<{
    key: string;
    result: LyricsRetryResult;
  } | null>(null);
  const revisionsRequestRef = useRef(new LatestRequest());

  useEffect(() => {
    revisionsRequestRef.current.invalidate();
    setDraftLyrics(result.lyrics);
    setEditingLyrics(false);
    setSelectedRevision("current");
    setRevisions([]);
    setLoadingRevisions(false);
    setRetryPreview(null);
    setRetryingChunk("");
    setLyricError("");
  }, [historyId, result]);

  const displayedLyrics = selectedRevision === "current"
    ? result.lyrics
    : revisions.find((item) => String(item.id) === selectedRevision)?.lyrics || result.lyrics;
  const qualityEvents = result.evidence.filter((item) =>
    item.id.includes(".quality_retry")
  );
  const duration = useMemo(() => {
    const ends = [
      ...result.lyrics.map((item) => item.span?.end_s || 0),
      ...result.emotion_timeline.map((item) => item.span?.end_s || 0),
      ...result.sound_events.map((item) => item.span?.end_s || 0),
      ...result.technical_metrics.evidence.map((item) => {
        const value = item.metadata.duration_s;
        return typeof value === "number" ? value : 0;
      }),
    ];
    return Math.max(...ends, 1);
  }, [result]);

  const seek = (value: number | undefined) => {
    if (audioRef.current && value != null) {
      audioRef.current.currentTime = value;
      void audioRef.current.play();
    }
  };

  const loadRevisions = async () => {
    if (!historyId || loadingRevisions || revisions.length) return;
    const request = revisionsRequestRef.current.begin();
    setLoadingRevisions(true);
    try {
      const next = await api.historyRevisions(historyId);
      if (revisionsRequestRef.current.isCurrent(request)) {
        setRevisions(next);
      }
    } catch (cause) {
      if (revisionsRequestRef.current.isCurrent(request)) {
        setLyricError(cause instanceof Error ? cause.message : "无法读取修订历史");
      }
    } finally {
      if (revisionsRequestRef.current.isCurrent(request)) {
        setLoadingRevisions(false);
      }
    }
  };

  const saveLyrics = async () => {
    const invalid = draftLyrics.some((line) =>
      !line.text.trim()
      || Boolean(line.span && line.span.end_s < line.span.start_s)
    );
    if (invalid) {
      setLyricError("歌词不能为空，结束时间也不能早于开始时间。");
      return;
    }
    setSavingLyrics(true);
    setLyricError("");
    try {
      await onSaveLyrics(draftLyrics);
      setEditingLyrics(false);
      setSelectedRevision("current");
      setRevisions([]);
    } catch (cause) {
      setLyricError(cause instanceof Error ? cause.message : "歌词保存失败");
    } finally {
      setSavingLyrics(false);
    }
  };

  const updateDraft = (
    index: number,
    field: "text" | "start_s" | "end_s",
    value: string,
  ) => {
    setDraftLyrics((items) => items.map((item, itemIndex) => {
      if (itemIndex !== index) return item;
      if (field === "text") return { ...item, text: value };
      const numeric = Math.max(0, Number(value) || 0);
      const span = item.span || { start_s: 0, end_s: 0 };
      return { ...item, span: { ...span, [field]: numeric } };
    }));
  };

  const retryChunk = async (key: string, start_s: number, end_s: number) => {
    if (!historyId) return;
    setRetryingChunk(key);
    setRetryPreview(null);
    setLyricError("");
    try {
      const retried = await api.retryLyrics(historyId, start_s, end_s);
      setRetryPreview({ key, result: retried });
    } catch (cause) {
      setLyricError(cause instanceof Error ? cause.message : "分块重听失败");
    } finally {
      setRetryingChunk("");
    }
  };

  const applyRetry = async () => {
    if (!retryPreview?.result.lyrics.length) return;
    const retried = retryPreview.result;
    const unaffected = result.lyrics.filter((line) =>
      !line.span
      || line.span.end_s <= retried.start_s
      || line.span.start_s >= retried.end_s
    );
    const merged = [...unaffected, ...retried.lyrics].sort((first, second) =>
      (first.span?.start_s ?? Number.POSITIVE_INFINITY)
      - (second.span?.start_s ?? Number.POSITIVE_INFINITY)
    );
    setSavingLyrics(true);
    setLyricError("");
    try {
      await onSaveLyrics(merged);
      setRetryPreview(null);
    } catch (cause) {
      setLyricError(cause instanceof Error ? cause.message : "重听结果保存失败");
    } finally {
      setSavingLyrics(false);
    }
  };

  return (
    <div className="results">
      <section className="result-hero panel">
        <div className="record-art"><SignalMark /></div>
        <div className="track-info">
          <div className="section-kicker">ANALYSIS COMPLETE</div>
          <h2>{result.title || fileName.replace(/\.[^.]+$/, "")}</h2>
          <p>{result.summary}</p>
          <audio
            crossOrigin={audioUrl.startsWith(API_BASE) ? "use-credentials" : undefined}
            ref={audioRef}
            src={audioUrl}
            controls
            preload="metadata"
          />
        </div>
        <div className="metric-strip">
          <div>
            <span>BPM</span>
            <strong>{result.technical_metrics.bpm?.toFixed(1) || "—"}</strong>
            <small>
              可信度 {percent(result.technical_metrics.bpm_confidence)}
              {result.technical_metrics.bpm_ambiguous && result.technical_metrics.bpm_candidates.length > 1
                ? ` · 倍频候选 ${result.technical_metrics.bpm_candidates.slice(1).join(" / ")}`
                : ""}
            </small>
          </div>
          <div><span>KEY</span><strong>{result.technical_metrics.key || "—"}</strong><small>可信度 {percent(result.technical_metrics.key_confidence)}</small></div>
          <div><span>LYRICS</span><strong>{result.lyrics.length}</strong><small>个片段</small></div>
        </div>
      </section>

      {result.warnings.length > 0 && (
        <section className="warning-box">
          <strong>分析提醒</strong>
          {result.warnings.map((warning) => <p key={warning}>{warning}</p>)}
        </section>
      )}

      <div className="result-grid">
        <section className="panel lyrics-panel">
          <header>
            <div><span className="section-number">01</span><h3>歌词时间轴</h3></div>
            <div className="lyrics-tools">
              {revisionCount > 0 && (
                <select
                  aria-label="歌词修订版本"
                  value={selectedRevision}
                  disabled={editingLyrics}
                  onFocus={() => void loadRevisions()}
                  onChange={(event) => setSelectedRevision(event.target.value)}
                >
                  <option value="current">当前版本</option>
                  {revisions.map((revision, index) => (
                    <option key={revision.id} value={String(revision.id)}>
                      修订前 {revisions.length - index}
                    </option>
                  ))}
                </select>
              )}
              {historyId && selectedRevision === "current" && (
                editingLyrics ? (
                  <>
                    <button
                      type="button"
                      onClick={() => {
                        setDraftLyrics(result.lyrics);
                        setEditingLyrics(false);
                        setLyricError("");
                      }}
                    >
                      取消
                    </button>
                    <button
                      type="button"
                      className="save"
                      disabled={savingLyrics}
                      onClick={() => void saveLyrics()}
                    >
                      {savingLyrics ? "保存中…" : "保存修订"}
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    onClick={() => {
                      setDraftLyrics(result.lyrics);
                      setEditingLyrics(true);
                    }}
                  >
                    校对歌词
                  </button>
                )
              )}
              <small>{displayedLyrics.length} 个片段</small>
            </div>
          </header>
          {qualityEvents.length > 0 && selectedRevision === "current" && (
            <div className="lyrics-quality">
              <strong>自动质量检查处理了 {qualityEvents.length} 个分块</strong>
              {qualityEvents.map((event) => {
                const issues = Array.isArray(event.metadata.issues)
                  ? event.metadata.issues.map(String)
                  : [];
                const original = Array.isArray(event.metadata.original_lyrics)
                  ? event.metadata.original_lyrics as LyricsSegment[]
                  : [];
                const recovered = Array.isArray(event.metadata.recovered_lyrics)
                  ? event.metadata.recovered_lyrics as LyricsSegment[]
                  : [];
                return (
                  <details key={event.id}>
                    <summary>
                      <span>{seconds(event.span?.start_s)}–{seconds(event.span?.end_s)}</span>
                      {event.text}
                    </summary>
                    {issues.map((issue) => <p key={issue}>{issue}</p>)}
                    {(original.length > 0 || recovered.length > 0) && (
                      <div className="quality-compare">
                        <div>
                          <small>初次结果</small>
                          {original.map((line, index) => (
                            <p key={`${line.text}-${index}`}>{line.text}</p>
                          ))}
                        </div>
                        <div>
                          <small>重听结果</small>
                          {recovered.length
                            ? recovered.map((line, index) => (
                              <p key={`${line.text}-${index}`}>{line.text}</p>
                            ))
                            : <p>未获得可靠替代结果</p>}
                        </div>
                      </div>
                    )}
                    {historyId && event.span && (
                      <button
                        type="button"
                        className="retry-chunk"
                        disabled={Boolean(retryingChunk) || savingLyrics}
                        onClick={() => void retryChunk(
                          event.id,
                          event.span!.start_s,
                          event.span!.end_s,
                        )}
                      >
                        {retryingChunk === event.id
                          ? "8004 正在重新聆听…"
                          : "重新聆听此分块"}
                      </button>
                    )}
                  </details>
                );
              })}
            </div>
          )}
          {lyricError && <p className="lyrics-error">{lyricError}</p>}
          {retryPreview && (
            <div className="retry-preview">
              <div>
                <strong>重听预览</strong>
                <small>
                  {seconds(retryPreview.result.start_s)}–
                  {seconds(retryPreview.result.end_s)} · {retryPreview.result.source}
                </small>
              </div>
              {retryPreview.result.lyrics.length ? (
                <ul>
                  {retryPreview.result.lyrics.map((line, index) => (
                    <li key={`${line.text}-${index}`}>
                      <span>{seconds(line.span?.start_s)}</span>
                      {line.text}
                    </li>
                  ))}
                </ul>
              ) : (
                <p>本次重听没有确认到可靠歌词，不会覆盖当前结果。</p>
              )}
              {retryPreview.result.issues.map((issue) => (
                <p className="retry-issue" key={issue}>{issue}</p>
              ))}
              <div className="retry-actions">
                <button type="button" onClick={() => setRetryPreview(null)}>
                  放弃
                </button>
                <button
                  type="button"
                  className="apply"
                  disabled={!retryPreview.result.lyrics.length || savingLyrics}
                  onClick={() => void applyRetry()}
                >
                  {savingLyrics ? "保存中…" : "应用并保存为新版本"}
                </button>
              </div>
            </div>
          )}
          {editingLyrics ? (
            <div className="lyrics-editor">
              {draftLyrics.map((line, index) => (
                <div className="lyrics-editor-row" key={index}>
                  <input
                    aria-label={`第 ${index + 1} 行开始时间`}
                    type="number"
                    min="0"
                    step="0.01"
                    value={line.span?.start_s ?? 0}
                    onChange={(event) => updateDraft(index, "start_s", event.target.value)}
                  />
                  <input
                    aria-label={`第 ${index + 1} 行结束时间`}
                    type="number"
                    min="0"
                    step="0.01"
                    value={line.span?.end_s ?? 0}
                    onChange={(event) => updateDraft(index, "end_s", event.target.value)}
                  />
                  <input
                    aria-label={`第 ${index + 1} 行歌词`}
                    value={line.text}
                    onChange={(event) => updateDraft(index, "text", event.target.value)}
                  />
                  <button
                    type="button"
                    aria-label={`删除第 ${index + 1} 行`}
                    onClick={() => setDraftLyrics((items) =>
                      items.filter((_, itemIndex) => itemIndex !== index)
                    )}
                  >
                    ×
                  </button>
                </div>
              ))}
              <button
                type="button"
                className="add-lyric"
                onClick={() => setDraftLyrics((items) => [
                  ...items,
                  {
                    text: "",
                    span: {
                      start_s: items.at(-1)?.span?.end_s || 0,
                      end_s: items.at(-1)?.span?.end_s || 0,
                    },
                    language: null,
                    confidence: null,
                  },
                ])}
              >
                ＋ 添加歌词行
              </button>
            </div>
          ) : displayedLyrics.length ? (
            <div className="lyrics-list">
              {displayedLyrics.map((line: LyricsSegment, index) => {
                const checked = selectedRevision === "current" && qualityEvents.some(
                  (event) => event.span && line.span
                    && line.span.start_s >= event.span.start_s
                    && line.span.start_s <= event.span.end_s
                );
                const chunkStart = Math.floor((line.span?.start_s || 0) / 30) * 30;
                const chunkEnd = Math.min(chunkStart + 30, duration);
                const retryKey = `line-${index}`;
                return (
                  <div className="lyric-row" key={`${line.text}-${index}`}>
                    <button
                      className="lyric-seek"
                      onClick={() => seek(line.span?.start_s)}
                    >
                      <span>{seconds(line.span?.start_s)}</span>
                      <p>
                        {line.text}
                        {checked && <small className="quality-badge">已重听</small>}
                      </p>
                      <Confidence value={line.confidence} />
                    </button>
                    {historyId && selectedRevision === "current" && line.span && (
                      <button
                        type="button"
                        className="line-retry"
                        title={`${seconds(chunkStart)}–${seconds(chunkEnd)} 重新聆听`}
                        disabled={Boolean(retryingChunk) || savingLyrics}
                        onClick={() => void retryChunk(retryKey, chunkStart, chunkEnd)}
                      >
                        {retryingChunk === retryKey ? "…" : "重听"}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          ) : <p className="empty-copy">没有确认到可靠歌词</p>}
        </section>

        <div className="side-stack">
          <section className="panel compact-panel">
            <header><div><span className="section-number">02</span><h3>乐器与声源</h3></div></header>
            <TagList items={result.instruments} empty="没有确认到具体乐器" />
            {result.sound_events.length > 0 && (
              <div className="event-list">
                {result.sound_events.map((event) => (
                  <button key={event.id} onClick={() => seek(event.span?.start_s)}>
                    <span>{event.text}</span><small>{seconds(event.span?.start_s)}–{seconds(event.span?.end_s)}</small>
                  </button>
                ))}
              </div>
            )}
          </section>

          <section className="panel compact-panel">
            <header><div><span className="section-number">03</span><h3>主题</h3></div></header>
            <TagList items={result.themes} />
          </section>
        </div>
      </div>

      <section className="panel emotion-panel">
        <header>
          <div><span className="section-number">04</span><h3>直接情绪证据</h3></div>
          <small>来自音色、力度与演唱方式</small>
        </header>
        {result.emotion_timeline.length ? (
          <div className="timeline">
            <div className="timeline-ruler"><span>0:00</span><span>{seconds(duration / 2)}</span><span>{seconds(duration)}</span></div>
            <div className="timeline-track">
              {result.emotion_timeline.map((item: Evidence) => (
                <button
                  key={item.id}
                  style={{
                    left: `${((item.span?.start_s || 0) / duration) * 100}%`,
                    width: `${Math.max((((item.span?.end_s || duration) - (item.span?.start_s || 0)) / duration) * 100, 7)}%`,
                  }}
                  onClick={() => seek(item.span?.start_s)}
                >
                  <span>{item.text}</span><Confidence value={item.confidence} />
                </button>
              ))}
            </div>
          </div>
        ) : <p className="empty-copy">模型没有确认直接情绪；这不等于音乐没有氛围。</p>}
      </section>

      <section className="panel atmosphere-panel">
        <header>
          <div><span className="section-number">05</span><h3>推断氛围</h3></div>
          <small>非直接听觉证据</small>
        </header>
        {result.inferred_atmosphere.length ? (
          <div className="atmosphere-grid">
            {result.inferred_atmosphere.map((item) => (
              <article key={item.id}>
                <div><strong>{item.text}</strong><Confidence value={item.confidence} /></div>
                <p>{String(item.metadata.basis || "由歌词、节奏和声音描述综合推断")}</p>
              </article>
            ))}
          </div>
        ) : <p className="empty-copy">证据不足，未生成推断氛围</p>}
      </section>
      <SingingComparison key={historyId || "unsaved"} historyId={historyId} />
    </div>
  );
}
