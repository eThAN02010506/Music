import { useCallback, useEffect, useState } from "react";
import { api, isAbortError } from "../../api";
import { ModalDialog } from "../../components/ModalDialog";
import { percent, seconds } from "../../format";
import { useAudioRecorder } from "../../hooks/useAudioRecorder";
import { useObjectUrl } from "../../hooks/useObjectUrl";
import type {
  LeaderboardEntry,
  SingingScore,
  User,
} from "../../types";
import { historyTime } from "../history/HistoryViews";

export function SingingScoreResult({ score }: { score: SingingScore }) {
  return (
    <div className="score-result">
      <div className="total-score">
        <strong>{score.total}</strong><span>/ 100</span><small>综合得分</small>
      </div>
      <div className="score-breakdown">
        {[
          ["音准", score.pitch],
          ["节奏", score.rhythm],
          ["完整度", score.completeness],
          ["稳定性", score.stability],
        ].map(([label, value]) => (
          <div key={label}>
            <span>{label}</span><strong>{value}</strong>
            <i><b style={{ width: `${value}%` }} /></i>
          </div>
        ))}
      </div>
      <div className="pitch-error-strip" aria-label="音高误差时间轴">
        {score.pitch_curve.map((point, index) => {
          const errorValue = point.error_semitones;
          const level = errorValue == null ? "missing"
            : errorValue <= 0.5 ? "good"
            : errorValue <= 1.5 ? "medium" : "bad";
          return <span key={index} className={level} title={
            errorValue == null
              ? "无可比音高"
              : `${seconds(point.reference_time_s)} · 偏差 ${errorValue} 半音`
          } />;
        })}
      </div>
      <p className="score-summary">
        音高误差中位数：
        {score.median_pitch_error == null ? "证据不足" : `${score.median_pitch_error} 半音`}
        {score.in_tune_ratio != null && ` · 半音内命中 ${percent(score.in_tune_ratio)}`}
      </p>
      <p className="score-summary">
        参考 {seconds(score.reference_duration_s)} · 演唱 {seconds(score.performance_duration_s)}
      </p>
      {score.practice_moments.length > 0 && (
        <div className="singing-practice-moments">
          <strong>优先练习的时间段</strong>
          {score.practice_moments.map((moment) => (
            <article key={`${moment.start_s}-${moment.end_s}`}>
              <time>
                {seconds(moment.start_s)}–{seconds(moment.end_s)}
              </time>
              <span>{moment.observation}</span>
              <small>{moment.listening_task}</small>
            </article>
          ))}
        </div>
      )}
      {score.notes.map((note) => <p className="score-note" key={note}>{note}</p>)}
    </div>
  );
}

export function SingingComparison({ historyId }: { historyId: string | null }) {
  const [attempt, setAttempt] = useState<Blob | null>(null);
  const [attemptName, setAttemptName] = useState("my-singing.webm");
  const attemptObjectUrl = useObjectUrl();
  const [score, setScore] = useState<SingingScore | null>(null);
  const [scoring, setScoring] = useState(false);
  const [error, setError] = useState("");

  const useAttempt = useCallback((blob: Blob, name: string) => {
    setAttempt(blob);
    setAttemptName(name);
    attemptObjectUrl.setBlob(blob);
    setScore(null);
    setError("");
  }, [attemptObjectUrl.setBlob]);

  const audioRecorder = useAudioRecorder({
    onRecorded: useAttempt,
    onError: (cause) => setError(cause.message),
  });

  const startRecording = async () => {
    setError("");
    try {
      await audioRecorder.start();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法使用麦克风");
    }
  };

  const stopRecording = () => {
    try {
      audioRecorder.stop();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法停止录音");
    }
  };

  const runScore = async () => {
    if (!historyId || !attempt) return;
    setScoring(true);
    setError("");
    try {
      setScore(await api.scoreSinging(historyId, attempt, attemptName));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "演唱评分失败");
    } finally {
      setScoring(false);
    }
  };

  return (
    <section className="panel singing-panel">
      <header>
        <div><span className="section-number">06</span><h3>演唱对比</h3></div>
        <small>本地声学评分 · 不由大模型决定总分</small>
      </header>
      <div className="singing-actions">
        <button
          type="button"
          className={audioRecorder.recording ? "recording" : ""}
          disabled={
            !historyId
            || scoring
            || (audioRecorder.busy && !audioRecorder.recording)
          }
          onClick={() => audioRecorder.recording ? stopRecording() : void startRecording()}
        >
          {audioRecorder.starting
            ? "等待麦克风授权…"
            : audioRecorder.finalizing
              ? "正在生成录音…"
            : audioRecorder.recording ? "■ 停止录音" : "● 开始演唱"}
        </button>
        <label aria-disabled={scoring || audioRecorder.busy}>
          上传录音
          <input
            type="file"
            accept="audio/*,.wav,.mp3,.m4a,.webm,.ogg"
            className="visually-hidden"
            disabled={scoring || audioRecorder.busy}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) useAttempt(file, file.name);
            }}
          />
        </label>
        <button
          type="button"
          className="score-button"
          disabled={
            !attempt
            || !historyId
            || scoring
            || audioRecorder.busy
          }
          onClick={() => void runScore()}
        >
          {scoring ? "正在对齐音高与节奏…" : "开始评分"}
        </button>
      </div>
      {attemptObjectUrl.url && <audio src={attemptObjectUrl.url} controls preload="metadata" />}
      {error && <p className="singing-error">{error}</p>}
      {score && <SingingScoreResult score={score} />}
    </section>
  );
}

