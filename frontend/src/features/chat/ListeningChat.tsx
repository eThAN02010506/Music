import { type FormEvent, useMemo, useState } from "react";

import { seconds } from "../../format";
import { useI18n } from "../../i18n";
import type {
  ListenerLevel,
  ListenerProfile,
  MusicUnderstandingMap,
  TeachingChatRequest,
} from "../../types";
import {
  adjacentComparisonRanges,
  rangeAround,
} from "../player/playerController";
import {
  usePlayer,
  usePlayerSnapshot,
} from "../player/PlayerContext";
import {
  buildTeachingChatRequest,
  createClientRequestId,
  type ChatRangeMode,
} from "./chatPayload";
import { ChatAnswer } from "./ChatAnswer";
import { useSongConversation } from "./useSongConversation";

const LEVEL_LABELS: Record<ListenerLevel, string> = {
  beginner: "刚开始认真听音乐",
  curious: "会主动比较声音变化",
  intermediate: "了解一些常见乐理",
  advanced: "有较系统的音乐基础",
};

function currentSection(
  map: MusicUnderstandingMap,
  time: number,
): string {
  return map.sections.find(
    (section) => time >= section.span.start_s && time < section.span.end_s,
  )?.label || "未确认段落";
}

export function ListeningChat({
  historyId,
  map,
  profile,
  onLevelChange,
}: {
  historyId: string;
  map: MusicUnderstandingMap;
  profile: ListenerProfile;
  onLevelChange: (level: ListenerLevel) => Promise<void>;
}) {
  const { t } = useI18n();
  const player = usePlayer();
  const snapshot = usePlayerSnapshot();
  const conversation = useSongConversation(historyId);
  const [question, setQuestion] = useState("");
  const [rangeMode, setRangeMode] = useState<ChatRangeMode>("current");
  const [updatingLevel, setUpdatingLevel] = useState(false);
  const [levelError, setLevelError] = useState("");
  const [contextHint, setContextHint] = useState(
    "未指定片段时，会以当前位置自动建立 15 秒范围。",
  );
  const section = useMemo(
    () => currentSection(map, snapshot.currentTime),
    [map, snapshot.currentTime],
  );

  const submit = async (
    text: string,
    mode: ChatRangeMode = rangeMode,
    requestSnapshot = snapshot,
  ) => {
    const cleaned = text.trim();
    if (!cleaned || conversation.sending) return;
    if (mode === "selection" && !requestSnapshot.selectedRange) return;
    if (mode === "compare" && (!requestSnapshot.rangeA || !requestSnapshot.rangeB)) {
      return;
    }
    const payload: TeachingChatRequest = buildTeachingChatRequest({
      message: cleaned,
      snapshot: requestSnapshot,
      mode,
      requestId: createClientRequestId(),
    });
    const result = await conversation.send(payload);
    if (result) setQuestion("");
  };

  const explainCurrent = () => {
    const selection = rangeAround(
      snapshot.currentTime,
      snapshot.duration,
      15,
    );
    player.setRange("selection", selection);
    void submit(
      t("请解释当前这 15 秒：我应该先听什么，以及这些声音产生了什么表达作用？"),
      "selection",
      { ...snapshot, selectedRange: selection },
    );
  };

  const selectQuestionRange = () => {
    if (!snapshot.selectedRange) {
      player.setRange(
        "selection",
        rangeAround(snapshot.currentTime, snapshot.duration, 15),
      );
      setContextHint("已自动选取当前位置附近 15 秒，可在播放器中继续调整。");
    } else {
      setContextHint("将携带播放器中已经框选的时间范围。");
    }
    setRangeMode("selection");
  };

  const selectComparison = () => {
    if (!snapshot.rangeA || !snapshot.rangeB) {
      const [rangeA, rangeB] = adjacentComparisonRanges(
        snapshot.currentTime,
        snapshot.duration,
        15,
      );
      player.setRange("a", rangeA);
      player.setRange("b", rangeB);
      setContextHint("已自动建立相邻的 A/B 两段，可在播放器中重新框选并覆盖。");
    } else {
      setContextHint("将比较播放器中已经设置的 A/B 两段。");
    }
    setRangeMode("compare");
    if (!question.trim()) {
      setQuestion(t("请比较 A/B 两段的声音变化和表达作用。"));
    }
  };

  const submitForm = (event: FormEvent) => {
    event.preventDefault();
    void submit(question);
  };

  const updateLevel = async (level: ListenerLevel) => {
    if (updatingLevel || level === profile.level) return;
    setUpdatingLevel(true);
    setLevelError("");
    try {
      await onLevelChange(level);
    } catch (cause) {
      setLevelError(
        cause instanceof Error ? cause.message : "无法更新音乐基础设置",
      );
    } finally {
      setUpdatingLevel(false);
    }
  };

  const retryFailedSend = async () => {
    const failed = conversation.failedSend;
    if (!failed || conversation.sending) return;
    const result = await conversation.retryFailedSend();
    if (result) {
      setQuestion((current) =>
        current.trim() === failed.payload.message ? "" : current
      );
    }
  };

  return (
    <aside className="panel listening-chat">
      <header>
        <div>
          <span className="section-number">{t("问")}</span>
          <div>
            <h2>{t("边听边问")}</h2>
            <p>
              {seconds(snapshot.currentTime)} · {t(section)}
            </p>
          </div>
        </div>
        <select
          aria-label={t("我的音乐基础")}
          aria-describedby={levelError ? "listener-level-error" : undefined}
          value={profile.level}
          disabled={updatingLevel}
          onChange={(event) =>
            void updateLevel(event.target.value as ListenerLevel)}
        >
          {Object.entries(LEVEL_LABELS).map(([value, label]) => (
            <option key={value} value={value}>{t(label)}</option>
          ))}
        </select>
      </header>
      {levelError && (
        <p
          className="listener-level-error"
          id="listener-level-error"
          role="alert"
        >
          {t(levelError)}，{t("已保留原来的音乐基础设置。")}
        </p>
      )}

      <div className="conversation-toolbar">
        <select
          aria-label={t("歌曲对话")}
          value={conversation.activeConversationId || ""}
          disabled={conversation.loading}
          onChange={(event) =>
            conversation.selectConversation(event.target.value || null)}
        >
          <option value="">{t("新对话")}</option>
          {conversation.conversations.map((item, index) => (
            <option key={item.id} value={item.id}>
              {item.title || `${t("导赏对话")} ${conversation.conversations.length - index}`}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => void conversation.createConversation()}
        >
          ＋ {t("新对话")}
        </button>
        {conversation.activeConversationId && (
          <button
            type="button"
            className="delete-conversation"
            onClick={() => {
              const id = conversation.activeConversationId;
              if (id) void conversation.deleteConversation(id);
            }}
          >
            {t("删除")}
          </button>
        )}
      </div>

      <div className="chat-context-actions">
        <button type="button" onClick={explainCurrent}>
          {t("解释当前 15 秒")}
        </button>
        <button
          type="button"
          onClick={selectQuestionRange}
          className={rangeMode === "selection" ? "active" : ""}
        >
          {t("询问框选片段")}
        </button>
        <button
          type="button"
          onClick={selectComparison}
          className={rangeMode === "compare" ? "active" : ""}
        >
          {t("比较 A/B")}
        </button>
        <button
          type="button"
          onClick={() => setRangeMode("current")}
          className={rangeMode === "current" ? "active" : ""}
        >
          {t("跟随当前位置")}
        </button>
      </div>
      <p className="chat-context-hint" aria-live="polite">{t(contextHint)}</p>

      <div
        className="chat-messages"
        role="log"
        aria-live="polite"
        aria-relevant="additions text"
        aria-busy={conversation.loading || conversation.sending}
      >
        {conversation.loading && (
          <p className="chat-pending" role="status">
            {t("正在读取歌曲对话…")}
          </p>
        )}
        {!conversation.messages.length && !conversation.loading && (
          <div className="chat-empty">
            <strong>{t("从你的感受开始也可以")}</strong>
            <p>
              {t("例如：“我觉得这里突然变得很开阔，具体是什么声音造成的？”")}
            </p>
          </div>
        )}
        {conversation.messages.map((message) => (
          <div className="conversation-turn" key={message.id}>
            <div className="user-question">
              <span>{t("你")}</span>
              <p>{message.request.message}</p>
            </div>
            {message.status === "complete" && message.response ? (
              <ChatAnswer
                response={message.response}
                onSuggestedQuestion={(next) => void submit(next, "current")}
              />
            ) : message.status === "failed" ? (
              <p className="chat-error" role="alert">
                {t(message.error || "回答失败")}
              </p>
            ) : (
              <p className="chat-pending" role="status">
                {t("老师正在整理附近的听觉证据…")}
              </p>
            )}
          </div>
        ))}
      </div>

      {conversation.failedSend && (
        <div className="chat-send-error" role="alert">
          <p>{t(conversation.failedSend.error)}</p>
          <button
            type="button"
            disabled={conversation.sending}
            onClick={() => void retryFailedSend()}
          >
            {conversation.sending ? t("正在重试…") : t("重试同一问题")}
          </button>
        </div>
      )}
      {conversation.error && !conversation.failedSend && (
        <p className="chat-error" role="alert">{t(conversation.error)}</p>
      )}
      <form onSubmit={submitForm} className="chat-composer">
        <label htmlFor="music-question">
          {t("你的问题或理解")}
          <small>
            {rangeMode === "compare"
              ? t("将比较 A/B 两段")
              : rangeMode === "selection"
                ? t("将携带当前框选范围")
                : t("将携带当前播放位置")}
          </small>
        </label>
        <textarea
          id="music-question"
          maxLength={4000}
          rows={3}
          value={question}
          placeholder={t("我为什么会在这里产生这种感觉？")}
          onChange={(event) => setQuestion(event.target.value)}
        />
        <button
          type="submit"
          disabled={
            conversation.sending
            || !question.trim()
            || (rangeMode === "selection" && !snapshot.selectedRange)
            || (rangeMode === "compare" && (!snapshot.rangeA || !snapshot.rangeB))
          }
        >
          {conversation.sending ? t("正在聆听证据…") : t("提问")}
        </button>
      </form>
    </aside>
  );
}
