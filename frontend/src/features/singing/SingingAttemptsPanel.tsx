import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { api, isAbortError } from "../../api";
import { ModalDialog } from "../../components/ModalDialog";
import { AbortableLatestRequest } from "../../hooks/abortableLatestRequest";
import { useI18n } from "../../i18n";
import type { SingingAttempt } from "../../types";
import { historyTime } from "../history/HistoryViews";
import {
  isIdempotentSingingAttemptDelete,
  mergeSingingAttemptPage,
  SINGING_ATTEMPT_PAGE_SIZE,
  singingAttemptCursor,
  singingAttemptSource,
} from "./singingAttemptHistory";

function attemptFiles(
  attempt: SingingAttempt,
  t: (source: string) => string,
): string {
  const names = [
    attempt.reference_name && `${t("参考：")}${attempt.reference_name}`,
    attempt.performance_name && `${t("演唱：")}${attempt.performance_name}`,
  ].filter(Boolean);
  return names.join(" · ") || t("未保留音频文件名");
}

function AttemptScoreBreakdown({ attempt }: { attempt: SingingAttempt }) {
  const { t } = useI18n();
  const { score } = attempt;
  return (
    <div className="attempt-score-breakdown" aria-label={t("四项得分")}>
      <span>{t("音准")} <b>{score.pitch}</b></span>
      <span>{t("节奏")} <b>{score.rhythm}</b></span>
      <span>{t("完整")} <b>{score.completeness}</b></span>
      <span>{t("稳定")} <b>{score.stability}</b></span>
    </div>
  );
}