export function StandaloneSingingComparison() {
  const [reference, setReference] = useState<File | null>(null);
  const referenceObjectUrl = useObjectUrl();
  const [performance, setPerformance] = useState<Blob | null>(null);
  const [performanceName, setPerformanceName] = useState("my-singing.webm");
  const performanceObjectUrl = useObjectUrl();
  const [scoring, setScoring] = useState(false);
  const [score, setScore] = useState<SingingScore | null>(null);
  const [error, setError] = useState("");

  const chooseReference = (file: File) => {
    setReference(file);
    referenceObjectUrl.setBlob(file);
    setScore(null);
    setError("");
  };

  const choosePerformance = useCallback((blob: Blob, name: string) => {
    setPerformance(blob);
    setPerformanceName(name);
    performanceObjectUrl.setBlob(blob);
    setScore(null);
    setError("");
  }, [performanceObjectUrl.setBlob]);

  const audioRecorder = useAudioRecorder({
    onRecorded: choosePerformance,
    onError: (cause) => setError(cause.message),
  });

  const startRecording = async () => {
    setError("");
    try {
      await audioRecorder.start();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法使用麦克风");
    }
  };

  const stopRecording = () => {
    try {
      audioRecorder.stop();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法停止录音");
    }
  };

  const compare = async () => {
    if (!reference || !performance) return;
    setScoring(true);
    setError("");
    setScore(null);
    try {
      setScore(await api.compareSinging(
        reference,
        reference.name,
        performance,
        performanceName,
      ));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "演唱对比失败");
    } finally {
      setScoring(false);
    }
  };

  return (
    <section className="panel standalone-singing">
      <div className="section-kicker">SINGING COMPARE</div>
      <h1>上传示范，再唱一遍</h1>
      <p className="lead">
        分别提供参考音频和你的演唱。系统会自动去除首尾静音、统一时间尺度，
        并比较音准、节奏、完整度与稳定性。
      </p>
      <div className="dual-audio-grid">
        <article className={reference ? "ready" : ""}>
          <span className="audio-step">01</span>
          <h3>参考音频</h3>
          <p>原唱、标准示范或纯人声均可。纯人声参考通常最准确。</p>
          <label
            className="audio-file-button"
            aria-disabled={scoring || audioRecorder.busy}
          >
            {reference ? "更换参考音频" : "上传参考音频"}
            <input
              type="file"
              accept="audio/*,.wav,.mp3,.flac,.m4a,.webm,.ogg"
              className="visually-hidden"
              disabled={scoring || audioRecorder.busy}
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) chooseReference(file);
              }}
            />
          </label>
          {reference && <strong className="audio-file-name">{reference.name}</strong>}
          {referenceObjectUrl.url && <audio src={referenceObjectUrl.url} controls preload="metadata" />}
        </article>
        <article className={performance ? "ready" : ""}>
          <span className="audio-step">02</span>
          <h3>你的演唱</h3>
          <p>可以上传已有录音，也可以直接使用浏览器麦克风录制。</p>
          <div className="performance-buttons">
            <label
              className="audio-file-button"
              aria-disabled={scoring || audioRecorder.busy}
            >
              {performance ? "更换录音" : "上传录音"}
              <input
                type="file"
                accept="audio/*,.wav,.mp3,.flac,.m4a,.webm,.ogg"
                className="visually-hidden"
                disabled={scoring || audioRecorder.busy}
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) choosePerformance(file, file.name);
                }}
              />
            </label>
            <button
              type="button"
              className={audioRecorder.recording ? "recording" : ""}
              disabled={scoring || (audioRecorder.busy && !audioRecorder.recording)}
              onClick={() => audioRecorder.recording ? stopRecording() : void startRecording()}
            >
              {audioRecorder.starting
                ? "等待麦克风授权…"
                : audioRecorder.finalizing
                  ? "正在生成录音…"
                : audioRecorder.recording ? "■ 停止录音" : "● 麦克风录音"}
            </button>
          </div>
          {performance && <strong className="audio-file-name">{performanceName}</strong>}
          {performanceObjectUrl.url && <audio src={performanceObjectUrl.url} controls preload="metadata" />}
        </article>
      </div>
      <div className="standalone-score-action">
        <div>
          <strong>两份音频只用于本次计算</strong>
          <small>评分完成后，后端会立即删除临时文件。</small>
        </div>
        <button
          type="button"
          disabled={
            !reference
            || !performance
            || audioRecorder.busy
            || scoring
          }
          onClick={() => void compare()}
        >
          {scoring ? "正在提取旋律并对齐…" : "开始打分对比"}
        </button>
      </div>
      {error && <p className="singing-error">{error}</p>}
      {score && <SingingScoreResult score={score} />}
    </section>
  );
}

function leaderboardSource(source: string) {
  if (source === "history") return "分析记录演唱";
  if (source === "standalone") return "独立演唱对比";
  return source || "演唱评分";
}

export function LeaderboardPanel({
  user,
  onUserUpdated,
  onClose,
}: {
  user: User;
  onUserUpdated: (user: User) => void;
  onClose: () => void;
}) {
  const [entries, setEntries] = useState<LeaderboardEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [updatingVisibility, setUpdatingVisibility] = useState(false);
  const [error, setError] = useState("");

  const loadBoard = useCallback((signal?: AbortSignal) => {
    setLoading(true);
    setError("");
    return api.leaderboard(signal)
      .then((next) => {
        if (!signal?.aborted) setEntries(next.entries);
      })
      .catch((cause) => {
        if (!signal?.aborted && !isAbortError(cause)) {
          setError(cause instanceof Error ? cause.message : "排行榜加载失败");
        }
      })
      .finally(() => {
        if (!signal?.aborted) setLoading(false);
      });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void loadBoard(controller.signal);
    return () => controller.abort();
  }, [loadBoard]);

  const changeVisibility = async (visible: boolean) => {
    if (updatingVisibility) return;
    setUpdatingVisibility(true);
    setError("");
    try {
      const updated = await api.updateLeaderboardVisibility(visible);
      onUserUpdated(updated);
      await loadBoard();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "排行榜隐私设置失败");
    } finally {
      setUpdatingVisibility(false);
    }
  };

  return (
    <ModalDialog
      titleId="leaderboard-title"
      panelClassName="leaderboard-panel"
      onClose={onClose}
    >
      <header>
        <div>
          <span className="section-kicker">SINGING LEADERBOARD</span>
          <h2 id="leaderboard-title">演唱最高分榜</h2>
        </div>
        <button type="button" className="dialog-close" onClick={onClose} aria-label="关闭排行榜">×</button>
      </header>
      <p className="leaderboard-rule">
        娱乐最高分榜，每人仅取最高分；不同参考音频仅供娱乐。
      </p>
      <label className="leaderboard-privacy">
        <input
          type="checkbox"
          checked={user.leaderboard_visible}
          disabled={updatingVisibility}
          onChange={(event) => void changeVisibility(event.target.checked)}
        />
        <span>
          <strong>把我的最高分加入排行榜</strong>
          <small>默认不公开；关闭后会立即从其他用户可见的榜单移除。</small>
        </span>
      </label>
      {loading ? (
        <div className="leaderboard-state" role="status">正在汇总最高成绩…</div>
      ) : error ? (
        <div className="leaderboard-state error" role="alert">{error}</div>
      ) : entries.length ? (
        <div className="leaderboard-list">
          <div className="leaderboard-labels" aria-hidden="true">
            <span>名次 / 用户</span><span>四项得分</span><span>最高分</span>
          </div>
          {entries.map((entry) => {
            const own = entry.is_current_user;
            return (
              <article
                key={`${entry.rank}-${entry.username}`}
                className={`${own ? "own" : ""} ${entry.rank <= 3 ? `podium rank-${entry.rank}` : ""}`}
              >
                <div className="leaderboard-person">
                  <strong className="leaderboard-rank">{entry.rank}</strong>
                  <span>
                    <b>{entry.username}{own && <em>你</em>}</b>
                    <small>{leaderboardSource(entry.source)} · {entry.attempts} 次评分</small>
                  </span>
                </div>
                <div className="leaderboard-breakdown">
                  <span>音准 <b>{entry.pitch}</b></span>
                  <span>节奏 <b>{entry.rhythm}</b></span>
                  <span>完整 <b>{entry.completeness}</b></span>
                  <span>稳定 <b>{entry.stability}</b></span>
                </div>
                <div className="leaderboard-total">
                  <strong>{entry.total}</strong><span>/ 100</span>
                  <small>{historyTime(entry.created_at)}</small>
                </div>
              </article>
            );
          })}
        </div>
      ) : (
        <div className="leaderboard-state">
          <strong>排行榜还没有成绩</strong>
          <span>完成一次演唱打分后，最高分会出现在这里。</span>
        </div>
      )}
    </ModalDialog>
  );
}