export function SingingAttemptsPanel({
  onClose,
  onOpenAnalysis,
}: {
  onClose: () => void;
  onOpenAnalysis: (historyId: string) => void;
}) {
  const { locale, t } = useI18n();
  const [attempts, setAttempts] = useState<SingingAttempt[]>([]);
  const attemptsRef = useRef<SingingAttempt[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [deletingIds, setDeletingIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const deletingIdsRef = useRef(new Set<string>());
  const [error, setError] = useState("");
  const requestsRef = useRef(new AbortableLatestRequest());
  const mountedRef = useRef(true);

  const publishAttempts = useCallback((items: SingingAttempt[]) => {
    attemptsRef.current = items;
    setAttempts(items);
  }, []);

  const load = useCallback(async (reset: boolean) => {
    const request = requestsRef.current.begin();
    const previous = attemptsRef.current;
    const cursor = reset ? null : singingAttemptCursor(previous);
    if (!reset && !cursor) {
      requestsRef.current.settle(request.id);
      setHasMore(false);
      return;
    }
    setError("");
    if (reset && previous.length) {
      setRefreshing(true);
    } else if (reset) {
      setLoading(true);
    } else {
      setLoadingMore(true);
    }
    try {
      const response = await api.singingAttempts(
        SINGING_ATTEMPT_PAGE_SIZE + 1,
        cursor,
        request.signal,
      );
      if (!requestsRef.current.isCurrent(request.id)) return;
      const page = mergeSingingAttemptPage(
        reset ? [] : attemptsRef.current,
        response,
        { reset, pageSize: SINGING_ATTEMPT_PAGE_SIZE },
      );
      publishAttempts(page.items);
      setHasMore(page.hasMore);
    } catch (cause) {
      if (
        requestsRef.current.isCurrent(request.id)
        && !isAbortError(cause)
      ) {
        setError(cause instanceof Error ? cause.message : "演唱记录加载失败");
      }
    } finally {
      if (requestsRef.current.isCurrent(request.id)) {
        setLoading(false);
        setRefreshing(false);
        setLoadingMore(false);
      }
      requestsRef.current.settle(request.id);
    }
  }, [publishAttempts]);

  useEffect(() => {
    const requests = requestsRef.current;
    mountedRef.current = true;
    void load(true);
    return () => {
      mountedRef.current = false;
      requests.invalidate();
    };
  }, [load]);

  const deleteAttempt = async (attempt: SingingAttempt) => {
    if (deletingIdsRef.current.has(attempt.id)) return;
    const confirmed = window.confirm(
      `${t("确定删除")} ${historyTime(attempt.created_at, locale)} ${t("的演唱成绩吗？")}\n\n${t("删除后排行榜也会立即按剩余成绩重新计算。")}`,
    );
    if (!confirmed) return;
    deletingIdsRef.current.add(attempt.id);
    setDeletingIds(new Set(deletingIdsRef.current));
    setError("");
    try {
      try {
        await api.deleteSingingAttempt(attempt.id);
      } catch (cause) {
        if (!isIdempotentSingingAttemptDelete(cause)) throw cause;
      }
      if (!mountedRef.current) return;
      publishAttempts(
        attemptsRef.current.filter((item) => item.id !== attempt.id),
      );
      await load(true);
    } catch (cause) {
      if (mountedRef.current) {
        setError(cause instanceof Error ? cause.message : "演唱记录删除失败");
      }
    } finally {
      deletingIdsRef.current.delete(attempt.id);
      if (mountedRef.current) {
        setDeletingIds(new Set(deletingIdsRef.current));
      }
    }
  };

  return (
    <ModalDialog
      titleId="singing-attempts-title"
      panelClassName="singing-attempts-panel"
      onClose={onClose}
    >
      <header>
        <div>
          <span className="section-kicker">PERSONAL SINGING HISTORY</span>
          <h2 id="singing-attempts-title">{t("我的演唱记录")}</h2>
          <small>{t("仅显示当前账号保存的成绩")}</small>
        </div>
        <div className="dialog-header-actions">
          <button
            type="button"
            className="attempt-refresh"
            disabled={loading || refreshing || loadingMore}
            onClick={() => void load(true)}
          >
            {refreshing ? t("刷新中…") : `↻ ${t("刷新")}`}
          </button>
          <button
            type="button"
            className="dialog-close"
            onClick={onClose}
            aria-label={t("关闭演唱记录")}
          >
            ×
          </button>
        </div>
      </header>

      {loading ? (
        <div className="attempt-history-state" role="status">
          {t("正在读取个人演唱记录…")}
        </div>
      ) : !attempts.length && error ? (
        <div className="attempt-history-state error" role="alert">
          <strong>{t("暂时无法读取演唱记录")}</strong>
          <span>{t(error)}</span>
          <button type="button" onClick={() => void load(true)}>{t("重新加载")}</button>
        </div>
      ) : !attempts.length ? (
        <div className="attempt-history-state">
          <strong>{t("还没有演唱成绩")}</strong>
          <span>{t("完成一次分析歌曲演唱或独立演唱对比后，记录会出现在这里。")}</span>
        </div>
      ) : (
        <>
          {error && (
            <p className="attempt-history-error" role="alert">
              {t(error)}
              <button
                type="button"
                onClick={() => void load(true)}
              >
                {t("重新同步")}
              </button>
            </p>
          )}
          <div
            className="attempt-history-list"
            aria-busy={refreshing || loadingMore}
          >
            {attempts.map((attempt) => (
              <article key={attempt.id}>
                <div className="attempt-history-main">
                  <div>
                    <strong>{t(singingAttemptSource(attempt.source))}</strong>
                    <small>{historyTime(attempt.created_at, locale)}</small>
                  </div>
                  <p title={attemptFiles(attempt, t)}>{attemptFiles(attempt, t)}</p>
                  {attempt.history_id && (
                    <button
                      type="button"
                      className="attempt-open-analysis"
                      onClick={() => onOpenAnalysis(attempt.history_id!)}
                    >
                      {t("打开参考分析")}
                    </button>
                  )}
                </div>
                <AttemptScoreBreakdown attempt={attempt} />
                <div className="attempt-history-total">
                  <strong>{attempt.score.total}</strong>
                  <span>/ 100</span>
                  <button
                    type="button"
                    className="attempt-delete"
                    disabled={deletingIds.has(attempt.id)}
                    onClick={() => void deleteAttempt(attempt)}
                    aria-label={`${t("删除")} ${historyTime(attempt.created_at, locale)} ${t("演唱评分")}`}
                  >
                    {deletingIds.has(attempt.id) ? t("删除中…") : t("删除")}
                  </button>
                </div>
              </article>
            ))}
          </div>
          <footer className="attempt-history-footer">
            <span>{t("已加载")} {attempts.length} {t("条记录")}</span>
            {hasMore ? (
              <button
                type="button"
                disabled={loadingMore || refreshing}
                onClick={() => void load(false)}
              >
                {loadingMore ? t("加载中…") : t("加载更多")}
              </button>
            ) : <small>{t("已经到底了")}</small>}
          </footer>
        </>
      )}
    </ModalDialog>
  );
}
